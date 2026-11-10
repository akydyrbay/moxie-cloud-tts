"""Shared OpenAI client factory.

Origin: new for the OpenAI speech pipeline (replaces the Google Cloud TTS
provider). A single lazily-constructed `OpenAI` client is shared by STT, chat,
translation and TTS. Translation uses a per-call timeout/retry override without
changing the client used by the other stages.

The API key comes from `settings.OPENAI_API_KEY` (populated from the
OPENAI_API_KEY environment variable) — it is NEVER hardcoded and NEVER logged.
The client is built on first use so the app can boot and non-OpenAI routes work
without a key present; a clear error is raised only when a stage actually needs
to call the API.
"""

import logging
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING

from app.config import settings
from app.core.context import Local

if TYPE_CHECKING:
    from openai import OpenAI

logger = logging.getLogger(__name__)


class OpenAIConfigError(RuntimeError):
    """Raised when the OpenAI client is needed but no API key is configured."""


client_state = Local("provider_client", lambda: SimpleNamespace(client=None, lock=threading.Lock(), closed=False))


def get_client() -> "OpenAI":
    """Return a process-wide OpenAI client, constructing it on first use.

    Raises `OpenAIConfigError` if `OPENAI_API_KEY` is not configured. The
    `openai` package is imported lazily so importing this module never requires
    the dependency to be installed.
    """
    if client_state.closed:
        raise OpenAIConfigError("Provider client is closed")
    if client_state.client is not None:
        return client_state.client

    with client_state.lock:
        if client_state.closed:
            raise OpenAIConfigError("Provider client is closed")
        if client_state.client is not None:
            return client_state.client

        api_key = settings.OPENAI_API_KEY
        if not api_key:
            msg = (
                "OPENAI_API_KEY is not set. Provide it via the environment "
                "(never hardcode it) before using the OpenAI speech pipeline."
            )
            raise OpenAIConfigError(msg)

        # Lazy import: only require the `openai` package once a key is present.
        from openai import OpenAI  # noqa: PLC0415

        client_state.client = OpenAI(
            api_key=api_key,
            timeout=settings.OPENAI_REQUEST_TIMEOUT_S,
        )
        # Log that a client was created WITHOUT logging the key itself.
        logger.info(
            "constructed OpenAI client (timeout=%ss)",
            settings.OPENAI_REQUEST_TIMEOUT_S,
        )
        return client_state.client


def reset_client() -> None:
    """Drop the cached client (used by tests to force reconstruction)."""
    with client_state.lock:
        client_state.client = None
        client_state.closed = False


def close_client() -> None:
    """Close this app's HTTP resources and disallow lazy recreation."""
    with client_state.lock:
        client_state.closed = True
        client, client_state.client = client_state.client, None
    if client is not None:
        client.close()
