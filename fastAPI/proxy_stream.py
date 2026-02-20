import asyncio
import contextlib
import json
import logging
import random
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

from proxy_config import (
    BACKOFF_INITIAL,
    BACKOFF_MAX,
    DEBUG_UPSTREAM,
    DROP_UNKNOWN_TOOL_CALLS,
    EAGER_MESSAGE_START,
    MAX_RETRIES,
    STREAM_READERROR_FALLBACK_AFTER,
    STREAM_READERROR_FALLBACK_TO_NON_STREAM,
    STREAM_PREFETCH_CHARS,
    STREAM_PREFETCH_SECONDS,
)
from proxy_convert import anthropic_sse, map_finish_reason, openai_to_anthropic_full
from proxy_upstream import (
    compute_backoff,
    iter_sse_data_events,
    post_with_retry_json,
    retriable_status,
    throttle_interval,
)


logger = logging.getLogger("claude_to_openai_proxy")


async def _openai_non_stream_to_anthropic_sse(
    openai_resp: Dict[str, Any],
    *,
    incoming_model: str,
    fallback_model: str,
) -> AsyncGenerator[str, None]:
    """
    Emit an Anthropic SSE stream from a non-stream OpenAI Chat Completions response.

    This is used as a fallback when the upstream streaming connection keeps resetting.
    """
    msg = openai_to_anthropic_full(
        openai_resp,
        incoming_model=incoming_model,
        fallback_model=fallback_model,
    )

    # message_start
    yield anthropic_sse(
        {
            "type": "message_start",
            "message": {
                "id": msg.get("id"),
                "type": "message",
                "role": "assistant",
                "model": msg.get("model"),
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": msg.get("usage") or {"input_tokens": 0, "output_tokens": 0},
            },
        }
    )

    # content blocks
    content = msg.get("content") or []
    for idx, block in enumerate(content):
        btype = block.get("type")
        if btype == "text":
            text = block.get("text") or ""
            yield anthropic_sse(
                {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}}
            )
            if text:
                yield anthropic_sse(
                    {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text}}
                )
            yield anthropic_sse({"type": "content_block_stop", "index": idx})
            continue

        if btype == "tool_use":
            tool_id = block.get("id")
            tool_name = block.get("name")
            tool_input = block.get("input") or {}
            yield anthropic_sse(
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}},
                }
            )
            args_json = json.dumps(tool_input, ensure_ascii=False)
            yield anthropic_sse(
                {"type": "content_block_delta", "index": idx, "delta": {"type": "input_json_delta", "partial_json": args_json}}
            )
            yield anthropic_sse({"type": "content_block_stop", "index": idx})
            continue

        # Unknown block type: ignore.

    # message_stop
    yield anthropic_sse(
        {
            "type": "message_delta",
            "delta": {"stop_reason": msg.get("stop_reason") or "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": (msg.get("usage") or {}).get("output_tokens", 0)},
        }
    )
    yield anthropic_sse({"type": "message_stop"})


