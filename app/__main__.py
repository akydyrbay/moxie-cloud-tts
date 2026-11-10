"""Single-worker launcher with a separate HTTP port from the general API."""

import uvicorn

from app.config import Settings
from app.logging_conf import configure_logging


def main():
    configure_logging()
    uvicorn.run("app.main:app", host="0.0.0.0", port=Settings().HTTP_PORT, workers=1)


if __name__ == "__main__":
    main()
