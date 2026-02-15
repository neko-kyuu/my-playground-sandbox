#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
from datetime import datetime, timezone

try:
    import yaml  # pip install pyyaml
except ImportError as e:
    raise SystemExit("Missing dependency: pyyaml. Install with: pip install pyyaml") from e


SCAN_DIRS = ["02_Literature", "03_Notes", "04_Build"]
FACETS_VOCAB_PATH = os.path.join("05_Navigate", "Facets - Vocabulary.md")


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def parse_frontmatter(md_text: str):
    """
    Parse YAML frontmatter between the first pair of --- ... --- at file start.
    Returns dict or None.
    """
    if not md_text.startswith("---"):
        return None

    # Find the second --- delimiter
    # frontmatter must start at beginning
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", md_text, flags=re.DOTALL)
    if not m:
        return None

    fm_text = m.group(1)
    try:
        data = yaml.safe_load(fm_text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def iso_mtime(path: str) -> str:
    ts = os.path.getmtime(path)
    # local time is OK for your use; if you want UTC, change here
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def normalize_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return [i for i in x if isinstance(i, (str, int, float, bool)) or i is None] and [str(i) for i in x if i is not None]
    if isinstance(x, str):
        return [x]
    return []


def rag_included(fm: dict) -> bool:
    rag = fm.get("rag")
    if isinstance(rag, dict):
        inc = rag.get("include")
        # default true for non-inbox; but here we only scan non-inbox dirs anyway
        return False if inc is False else True
    return True


def candidate_from_file(vault_root: str, rel_path: str, fm: dict):
    abs_path = os.path.join(vault_root, rel_path)
    return {
        "path": rel_path.replace("\\", "/"),
        "type": fm.get("type", ""),
        "status": fm.get("status", ""),
        "facets": normalize_list(fm.get("facets")),
        "summary": fm.get("summary", "") or "",
        "mtime": iso_mtime(abs_path),
    }


def extract_common_facets(vault_root: str):
    abs_path = os.path.join(vault_root, FACETS_VOCAB_PATH)
    if not os.path.exists(abs_path):
        return []

    text = read_text(abs_path)

    # Find section "## Common facets" and read bullet lines until next heading "## "
    m = re.search(r"^##\s+Common facets.*?\n(.*?)(?=^\s*##\s+|\Z)", text, flags=re.DOTALL | re.MULTILINE)
    if not m:
        return []

    block = m.group(1)
    facets = []
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        # bullet like "- embeddings"
        if line.startswith("- "):
            val = line[2:].strip()
            if val:
                facets.append(val)
    # de-dup preserve order
    seen = set()
    out = []
    for f in facets:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def walk_md_files(root_dir: str):
    for dirpath, _, filenames in os.walk(root_dir):
        for fn in filenames:
            if fn.lower().endswith(".md"):
                yield os.path.join(dirpath, fn)


def export_topic(vault_root: str, topic: str):
    candidates = []
    for d in SCAN_DIRS:
        abs_dir = os.path.join(vault_root, d)
        if not os.path.isdir(abs_dir):
            continue

        for abs_path in walk_md_files(abs_dir):
            rel_path = os.path.relpath(abs_path, vault_root)
            text = read_text(abs_path)
            fm = parse_frontmatter(text)
            if not fm:
                continue

            if not rag_included(fm):
                continue

            topics = normalize_list(fm.get("topics"))
            if topic not in topics:
                continue

            candidates.append(candidate_from_file(vault_root, rel_path, fm))

    common_facets = extract_common_facets(vault_root)

    return {
        "topic": topic,
        "candidates": sorted(candidates, key=lambda x: (x.get("mtime", ""), x.get("path", "")), reverse=True),
        "common_facets": common_facets,
    }


def main():
    ap = argparse.ArgumentParser(description="Export per-topic candidate list for MOC Key Links selection.")
    ap.add_argument("--topic", required=True, help="Topic name, e.g., RAG")
    ap.add_argument("--out", default="", help="Output file path (json). If omitted, prints to stdout.")
    args = ap.parse_args()

    vault_root = "/Users/nekokyuu/genAI/genAI"
    payload = export_topic(vault_root, args.topic)

    s = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(s + "\n")
    else:
        print(s)


if __name__ == "__main__":
    main()