async def openai_stream_to_anthropic_sse_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    incoming_model: str,
    fallback_model: str,
    allowed_tool_names: Optional[set] = None,
    warned_tool_suppressed: bool = False,
) -> AsyncGenerator[str, None]:
    attempt = 0

    # Prefetch buffer to reduce "partial then disconnect" cases.
    prefetch_started_at: Optional[float] = None
    prefetch_chars = 0
    preflush_events: List[Tuple[str, Any]] = []
    flushed = False  # once flushed, we can't retry without breaking downstream

    # Downstream Anthropic message state
    started_message = False
    message_id = f"msg_{uuid.uuid4().hex}"

    index = 0
    text_open = False
    text_index: Optional[int] = None
    tool_states: Dict[str, Dict[str, Any]] = {}
    finish_reason = None
    did_non_stream_fallback = False

    def tool_allowed(name: Optional[str]) -> bool:
        if not DROP_UNKNOWN_TOOL_CALLS:
            return True
        if allowed_tool_names is None:
            return True
        if name is None:
            return True
        return name in allowed_tool_names

    def record_event(kind: str, event_payload: Any) -> None:
        nonlocal prefetch_started_at, prefetch_chars
        if prefetch_started_at is None:
            prefetch_started_at = time.monotonic()
        preflush_events.append((kind, event_payload))
        if kind == "text":
            prefetch_chars += len(event_payload)

    def reset_prefetch_buffer() -> None:
        nonlocal prefetch_started_at, prefetch_chars, preflush_events
        prefetch_started_at = None
        prefetch_chars = 0
        preflush_events = []

    async def emit_message_start() -> AsyncGenerator[str, None]:
        nonlocal started_message
        if started_message:
            return
        started_message = True
        yield anthropic_sse(
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": incoming_model or fallback_model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
        )

    async def flush_prefetch_if_needed(force: bool = False) -> AsyncGenerator[str, None]:
        nonlocal flushed, preflush_events, prefetch_started_at, prefetch_chars
        if flushed:
            return

        if not force:
            if not preflush_events:
                return
            now = time.monotonic()
            elapsed = 0 if prefetch_started_at is None else (now - prefetch_started_at)
            if prefetch_chars < STREAM_PREFETCH_CHARS and elapsed < STREAM_PREFETCH_SECONDS:
                return

        flushed = True

        # message_start first
        async for e in emit_message_start():
            yield e

        # Replay buffered events
        for kind, p in preflush_events:
            if kind == "text":
                async for e in _emit_text_delta(p):
                    yield e
            elif kind == "tool":
                async for e in _emit_tool_delta(p):
                    yield e

        preflush_events = []
        prefetch_started_at = None
        prefetch_chars = 0

    async def _emit_text_delta(text_piece: str) -> AsyncGenerator[str, None]:
        nonlocal index, text_open, text_index
        if not started_message:
            async for e in emit_message_start():
                yield e
        if not text_open:
            text_open = True
            text_index = index
            yield anthropic_sse(
                {
                    "type": "content_block_start",
                    "index": text_index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            index += 1
        yield anthropic_sse(
            {
                "type": "content_block_delta",
                "index": text_index,
                "delta": {"type": "text_delta", "text": text_piece},
            }
        )

    async def _emit_tool_delta(tc: Dict[str, Any]) -> AsyncGenerator[str, None]:
        """
        tc: {id, index, function:{name, arguments(partial)}}
        """
        nonlocal index, text_open, text_index, tool_states
        fn = tc.get("function") or {}
        name = fn.get("name")
        if not tool_allowed(name):
            return

        if not started_message:
            async for e in emit_message_start():
                yield e

        # Close open text block before tool blocks.
        if text_open:
            yield anthropic_sse({"type": "content_block_stop", "index": text_index})
            text_open = False
            text_index = None

        tc_id = tc.get("id")
        tc_idx = tc.get("index")
        args_part = fn.get("arguments")

        key = tc_id or f"tc_{tc_idx}"
        st = tool_states.get(key)
        if st is None:
            st = {"index": index, "name": None, "started": False, "id": tc_id or key}
            tool_states[key] = st
            index += 1

        if name:
            st["name"] = name

        if st.get("name") is None:
            return  # wait for later chunks with a name

        if (not st["started"]) and st.get("name"):
            st["started"] = True
            yield anthropic_sse(
                {
                    "type": "content_block_start",
                    "index": st["index"],
                    "content_block": {
                        "type": "tool_use",
                        "id": st["id"],
                        "name": st["name"],
                        "input": {},
                    },
                }
            )

        if args_part is not None and st["started"]:
            yield anthropic_sse(
                {
                    "type": "content_block_delta",
                    "index": st["index"],
                    "delta": {"type": "input_json_delta", "partial_json": args_part},
                }
            )

    async def emit_error_and_stop(err_text: str) -> AsyncGenerator[str, None]:
        # If we haven't flushed anything yet, force flush so Claude Code sees the error.
        async for e in flush_prefetch_if_needed(force=True):
            yield e

        nonlocal text_open, text_index
        if text_open:
            yield anthropic_sse({"type": "content_block_stop", "index": text_index})
            text_open = False
            text_index = None

        yield anthropic_sse(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""},
            }
        )
        yield anthropic_sse(
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": err_text},
            }
        )
        yield anthropic_sse({"type": "content_block_stop", "index": index})

        yield anthropic_sse(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 0},
            }
        )
        yield anthropic_sse({"type": "message_stop"})

    while True:
        attempt += 1

        if EAGER_MESSAGE_START and (not started_message):
            async for e in emit_message_start():
                yield e

        await throttle_interval()

        resp = await client.stream("POST", url, headers=headers, json=payload).__aenter__()

        if DEBUG_UPSTREAM:
            logger.debug("UPSTREAM status=%s", resp.status_code)
            logger.debug("UPSTREAM headers=%s", dict(resp.headers))

        # Retry on connect-phase errors.
        if retriable_status(resp.status_code):
            body = await resp.aread()
            await resp.aclose()
            if attempt > MAX_RETRIES:
                async for e in emit_error_and_stop(
                    f"Upstream error {resp.status_code}: {body.decode('utf-8', errors='ignore')}"
                ):
                    yield e
                return
            sleep_s = compute_backoff(attempt, resp.headers)
            if DEBUG_UPSTREAM:
                logger.debug("UPSTREAM connect retry status=%s attempt=%s sleep=%.2fs", resp.status_code, attempt, sleep_s)
            await asyncio.sleep(sleep_s)
            continue

        if resp.status_code >= 400:
            body = await resp.aread()
            await resp.aclose()
            err = body.decode("utf-8", errors="ignore")
            logger.error("UPSTREAM error status=%s body=%s", resp.status_code, err[:2000])

            async for e in emit_error_and_stop(f"Upstream error {resp.status_code}: {err}"):
                yield e
            return

        # 200: read stream
        try:
            async for data in iter_sse_data_events(resp):
                if DEBUG_UPSTREAM and data:
                    logger.debug("UPSTREAM data=%s", data[:400])

                if data == "[DONE]":
                    break

                try:
                    chunk = json.loads(data)
                except Exception:
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                c0 = choices[0]
                delta = c0.get("delta") or {}
                if c0.get("finish_reason") is not None:
                    finish_reason = c0.get("finish_reason")

                # Text: prefer content, fallback to reasoning_content (some upstreams use it for main output).
                text_piece = None
                if "content" in delta and delta["content"] not in (None, ""):
                    text_piece = delta["content"]
                elif delta.get("reasoning_content") not in (None, ""):
                    text_piece = delta["reasoning_content"]

                if text_piece is not None:
                    if not flushed:
                        record_event("text", text_piece)
                        async for e in flush_prefetch_if_needed(force=False):
                            yield e
                    else:
                        async for e in _emit_text_delta(text_piece):
                            yield e

                tool_calls = delta.get("tool_calls") or []
                if tool_calls:
                    for tc in tool_calls:
                        fn = tc.get("function") or {}
                        name = fn.get("name")

                        if (name is not None) and (not tool_allowed(name)):
                            if not warned_tool_suppressed:
                                warned_tool_suppressed = True
                                msg = f"[proxy] suppressed unknown tool call: {name}"
                                if not flushed:
                                    record_event("text", msg)
                                    async for e in flush_prefetch_if_needed(force=False):
                                        yield e
                                else:
                                    async for e in _emit_text_delta(msg):
                                        yield e
                            continue

                        if not flushed:
                            record_event("tool", tc)
                            async for e in flush_prefetch_if_needed(force=False):
                                yield e
                        else:
                            async for e in _emit_tool_delta(tc):
                                yield e

        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.TransportError) as e:
            if DEBUG_UPSTREAM:
                logger.debug("UPSTREAM stream read error=%r", e)
            await resp.aclose()

            # Some upstreams reset the SSE connection; if we haven't sent anything downstream,
            # try a single non-stream request and replay it as Anthropic SSE.
            if (
                (not flushed)
                and (not started_message)
                and (not did_non_stream_fallback)
                and STREAM_READERROR_FALLBACK_TO_NON_STREAM
                and (attempt >= STREAM_READERROR_FALLBACK_AFTER)
            ):
                did_non_stream_fallback = True
                if DEBUG_UPSTREAM:
                    logger.debug("UPSTREAM switching to non-stream fallback after read error (attempt=%s)", attempt)
                reset_prefetch_buffer()
                try:
                    non_stream_payload = dict(payload)
                    non_stream_payload["stream"] = False
                    non_stream_headers = dict(headers)
                    non_stream_headers["Accept"] = "application/json"
                    r2 = await post_with_retry_json(client, url, non_stream_headers, non_stream_payload)
                    if r2.status_code < 400:
                        if DEBUG_UPSTREAM:
                            logger.debug("UPSTREAM non-stream fallback succeeded status=%s", r2.status_code)
                        openai_resp2 = r2.json()
                        async for sse in _openai_non_stream_to_anthropic_sse(
                            openai_resp2,
                            incoming_model=incoming_model,
                            fallback_model=fallback_model,
                        ):
                            yield sse
                        return
                    if DEBUG_UPSTREAM:
                        logger.debug(
                            "UPSTREAM non-stream fallback failed status=%s body=%s",
                            r2.status_code,
                            (r2.text or "")[:2000],
                        )
                except Exception as e2:
                    if DEBUG_UPSTREAM:
                        logger.debug("UPSTREAM non-stream fallback exception=%r", e2)

            # If we haven't flushed any content blocks, we can reconnect and retry.
            if (not flushed) and (attempt <= MAX_RETRIES):
                # Discard any buffered pieces from the failed attempt to avoid mixing outputs across retries.
                reset_prefetch_buffer()
                sleep_s = min(BACKOFF_MAX, BACKOFF_INITIAL * (2 ** (attempt - 1)))
                sleep_s *= (0.7 + random.random() * 0.6)
                if DEBUG_UPSTREAM:
                    logger.debug("UPSTREAM read error retry attempt=%s sleep=%.2fs", attempt, sleep_s)
                await asyncio.sleep(sleep_s)
                continue

            async for e2 in emit_error_and_stop(f"Upstream stream interrupted: {repr(e)}"):
                yield e2
            return
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass

        break

    # If upstream never emitted anything, flush an empty message.
    async for e in flush_prefetch_if_needed(force=True):
        yield e

    # Close blocks
    if text_open:
        yield anthropic_sse({"type": "content_block_stop", "index": text_index})
    for st in tool_states.values():
        if st.get("started"):
            yield anthropic_sse({"type": "content_block_stop", "index": st["index"]})

    yield anthropic_sse(
        {
            "type": "message_delta",
            "delta": {"stop_reason": map_finish_reason(finish_reason), "stop_sequence": None},
            "usage": {"output_tokens": 0},
        }
    )
    yield anthropic_sse({"type": "message_stop"})


async def with_sse_keepalive(
    source: AsyncGenerator[str, None],
    interval: float,
) -> AsyncGenerator[str, None]:
    """
    If no output within `interval` seconds, send an SSE comment line ': ping\\n\\n'.
    This keeps the downstream connection alive without cancelling the upstream read.
    """
    if interval <= 0:
        async for x in source:
            yield x
        return

    q: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()

    async def pump() -> None:
        try:
            async for item in source:
                await q.put(item)
        finally:
            await q.put(SENTINEL)

    task = asyncio.create_task(pump())

    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue

            if item is SENTINEL:
                break
            yield item
    finally:
        task.cancel()
        with contextlib.suppress(Exception):
            await task
