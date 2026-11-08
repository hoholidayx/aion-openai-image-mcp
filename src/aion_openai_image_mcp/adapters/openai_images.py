"""Reference adapter for the documented OpenAI Images request and response shapes."""

import json
import re

import httpx

from .base import ImageRequest, ProviderError
from ..config import Profile, origin
from ..images import MAX_RESULT, decode_base64


async def read_bounded(response: httpx.Response, limit: int) -> bytes:
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ProviderError("output_invalid")
    chunks = bytearray()
    async for chunk in response.aiter_raw(chunk_size=64 * 1024):
        if len(chunks) + len(chunk) > limit:
            raise ProviderError("output_invalid")
        chunks.extend(chunk)
    return bytes(chunks)


class OpenAIImages:
    async def generate(self, profile: Profile, request: ImageRequest, client: httpx.AsyncClient) -> bytes:
        payload = {"model": profile.model, "prompt": request.prompt}
        if request.size and request.size != "auto":
            if not re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", request.size):
                raise ProviderError("generation_failed")
            payload["size"] = request.size
        quality = profile.quality.get(request.preset, profile.options.get("default_quality"))
        if quality is not None:
            payload["quality"] = quality
        url = profile.connection.base_url + "/images/"
        if request.images:
            if not profile.supports_edits:
                raise ProviderError("generation_failed")
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
                if response.status_code >= 500:
                    raise ProviderError("submission_uncertain")
                if not response.is_success:
                    raise ProviderError("generation_failed")
                raw = await read_bounded(response, 4 * ((MAX_RESULT + 2) // 3) + 1024 * 1024)
        except httpx.RequestError as exc:
            raise ProviderError("submission_uncertain") from exc
        try:
            entries = json.loads(raw)["data"]
            if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
                raise ValueError("Expected a single image")
            image = entries[0]
            if isinstance(image.get("b64_json"), str):
                return decode_base64(image["b64_json"], MAX_RESULT)
            if isinstance(image.get("url"), str):
                return await self._download(profile, image["url"], client)
            raise ValueError("Missing image data")
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("output_invalid") from exc

    async def _download(self, profile: Profile, url: str, client: httpx.AsyncClient) -> bytes:
        allowed = (origin(profile.connection.base_url), *profile.connection.result_origins)
        if origin(url) not in allowed:
            raise ProviderError("output_unavailable")
        try:
            client.cookies.clear()
            async with client.stream("GET", url) as response:
                if not response.is_success:
                    raise ProviderError("output_unavailable")
                return await read_bounded(response, MAX_RESULT)
        except httpx.RequestError as exc:
            raise ProviderError("output_unavailable") from exc
