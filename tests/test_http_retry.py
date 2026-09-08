"""Tests for the shared PREZIP retry helper."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from data_pipeline.etls._http_retry import get_with_retry

_URL = "https://transtats.bts.gov/PREZIP/On_Time_2025_1.zip"


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace asyncio.sleep with a no-op and record delays."""
    slept: list[float] = []

    async def _fake(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake)
    return slept


async def test_transient_failure_then_success(no_sleep: list[float]) -> None:
    ok = httpx.Response(200, content=b"PK\x03\x04")
    mock_client = AsyncMock()
    mock_client.get.side_effect = [httpx.ReadError("connection reset"), ok]

    result = await get_with_retry(mock_client, _URL)

    assert result is ok
    assert mock_client.get.call_count == 2
    assert no_sleep == [1.0]


async def test_all_retry_attempts_fail(no_sleep: list[float]) -> None:
    exc = httpx.ReadError("read timeout")
    mock_client = AsyncMock()
    mock_client.get.side_effect = [exc, exc, exc]

    with pytest.raises(httpx.ReadError):
        await get_with_retry(mock_client, _URL)

    assert mock_client.get.call_count == 3
    assert no_sleep == [1.0, 2.0]


async def test_http_4xx_not_retried(no_sleep: list[float]) -> None:
    mock_client = AsyncMock()
    mock_client.get.return_value = httpx.Response(404)

    result = await get_with_retry(mock_client, _URL)

    assert result.status_code == 404
    assert mock_client.get.call_count == 1
    assert no_sleep == []
