"""Every model is reached through OpenRouter, so a model is just its OpenRouter id."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import RawResult

if TYPE_CHECKING:
    from .openrouter import OpenRouterProvider


def make_provider(model: str) -> OpenRouterProvider:
    from .openrouter import OpenRouterProvider  # deferred: report and validate never need the openai SDK

    return OpenRouterProvider(model)


__all__ = ["RawResult", "make_provider"]
