from __future__ import annotations

from app.config import Settings
from app.providers.jev.mock import MockJevProvider
from app.providers.jev.real import RealJevProvider


def build_jev_provider(settings: Settings):
    choice = (settings.jev_provider or "auto").lower()
    has_remote_hint = bool(settings.jev_api_key or settings.jev_base_url)
    if choice == "mock" or (choice == "auto" and not has_remote_hint):
        return MockJevProvider(settings.jev_prompt_version)
    return RealJevProvider(
        prompt_version=settings.jev_prompt_version,
        base_url=settings.jev_base_url,
        api_key=settings.jev_api_key,
        model=settings.jev_model,
        timeout_s=settings.jev_timeout_s,
        min_data_quality=settings.min_jev_data_quality,
    )
