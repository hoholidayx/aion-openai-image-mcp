"""Grok Imagine generation routed through the AiWave Images API."""

from io import BytesIO
from math import gcd
import re

from PIL import Image

from .base import ImageRequest, ProviderError
from .openai_images import OpenAIImages
from ..config import Profile
from ..images import PNG_HEADER, bounded_dimensions


class AiWaveGrokImages(OpenAIImages):
    def validate_options(self, profile: Profile) -> None:
        if profile.supports_edits:
            raise ValueError("The aiwave_grok adapter only supports image generation")

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
                bounded_dimensions(source)
                if getattr(source, "is_animated", False):
                    raise ValueError("Animated images are unsupported")
                with source.convert("RGBA" if "A" in source.getbands() else "RGB") as converted:
                    converted.info.clear()
                    output = BytesIO()
                    converted.save(output, format="PNG")
            return output.getvalue()
        except (OSError, SyntaxError, EOFError, Image.DecompressionBombError) as exc:
            raise ValueError("Invalid Grok image") from exc
