"""Explicit registry of installed supplier adapters."""

from .base import Adapter
from .openai_images import OpenAIImages

ADAPTERS: dict[str, Adapter] = {"openai_images": OpenAIImages()}
