"""Reference adapter for the documented OpenAI Images request and response shapes."""

import asyncio
import json
import re

import httpx

from .base import ImageRequest, ProviderError
from ..config import Profile, origin
from ..images import MAX_RESULT, decode_base64


PROVIDER_CODES = {
    "insufficient_quota": "provider_quota",
    "insufficient_user_quota": "provider_quota",
    "insufficient_balance": "provider_quota",
    "billing_hard_limit_reached": "provider_quota",
    "rate_limit_exceeded": "provider_rate_limited",
    "model_not_found": "provider_model_unavailable",
    "model_not_supported": "provider_model_unavailable",
    "model_not_available": "provider_model_unavailable",
    "content_policy_violation": "provider_policy",
    "content_filter": "provider_policy",
    "invalid_api_key": "provider_auth",
}
HTTP_REJECTIONS = {
    401: "provider_auth",
    402: "provider_quota",
    403: "provider_auth",
    404: "provider_model_unavailable",
    429: "provider_rate_limited",
}


async def read_bounded(response: httpx.Response, limit: int) -> bytes:
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ProviderError("output_invalid")
    chunks = bytearray()
    async for chunk in response.aiter_raw(chunk_size=64 * 1024):
        if len(chunks) + len(chunk) > limit:
            raise ProviderError("output_invalid")
        chunks.extend(chunk)
    return bytes(chunks)


async def response_failure(response: httpx.Response) -> ProviderError:
    status = response.status_code
    if status == 408 or status >= 500:
        return ProviderError("submission_uncertain", http_status=status)
    if response.is_redirect:
        return ProviderError("backend_redirected", http_status=status)

    provider_code = None
    try:
        body = json.loads(await read_bounded(response, 16 * 1024))
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and code in PROVIDER_CODES:
            provider_code = code
    except (httpx.RequestError, ProviderError, ValueError):
        pass
    reason = PROVIDER_CODES[provider_code] if provider_code else HTTP_REJECTIONS.get(status, "provider_request_rejected")
    return ProviderError(reason, http_status=status, provider_code=provider_code)


class OpenAIImages:
    def request_fields(self, profile: Profile, request: ImageRequest) -> dict[str, str]:
        payload = {}
        if request.size and request.size != "auto":
            if not re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", request.size):
                raise ProviderError("invalid_size")
            payload["size"] = request.size
        quality = profile.quality.get(request.preset, profile.options.get("default_quality"))
        if quality is not None:
            payload["quality"] = quality
        return payload

    def normalize_result(self, image: bytes) -> bytes:
        return image

    async def generate(self, profile: Profile, request: ImageRequest, client: httpx.AsyncClient) -> bytes:
        payload = {"model": profile.model, "prompt": request.prompt, **self.request_fields(profile, request)}
        url = profile.connection.base_url + "/images/"
        if request.images:
            if not profile.supports_edits:
                raise ProviderError("edit_unsupported")
            url += "edits"
            parts = [("image[]" if len(request.images) > 1 else "image", (f"reference-{i}.png", image, "image/png"))
                     for i, image in enumerate(request.images, 1)]
            body = {"data": payload, "files": parts}
        else:
            url += "generations"
            body = {"json": payload}
        try:
            async with client.stream("POST", url, headers={"Authorization": f"Bearer {profile.connection.api_key}"},
                                     **body) as response:
                if not response.is_success:
                    raise await response_failure(response)
                raw = await read_bounded(response, 4 * ((MAX_RESULT + 2) // 3) + 1024 * 1024)
        except httpx.RequestError as exc:
            raise ProviderError("submission_uncertain") from exc
        try:
            entries = json.loads(raw)["data"]
            if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
                raise ValueError("Expected a single image")
            image = entries[0]
            if isinstance(image.get("b64_json"), str):
                result = decode_base64(image["b64_json"], MAX_RESULT)
            elif isinstance(image.get("url"), str):
                result = await self._download(profile, image["url"], client)
            else:
                raise ValueError("Missing image data")
            return await asyncio.to_thread(self.normalize_result, result)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("output_invalid") from exc

    async def _download(self, profile: Profile, url: str, client: httpx.AsyncClient) -> bytes:
        allowed = (origin(profile.connection.base_url), *profile.connection.result_origins)
        if origin(url) not in allowed:
            raise ProviderError("output_source_denied")
        try:
            client.cookies.clear()
            async with client.stream("GET", url) as response:
                if not response.is_success:
                    raise ProviderError("output_unavailable")
                return await read_bounded(response, MAX_RESULT)
        except httpx.RequestError as exc:
            raise ProviderError("output_unavailable") from exc
