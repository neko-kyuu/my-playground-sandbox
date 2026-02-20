import json
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from proxy_config import (
    BASE_DIR,
    CONCURRENCY_LIMIT,
    DEBUG_UPSTREAM,
    DEFAULT_OPENAI_MODEL,
    KEEPALIVE_INTERVAL,
    MAX_RETRIES,
    MIN_REQUEST_INTERVAL,
    MODEL_MAP,
    MODELSCOPE_BASE_URL,
    OPENAI_ACCESS_TOKEN,
    UPSTREAM_TOOLS_STYLE,
)
from proxy_convert import anthropic_to_openai_messages, openai_to_anthropic_full
from proxy_logging import setup_debug_logging
from proxy_stream import openai_stream_to_anthropic_sse_with_retry, with_sse_keepalive
from proxy_tools import anthropic_tools_to_openai_tools
from proxy_upstream import post_with_retry_json, sema


# Configure logs early (overwrite per process start, not append).
setup_debug_logging(log_dir=BASE_DIR, filename="debug.log", level=logging.DEBUG, also_console=True)
logger = logging.getLogger("claude_to_openai_proxy")


app = FastAPI()


def _style_norm(style: str) -> str:
    # Allow env like "openai# comment".
    return (style or "").split("#", 1)[0].strip().lower()


@app.post("/v1/messages")
async def v1_messages(
    request: Request,
    anthropic_version: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
):
    req = await request.json()

    incoming_model = req.get("model")
    target_model = MODEL_MAP.get(incoming_model) or DEFAULT_OPENAI_MODEL
    want_stream = bool(req.get("stream", False))

    tool_names = []
    if isinstance(req.get("tools"), list):
        for t in req.get("tools") or []:
            if isinstance(t, dict) and t.get("name"):
                tool_names.append(t.get("name"))
                if DEBUG_UPSTREAM and t.get("name") == "Grep":
                    try:
                        logger.debug("IN Grep input_schema=%s", json.dumps(t.get("input_schema"), ensure_ascii=False)[:8000])
                    except Exception:
                        logger.debug("IN Grep input_schema=<unserializable>")

    logger.debug(
        "IN /v1/messages stream=%s incoming_model=%s target_model=%s tools=%s",
        want_stream,
        incoming_model,
        target_model,
        tool_names,
    )

    openai_payload: Dict[str, Any] = {
        "model": target_model,
        "messages": anthropic_to_openai_messages(req),
        "stream": want_stream,
    }

    for k in ("max_tokens", "temperature", "top_p"):
        if k in req:
            openai_payload[k] = req[k]

    tools_list = req.get("tools")
    openai_tools = anthropic_tools_to_openai_tools(req)

    style = _style_norm(UPSTREAM_TOOLS_STYLE)

    if tools_list and openai_tools:
        openai_payload["tools"] = openai_tools
        if style == "openai":
            openai_payload["tool_choice"] = "auto"
        else:
            openai_payload.pop("tool_choice", None)
    else:
        # tools is [] or missing: explicitly disable tool calling upstream.
        openai_payload["tool_choice"] = "none"

    if DEBUG_UPSTREAM:
        try:
            # Helpful when upstream rejects tool schemas.
            tools_preview = json.dumps(openai_payload.get("tools", [])[:5], ensure_ascii=False)[:4000]
            logger.debug("OUT tools preview(first5)=%s", tools_preview)

            grep_tool = None
            for t in openai_payload.get("tools", []) or []:
                try:
                    fn = (t.get("function") or {}) if isinstance(t, dict) else {}
                    name = fn.get("name") if isinstance(fn, dict) else None
                    if name == "Grep":
                        grep_tool = t
                        break
                except Exception:
                    continue
            if grep_tool is not None:
                logger.debug("OUT Grep tool=%s", json.dumps(grep_tool, ensure_ascii=False)[:12000])
        except Exception:
            pass

    headers = {
        "Authorization": f"Bearer {OPENAI_ACCESS_TOKEN}",
        "Accept": "text/event-stream",
    }
    url = f"{MODELSCOPE_BASE_URL}/chat/completions"

    if want_stream:
        allowed = None
        if isinstance(req.get("tools"), list):
            allowed = {t.get("name") for t in req["tools"] if isinstance(t, dict) and t.get("name")}

        async def streaming_gen():
            async with sema:
                async with httpx.AsyncClient(timeout=None) as client:
                    src = openai_stream_to_anthropic_sse_with_retry(
                        client=client,
                        url=url,
                        headers=headers,
                        payload=openai_payload,
                        incoming_model=incoming_model,
                        fallback_model=target_model,
                        allowed_tool_names=allowed,
                    )
                    async for sse in with_sse_keepalive(src, KEEPALIVE_INTERVAL):
                        yield sse

        return StreamingResponse(
            streaming_gen(),
            media_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # Non-streaming
    async with sema:
        async with httpx.AsyncClient(timeout=120) as client:
            openai_payload["stream"] = False
            r = await post_with_retry_json(client, url, headers, openai_payload)
            if r.status_code >= 400:
                logger.error("UPSTREAM non-stream error status=%s body=%s", r.status_code, r.text[:2000])
                return JSONResponse(status_code=r.status_code, content={"error": r.text})

            openai_resp = r.json()
            anthropic_full = openai_to_anthropic_full(
                openai_resp,
                incoming_model=incoming_model,
                fallback_model=target_model,
            )
            return JSONResponse(content=anthropic_full)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "concurrency_limit": CONCURRENCY_LIMIT,
        "min_request_interval": MIN_REQUEST_INTERVAL,
        "max_retries": MAX_RETRIES,
    }
