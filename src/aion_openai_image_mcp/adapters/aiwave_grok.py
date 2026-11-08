"""Grok Imagine generation routed through the AiWave Images API."""

from io import BytesIO
from math import gcd
import re

from PIL import Image

from .base import ImageRequest, ProviderError
from .openai_images import OpenAIImages
from ..config import Profile
from ..images import PNG_HEADER


class AiWaveGrokImages(OpenAIImages):
    def request_fields(self, profile: Profile, request: ImageRequest) -> dict[str, str]:
        fields = {"response_format": "b64_json"}
        if request.size and request.size != "auto":
            size = re.fullmatch(r"([1-9][0-9]{0,4})[x:]([1-9][0-9]{0,4})", request.size)
            if size is None:
                raise ProviderError("invalid_size")
            width, height = map(int, size.groups())
            divisor = gcd(width, height)
            fields["aspect_ratio"] = f"{width // divisor}:{height // divisor}"
        return fields

    def normalize_result(self, image: bytes) -> bytes:
        if image.startswith(PNG_HEADER):
            return image
        try:
            with Image.open(BytesIO(image), formats=["JPEG", "WEBP"]) as source:
                width, height = source.size
                if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 32_000_000):
                    raise ValueError("Image dimensions exceed the limit")
                if getattr(source, "is_animated", False):
                    raise ValueError("Animated images are unsupported")
                with source.convert("RGBA" if "A" in source.getbands() else "RGB") as converted:
                    converted.info.clear()
                    output = BytesIO()
                    converted.save(output, format="PNG")
            return output.getvalue()
        except (OSError, SyntaxError, EOFError, Image.DecompressionBombError) as exc:
            raise ValueError("Invalid Grok image") from exc
