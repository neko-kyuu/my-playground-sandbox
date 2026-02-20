import asyncio
import random
import time
from typing import Any, AsyncGenerator, Dict, List

import httpx

from proxy_config import (
    BACKOFF_INITIAL,
    BACKOFF_MAX,
    CONCURRENCY_LIMIT,
    DEBUG_UPSTREAM,
    MAX_RETRIES,
    MIN_REQUEST_INTERVAL,
)

import logging


logger = logging.getLogger("claude_to_openai_proxy")


sema = asyncio.Semaphore(CONCURRENCY_LIMIT)

# Global throttle (min interval between upstream requests)
_last_req_lock = asyncio.Lock()
_last_req_ts = 0.0


async def throttle_interval() -> None:
    """Ensure at least MIN_REQUEST_INTERVAL seconds between upstream requests (global)."""
    global _last_req_ts
    if MIN_REQUEST_INTERVAL <= 0:
        return
    async with _last_req_lock:
        now = time.monotonic()
        wait = (_last_req_ts + MIN_REQUEST_INTERVAL) - now
        if wait > 0:
            await asyncio.sleep(wait)
        _last_req_ts = time.monotonic()


def retriable_status(code: int) -> bool:
    return code in (429, 500, 502, 503, 504)


def compute_backoff(attempt: int, headers: httpx.Headers) -> float:
    """attempt starts from 1"""
    ra = headers.get("Retry-After")
    if ra:
        try:
            return float(ra)
        except Exception:
            pass
    sleep_s = min(BACKOFF_MAX, BACKOFF_INITIAL * (2 ** (attempt - 1)))
    return sleep_s * (0.7 + random.random() * 0.6)  # jitter


async def post_with_retry_json(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    json_payload: Dict[str, Any],
) -> httpx.Response:
    attempt = 0
    while True:
        await throttle_interval()
        r = await client.post(url, headers=headers, json=json_payload)

        if not retriable_status(r.status_code):
            return r

        attempt += 1
        if attempt > MAX_RETRIES:
            return r

        sleep_s = compute_backoff(attempt, r.headers)
        if DEBUG_UPSTREAM:
            logger.debug("UPSTREAM non-stream retryable status=%s sleep=%.2fs", r.status_code, sleep_s)
        await asyncio.sleep(sleep_s)


async def iter_sse_data_events(resp: httpx.Response) -> AsyncGenerator[str, None]:
    """
    Parse SSE and yield each event's merged `data:` payload string.
    Events are separated by an empty line.
    """
    buf: List[str] = []
    async for line in resp.aiter_lines():
        line = line.rstrip("\r")
        if line.startswith("data:"):
            buf.append(line[len("data:") :].lstrip())
            continue

        if line == "":
            if buf:
                yield "\n".join(buf)
                buf = []
            continue

        # Ignore event:, id:, retry:, comments...
        continue

    if buf:
        yield "\n".join(buf)

