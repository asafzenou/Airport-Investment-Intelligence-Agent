"""Shared retry helper for BTS PREZIP HTTP downloads."""

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TRANSIENT_ERRORS = (httpx.ReadError, httpx.ReadTimeout, httpx.ConnectTimeout)
_BACKOFF_SECS = (1.0, 2.0)


async def get_with_retry(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """GET *url* retrying up to 3 times on transient transport errors.

    Backoff: 1 s after the first failure, 2 s after the second.
    4xx/5xx HTTP responses are returned as-is; callers decide whether to
    raise_for_status().
    """
    for attempt in range(1, 4):
        try:
            return await client.get(url, **kwargs)
        except _TRANSIENT_ERRORS as exc:
            if attempt == 3:
                log.warning("All 3 attempts failed for %s: %s", url, exc)
                raise
            delay = _BACKOFF_SECS[attempt - 1]
            log.warning(
                "Attempt %d/3 failed for %s: %s; retrying in %.0fs",
                attempt, url, exc, delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
