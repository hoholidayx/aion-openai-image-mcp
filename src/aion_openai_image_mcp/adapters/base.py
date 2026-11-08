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
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Adapter(Protocol):
    async def generate(self, profile: Profile, request: ImageRequest, client: httpx.AsyncClient) -> bytes: ...
