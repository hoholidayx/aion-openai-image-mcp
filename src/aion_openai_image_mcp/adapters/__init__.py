"""Explicit registry of installed supplier adapters."""

from .aiwave_grok import AiWaveGrokImages
from .base import Adapter
from .openai_images import OpenAIImages

ADAPTERS: dict[str, Adapter] = {"openai_images": OpenAIImages(), "aiwave_grok": AiWaveGrokImages()}
