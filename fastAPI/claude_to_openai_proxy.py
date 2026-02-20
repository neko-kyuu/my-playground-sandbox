"""
Entrypoint for uvicorn.

Keep this file minimal so the proxy logic can live in smaller, testable modules.

Run:
  uvicorn claude_to_openai_proxy:app --host 127.0.0.1 --port 8045
"""

from proxy_app import app  # noqa: F401

