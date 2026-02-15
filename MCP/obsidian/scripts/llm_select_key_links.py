#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Read the exporter JSON from stdin (or --json file), ask an OpenAI-compatible LLM
to select MOC Key Links, and print STRICT Markdown bullet lines:

- [[Title]] — reason

Dependencies:
  uv pip install openai

Environment variables:
  OPENAI_API_KEY (required)
  OPENAI_MODEL (optional; default: gpt-4o-mini)
  OPENAI_BASE_URL (optional; for OpenAI-compatible providers)
"""

import argparse
import json
import os
import re
import sys
from typing import List, Optional
from dotenv import load_dotenv

try:
    from openai import OpenAI
except ImportError as e:
    raise SystemExit("Missing dependency: openai. Install with: uv pip install openai") from e


SYSTEM = """You are selecting Key Links for an Obsidian Topic MOC.
You MUST follow the output format strictly.
"""

PROMPT_TEMPLATE = r"""
Input: a single JSON object with fields:
- topic: string
- candidates: array of objects, each has:
  - path: string (e.g. "03_Notes/Chunking.md")
  - type: string ("note" | "literature" | "prompt" | "eval" | "moc" | others)
  - status: string ("seedling" | "developing" | "evergreen" | "archived" | "")
  - facets: array of strings
  - summary: string
  - mtime: string (ISO datetime)
- common_facets: array of strings

Task:
Choose Key Links for the Topic MOC: {topic}.

Selection requirements:
1) Output 5–12 items.
2) Prefer rag-friendly, high-signal items:
   - status priority: evergreen > developing > seedling > archived
   - prefer non-empty summaries
   - avoid items whose type is "moc" (do not select MOCs as Key Links)
3) Type diversity (when possible):
   - at least 2 items with type="note"
   - at least 1 item with type="literature"
   - at least 1 item with type in {{"prompt","eval"}} if any such candidates exist
4) Facet coverage & anti-homogeneity:
   - try to cover multiple facets across the set
   - prioritize including items that represent common_facets when relevant
   - avoid picking many items with near-identical facets unless necessary
5) Link formatting:
   - Use the filename (without extension) as the wikilink label: [[Title]]
     Example: "03_Notes/Chunking.md" -> [[Chunking]]
6) Reason formatting:
   - After the wikilink, add " — " then a short reason (3–8 words, in **Chinese**).
   - The reason MUST be derived from the candidate’s summary (paraphrase allowed, but do not invent new claims).
   - If summary is empty, derive reason from facets + type (generic, short).
7) Deterministic tie-breaking:
   - If multiple candidates are similar, prefer more recent mtime.

Output rules (STRICT):
- Output ONLY the Markdown bullet list lines, nothing else.
- Each line must be exactly: "- [[Title]] — reason"
- No headings, no explanations, no code fences, no JSON.

JSON:
{json}
""".strip()


def read_json_input(path: Optional[str]) -> dict:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("No JSON input. Provide --json <file> or pipe JSON via stdin.")
    return json.loads(raw)


def extract_bullets(text: str) -> List[str]:
    lines = [ln.strip() for ln in text.strip().splitlines()]
    bullets = []
    pat = re.compile(r"^- \[\[[^\]]+\]\] — .+")
    for ln in lines:
        if pat.match(ln):
            bullets.append(ln)
    return bullets


def call_llm(client: OpenAI, model: str, topic: str, payload: dict) -> str:
    user = PROMPT_TEMPLATE.format(topic=topic, json=json.dumps(payload, ensure_ascii=False, indent=2))
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def main():
    load_dotenv(dotenv_path="/Users/nekokyuu/vscode/playground-sandbox/MCP/.env")

    ap = argparse.ArgumentParser(description="Select Obsidian MOC Key Links using an LLM (stdin JSON -> bullets).")
    ap.add_argument("--json", default="", help="Path to exporter JSON. If omitted, read from stdin.")
    ap.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), help="LLM model name.")
    ap.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", ""), help="Optional OpenAI-compatible base URL.")
    ap.add_argument("--min-items", type=int, default=5)
    ap.add_argument("--max-items", type=int, default=12)
    args = ap.parse_args()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required.")

    payload = read_json_input(args.json or None)
    topic = payload.get("topic") or "UNKNOWN"

    client_kwargs = {"api_key": api_key}
    if args.base_url:
        client_kwargs["base_url"] = args.base_url
    client = OpenAI(**client_kwargs)

    # First attempt
    txt = call_llm(client, args.model, topic, payload)
    bullets = extract_bullets(txt)

    # If format violated, do one correction attempt
    if len(bullets) < args.min_items:
        correction = {
            "error": "FORMAT_VIOLATION",
            "required_format": '- [[Title]] — reason (one per line, 5–12 lines, no extra text)',
            "previous_output": txt,
        }
        payload2 = dict(payload)
        payload2["_correction"] = correction
        txt2 = call_llm(client, args.model, topic, payload2)
        bullets = extract_bullets(txt2)

    # Final sanitation: enforce min/max by truncation (never add invented lines)
    if len(bullets) > args.max_items:
        bullets = bullets[: args.max_items]

    # Print only bullets (or nothing if total failure)
    sys.stdout.write("\n".join(bullets).rstrip() + ("\n" if bullets else ""))


if __name__ == "__main__":
    main()