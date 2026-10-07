from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn

from app.config import get_settings
from app.logging_setup import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    host = os.environ.get("HOST", settings.host)
    port = int(os.environ.get("PORT", str(settings.port)))
    uvicorn.run("app.main:app", host=host, port=port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
