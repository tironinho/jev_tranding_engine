from __future__ import annotations

from app.api.router import create_app
from app.logging_setup import configure_logging
from app.config import get_settings

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)
app = create_app(settings)
