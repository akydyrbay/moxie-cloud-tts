"""Logging setup.

UPDATED from your uploaded main.py (2026-07-14): that file adds a
TimedRotatingFileHandler writing to log/debug.log (daily rotation, 7-day
backlog) alongside the console, and simplifies the format string to drop
levelname/name. Both are copied here verbatim.

KEPT from my first draft, not present in your uploaded file: the `paho`
logger override pinning internal chatter to WARNING. Your senior's
instruction was explicit — "print all messages, but explicitly filter out and
mute MQTT request logs" — and the uploaded main.py doesn't do this itself, so
I've kept it layered on top rather than dropping it.
"""

import logging.config
from pathlib import Path


def configure_logging() -> None:
    Path("log").mkdir(exist_ok=True)

    logging.config.dictConfig(
        {
            "version": 1.0,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s | %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                },
                "file": {
                    "class": "logging.handlers.TimedRotatingFileHandler",
                    "filename": "log/debug.log",
                    "when": "D",
                    "interval": 1,
                    "backupCount": 7,
                    "formatter": "default",
                },
            },
            "loggers": {
                "": {
                    "handlers": ["console", "file"],
                    "level": logging.DEBUG,
                    "propagate": False,
                },
                # paho-mqtt's own internal logger, if ever enabled via
                # client.enable_logger() — muted so it can't spam the console.
                "paho": {
                    "handlers": ["console", "file"],
                    "level": logging.WARNING,
                    "propagate": False,
                },
            },
        },
    )
