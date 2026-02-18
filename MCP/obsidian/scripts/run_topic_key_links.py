#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Run export_topic_candidates + llm_select_key_links in one script."""

import argparse
import json
import os
import sys

from dotenv import load_dotenv

from export_topic_candidates import export_topic
from llm_select_key_links import DEFAULT_DOTENV_PATH, select_key_links


DEFAULT_VAULT_ROOT = "/Users/nekokyuu/genAI/genAI"


def write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_lines(path: str, lines) -> None:
    content = "\n".join(lines).rstrip()
    with open(path, "w", encoding="utf-8") as f:
        if content:
            f.write(content + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="One-shot pipeline: export topic candidates and select key links.")
    ap.add_argument("--topic", required=True, help="Topic name, e.g. RAG")
    ap.add_argument("--vault-root", default=DEFAULT_VAULT_ROOT, help="Obsidian vault root path")
    ap.add_argument("--dotenv", default=DEFAULT_DOTENV_PATH, help=".env file path for OpenAI credentials")
    ap.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), help="LLM model name")
    ap.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", ""), help="Optional OpenAI-compatible base URL")
    ap.add_argument("--min-items", type=int, default=5)
    ap.add_argument("--max-items", type=int, default=12)
    ap.add_argument("--json-out", default="", help="Optional path to save exporter JSON")
    ap.add_argument("--out", default="", help="Optional path to save key links Markdown")
    args = ap.parse_args()

    load_dotenv(dotenv_path=args.dotenv)

    payload = export_topic(args.vault_root, args.topic)
    if args.json_out:
        write_json(args.json_out, payload)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required.")

    bullets = select_key_links(
        payload,
        model=args.model,
        api_key=api_key,
        base_url=args.base_url,
        min_items=args.min_items,
        max_items=args.max_items,
    )

    if args.out:
        write_lines(args.out, bullets)
        return

    sys.stdout.write("\n".join(bullets).rstrip() + ("\n" if bullets else ""))


if __name__ == "__main__":
    main()
