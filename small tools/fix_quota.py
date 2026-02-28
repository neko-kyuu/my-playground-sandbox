#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import re
from bisect import bisect_right
from pathlib import Path

QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"')

# dataview fenced code block：```dataview ... ```
DATAVIEW_FENCE_RE = re.compile(
    r"(^|\n)```[ \t]*dataview[ \t]*\r?\n.*?\r?\n```[ \t]*(?=\r?\n|$)",
    re.IGNORECASE | re.DOTALL,
)

# YAML frontmatter（仅文件开头）：--- ... --- 或 --- ... ...
YAML_FRONTMATTER_RE = re.compile(
    r"^\ufeff?---[ \t]*\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*(?=\r?\n|$)",
    re.DOTALL,
)

def iter_files(root: Path, exts: set[str]):
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in exts:
                yield p

def line_no_from_pos(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1

def collect_ignore_ranges(text: str):
    ranges = []

    m = YAML_FRONTMATTER_RE.search(text)
    if m:
        ranges.append((m.start(), m.end()))

    for m in DATAVIEW_FENCE_RE.finditer(text):
        ranges.append((m.start(), m.end()))

    ranges.sort()
    merged = []
    for s, e in ranges:
        if not merged or s > merged[-1][1]:
            merged.append([s, e])
        else:
            merged[-1][1] = max(merged[-1][1], e)

    merged = [(s, e) for s, e in merged]
    return merged

def in_ranges(pos: int, ranges):
    starts = [s for s, _ in ranges]
    i = bisect_right(starts, pos) - 1
    return i >= 0 and pos < ranges[i][1]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", help="要扫描的目录")
    ap.add_argument(
        "--ext",
        default=".md,.markdown,.mdx",
        help="要扫描的扩展名，逗号分隔（默认：.md,.markdown,.mdx）",
    )
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    exts = {e.strip().lower() for e in args.ext.split(",") if e.strip()}

    print(f"Scanning directory: {root}")

    for path in iter_files(root, exts):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        ignore_ranges = collect_ignore_ranges(text)

        for m in QUOTED_RE.finditer(text):
            if in_ranges(m.start(), ignore_ranges):
                continue
            ln = line_no_from_pos(text, m.start())
            print(f"{path}:{ln}: {m.group(0)}")

if __name__ == "__main__":
    main()