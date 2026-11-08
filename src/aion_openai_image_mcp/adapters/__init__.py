"""Explicit registry of installed supplier adapters."""

from ..config import Config
from .aiwave_grok import AiWaveGrokImages
from .base import Adapter
from .openai_images import OpenAIImages

ADAPTERS: dict[str, Adapter] = {"openai_images": OpenAIImages(), "aiwave_grok": AiWaveGrokImages()}


def validate_profiles(config: Config) -> None:
    for profile in config.profiles.values():
        adapter = ADAPTERS.get(profile.adapter)
        if adapter is None:
            raise ValueError("A configured profile has an unknown adapter")
        adapter.validate_profile(profile)
