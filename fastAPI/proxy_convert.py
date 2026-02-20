import json
import uuid
from typing import Any, Dict, List, Optional


def _blocks_to_text(content_blocks: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for b in content_blocks:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "".join(parts).strip()


def anthropic_to_openai_messages(req: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Convert Anthropic Messages API-style payload into OpenAI Chat Completions messages.

    Notes:
    - Drops Claude "thinking" blocks to avoid upstream incompatibilities.
    - Converts Anthropic tool_use/tool_result into OpenAI tool_calls/tool messages.
    """
    out: List[Dict[str, Any]] = []

    system = req.get("system")
    if system:
        if isinstance(system, str):
            sys_text = system
        elif isinstance(system, list):
            sys_text = _blocks_to_text(system)
        else:
            sys_text = str(system)
        if sys_text:
            out.append({"role": "system", "content": sys_text})

    for m in req.get("messages", []):
        role = m.get("role")
        content = m.get("content")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if isinstance(content, list):
            for b in content:
                btype = b.get("type")

                if btype == "text":
                    text = b.get("text", "")
                    if text:
                        out.append({"role": role, "content": text})

                elif btype == "thinking":
                    # Drop thinking blocks.
                    pass

                elif btype == "tool_use":
                    tool_call_id = b.get("id") or f"call_{uuid.uuid4().hex}"
                    name = b.get("name")
                    tool_input = b.get("input", {}) or {}
                    out.append(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(tool_input, ensure_ascii=False),
                                    },
                                }
                            ],
                        }
                    )

                elif btype == "tool_result":
                    tool_use_id = b.get("tool_use_id")
                    tool_content = b.get("content", "")
                    if isinstance(tool_content, list):
                        tool_content = _blocks_to_text(tool_content)
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_use_id,
                            "content": tool_content if isinstance(tool_content, str) else str(tool_content),
                        }
                    )

    return out


def map_finish_reason(fr: Optional[str]) -> str:
    if fr in (None, "stop"):
        return "end_turn"
    if fr in ("tool_calls",):
        return "tool_use"
    if fr in ("length",):
        return "max_tokens"
    return "end_turn"


def openai_to_anthropic_full(
    openai_resp: Dict[str, Any],
    incoming_model: str,
    fallback_model: str,
) -> Dict[str, Any]:
    choice = (openai_resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")

    blocks: List[Dict[str, Any]] = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})

    tool_calls = msg.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        args = fn.get("arguments") or "{}"
        try:
            parsed_args = json.loads(args)
        except Exception:
            parsed_args = {"_raw": args}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"call_{uuid.uuid4().hex}",
                "name": fn.get("name"),
                "input": parsed_args,
            }
        )

    usage = openai_resp.get("usage") or {}
    stop_reason = map_finish_reason(finish_reason)
    # Some upstreams return finish_reason="stop" even when they emitted tool_calls.
    # Claude Code relies on stop_reason="tool_use" to actually execute the tool call.
    if tool_calls:
        stop_reason = "tool_use"
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": incoming_model or fallback_model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def anthropic_sse(event_obj: Dict[str, Any]) -> str:
    evt = event_obj.get("type", "message")
    payload = json.dumps(event_obj, ensure_ascii=False)
    return f"event: {evt}\\ndata: {payload}\\n\\n"
