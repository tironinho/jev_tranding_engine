from __future__ import annotations

import logging
import sys
from logging.config import dictConfig


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key in ("correlation_id", "snapshot_id", "decision_id", "trade_id", "symbol", "strategy"):
            if not hasattr(record, key):
                setattr(record, key, "-")
        return True


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    formatter = "json" if json_logs else "plain"
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"context": {"()": _ContextFilter}},
            "formatters": {
                "plain": {
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s correlation=%(correlation_id)s snapshot=%(snapshot_id)s decision=%(decision_id)s trade=%(trade_id)s symbol=%(symbol)s strategy=%(strategy)s"
                },
                "json": {
                    "format": '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s","correlation_id":"%(correlation_id)s","snapshot_id":"%(snapshot_id)s","decision_id":"%(decision_id)s","trade_id":"%(trade_id)s","symbol":"%(symbol)s","strategy":"%(strategy)s"}'
                },
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                    "formatter": formatter,
                    "filters": ["context"],
                }
            },
            "root": {"handlers": ["stdout"], "level": level.upper()},
        }
    )
