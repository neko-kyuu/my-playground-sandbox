import json
import os
import re
from typing import Any, Dict

from dotenv import load_dotenv


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env located next to this code (works no matter where uvicorn is launched from).
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _get_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) == "1"


def _get_json(name: str, default: str = "{}") -> Dict[str, Any]:
    raw = os.getenv(name, default) or default
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


MODELSCOPE_BASE_URL = os.getenv("MODELSCOPE_BASE_URL", "https://api-inference.modelscope.cn/v1").rstrip("/")
OPENAI_ACCESS_TOKEN = os.getenv("OPENAI_ACCESS_TOKEN", "")
DEFAULT_OPENAI_MODEL = os.getenv("DEFAULT_OPENAI_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct")
DEBUG_UPSTREAM = _get_bool("DEBUG_UPSTREAM", "0")

MODEL_MAP = _get_json("MODEL_MAP", "{}")

CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "2"))
MIN_REQUEST_INTERVAL = float(os.getenv("MIN_REQUEST_INTERVAL", "1"))  # seconds

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "6"))
BACKOFF_INITIAL = float(os.getenv("BACKOFF_INITIAL", "0.8"))
BACKOFF_MAX = float(os.getenv("BACKOFF_MAX", "20"))

STREAM_PREFETCH_CHARS = int(os.getenv("STREAM_PREFETCH_CHARS", "200"))
STREAM_PREFETCH_SECONDS = float(os.getenv("STREAM_PREFETCH_SECONDS", "0.5"))
DROP_UNKNOWN_TOOL_CALLS = _get_bool("DROP_UNKNOWN_TOOL_CALLS", "1")

KEEPALIVE_INTERVAL = float(os.getenv("KEEPALIVE_INTERVAL", "10"))
EAGER_MESSAGE_START = _get_bool("EAGER_MESSAGE_START", "0")

# Streaming reliability knobs (some upstreams reset SSE connections frequently).
STREAM_READERROR_FALLBACK_TO_NON_STREAM = _get_bool("STREAM_READERROR_FALLBACK_TO_NON_STREAM", "1")
STREAM_READERROR_FALLBACK_AFTER = int(os.getenv("STREAM_READERROR_FALLBACK_AFTER", "1"))

UPSTREAM_TOOLS_STYLE = (os.getenv("UPSTREAM_TOOLS_STYLE", "openai") or "openai").split("#", 1)[0].strip()
DROP_NON_IDENTIFIER_TOOL_PROPS = _get_bool("DROP_NON_IDENTIFIER_TOOL_PROPS", "1")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
