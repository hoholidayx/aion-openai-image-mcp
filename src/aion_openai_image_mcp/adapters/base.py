"""Provider adapter boundary. Adapters receive no MCP request or task data."""

from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import Profile


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    size: str | None
    preset: str | None
    images: tuple[bytes, ...]


class ProviderError(Exception):
    def __init__(self, code: str, *, http_status: int | None = None, provider_code: str | None = None):
        self.code = code
        self.http_status = http_status
        self.provider_code = provider_code
        super().__init__(code)


class Adapter(Protocol):
    def validate_profile(self, profile: Profile) -> None: ...

    async def generate(self, profile: Profile, request: ImageRequest, client: httpx.AsyncClient) -> bytes: ...
