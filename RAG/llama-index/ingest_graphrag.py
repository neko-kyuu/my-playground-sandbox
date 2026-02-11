# ingest_graphrag.py
from __future__ import annotations

import os
import re
import json
import argparse
import hashlib
from typing import Dict, Any, Optional, List, Set

from dotenv import load_dotenv
import chromadb

from llama_index.core import VectorStoreIndex, SimpleDirectoryReader
from llama_index.core import StorageContext
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.openai_like import OpenAILikeEmbedding


# -------- ObsidianReader（可选）--------
def load_obsidian_docs(vault_path: str):
    """
    优先使用 ObsidianReader；没有安装则回退到 SimpleDirectoryReader。
    """
    try:
        from llama_index.readers.obsidian import ObsidianReader  # pip install llama-index-readers-obsidian
        return ObsidianReader(input_dir=vault_path, recursive=True).load_data()
    except Exception:
        return SimpleDirectoryReader(vault_path, recursive=True, filename_as_id=True).load_data()


# -------- 通用工具 --------
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
TAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_\-/]+)")  # 简易 tag 规则

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def read_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json(path: str, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def normalize_note_key(s: str) -> str:
    # 用于 link 解析的 key：不区分大小写 + 去掉多余空白
    return s.strip().lower()

def extract_wikilinks(text: str) -> List[str]:
    out = []
    for m in WIKILINK_RE.findall(text or ""):
        # [[note|alias]] -> note
        target = m.split("|", 1)[0].strip()
        if target:
            out.append(target)
    return out

def extract_tags(text: str) -> List[str]:
    return sorted(set(TAG_RE.findall(text or "")))

def get_doc_source(doc) -> str:
    meta = doc.metadata or {}
    file_path = meta.get("file_path") or meta.get("filepath") or meta.get("path")
    if file_path:
        return os.path.abspath(file_path)
    # fallback：用 doc_id 当 source（不完美，但能跑）
    return os.path.abspath(doc.doc_id or "unknown")

def get_doc_title(source_path: str) -> str:
    base = os.path.basename(source_path)
    if base.lower().endswith(".md"):
        base = base[:-3]
    return base

def chroma_get_one_meta(collection, source: str) -> Optional[Dict[str, Any]]:
    got = collection.get(where={"source": source}, include=["metadatas"], limit=1)
    metas = got.get("metadatas") or []
    return metas[0] if metas else None


# -------- 图谱：JSON 结构 --------
# {
#   "nodes": { "<source>": {"title": "...", "file_hash": "...", "tags": [...]} },
#   "edges": { "<source>": ["<neighbor_source>", ...] }
# }
def build_note_index(sources: List[str], vault_path: str) -> Dict[str, str]:
    """
    把各种可能的 wikilink 形式映射到绝对路径：
    - [[Note]] -> Note.md
    - [[folder/Note]] -> vault/folder/Note.md
    """
    idx: Dict[str, str] = {}

    # 1) 用实际存在的文件构建：filename stem -> source
    for src in sources:
        title = get_doc_title(src)
        idx[normalize_note_key(title)] = src

        # 也收录相对 vault 的路径（不带 .md）
        try:
            rel = os.path.relpath(src, vault_path)
            rel_noext = rel[:-3] if rel.lower().endswith(".md") else rel
            idx[normalize_note_key(rel_noext.replace("\\", "/"))] = src
        except Exception:
            pass

    return idx

def resolve_wikilink(link: str, note_index: Dict[str, str], vault_path: str) -> Optional[str]:
    """
    尝试把 [[xxx]] 解析为 vault 内某个 md 的绝对路径。
    """
    raw = link.strip()
    if not raw:
        return None

    # 1) 直接命中索引（Note / folder/Note）
    k = normalize_note_key(raw.replace("\\", "/"))
    if k in note_index:
        return note_index[k]

    # 2) 如果写了 .md
    if raw.lower().endswith(".md"):
        k2 = normalize_note_key(raw[:-3].replace("\\", "/"))
        if k2 in note_index:
            return note_index[k2]

    # 3) 兜底：当成相对 vault 的路径试一下
    cand = os.path.abspath(os.path.join(vault_path, raw))
    if os.path.exists(cand) and cand.lower().endswith(".md"):
        return cand
    if os.path.exists(cand + ".md"):
        return os.path.abspath(cand + ".md")

    return None


def main():
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default=os.getenv("VAULT_PATH", "./markdown-notes"))
    parser.add_argument("--db", default=os.getenv("GRAPH_DB_PATH", "./llama_chroma_db"))
    parser.add_argument("--collection", default=os.getenv("CHROMA_COLLECTION", "quickstart"))
    parser.add_argument("--graph", default=os.getenv("GRAPH_PATH", "./graphrag/obsidian_graph.json"))
    parser.add_argument("--reset", action="store_true", help="删除并重建 collection + graph")
    parser.add_argument("--prune", action="store_true", help="删除库/图里已不存在于 vault 的文件")
    args = parser.parse_args()

    DMX_API_KEY = os.getenv("DMX_API_KEY")
    DMX_BASE_URL = os.getenv("DMX_BASE_URL", "https://www.dmxapi.cn/v1/")
    DMX_EMBEDDING_MODEL = os.getenv("DMX_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")

    if not DMX_API_KEY:
        raise ValueError("Missing DMX_API_KEY")

    # ---- Chroma ----
    chroma_client = chromadb.PersistentClient(path=args.db)
    if args.reset:
        try:
            chroma_client.delete_collection(args.collection)
        except Exception:
            pass
    chroma_collection = chroma_client.get_or_create_collection(args.collection)

    # ---- graph ----
    if args.reset and os.path.exists(args.graph):
        os.remove(args.graph)
    graph = read_json(args.graph, default={"nodes": {}, "edges": {}})

    # ---- Embedding ----
    embed_model = OpenAILikeEmbedding(
        model_name=DMX_EMBEDDING_MODEL,
        api_base=DMX_BASE_URL,
        api_key=DMX_API_KEY,
        embed_batch_size=int(os.getenv("EMBED_BATCH_SIZE", "10")),
    )

    # ---- Load docs from vault ----
    documents = load_obsidian_docs(args.vault)

    # 规范 source / hash / title 写回 metadata（供 Chroma filter + 图谱使用）
    current_sources: Set[str] = set()
    source_to_doc = {}
    to_index = []

    for doc in documents:
        source = get_doc_source(doc)
        current_sources.add(source)
        source_to_doc[source] = doc

        title = get_doc_title(source)

        try:
            file_hash = sha256_file(source)
        except Exception:
            file_hash = hashlib.sha256((doc.text or "").encode("utf-8")).hexdigest()

        meta = dict(doc.metadata or {})
        meta.update(
            {
                "source": source,
                "title": title,
                "file_hash": file_hash,
            }
        )
        doc.metadata = meta

        old_meta = chroma_get_one_meta(chroma_collection, source)
        old_hash = (old_meta or {}).get("file_hash")

        if old_meta is None:
            to_index.append(doc)
        elif old_hash != file_hash:
            chroma_collection.delete(where={"source": source})
            to_index.append(doc)

    # prune：删除 vault 中不存在的 source
    if args.prune:
        # 1) prune chroma（用 graph.nodes 里的 source 来做轻量遍历）
        known_sources = set(graph.get("nodes", {}).keys())
        removed = sorted(known_sources - current_sources)
        for s in removed:
            chroma_collection.delete(where={"source": s})
            graph["nodes"].pop(s, None)
            graph["edges"].pop(s, None)
        # 同时把所有 edges 里指向 removed 的也移除
        removed_set = set(removed)
        for s, nbrs in list(graph.get("edges", {}).items()):
            graph["edges"][s] = [x for x in (nbrs or []) if x not in removed_set]

    # ---- 写入向量库（仅变更部分）----
    if to_index:
        vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
        storage_context = StorageContext.from_defaults(vector_store=vector_store)
        _ = VectorStoreIndex.from_documents(
            to_index,
            storage_context=storage_context,
            embed_model=embed_model,
            show_progress=True,
        )

    # ---- 构建/更新图谱（仅变更部分也行；这里简单：对所有 current_sources 重算边，轻量且稳）----
    note_index = build_note_index(list(current_sources), args.vault)

    new_nodes = {}
    new_edges: Dict[str, List[str]] = {}

    for source in current_sources:
        doc = source_to_doc.get(source)
        if not doc:
            continue
        meta = doc.metadata or {}
        file_hash = meta.get("file_hash")
        title = meta.get("title") or get_doc_title(source)

        text = doc.text or ""
        links = extract_wikilinks(text)
        tags = extract_tags(text)

        nbrs = []
        for lk in links:
            resolved = resolve_wikilink(lk, note_index, args.vault)
            if resolved and resolved != source:
                nbrs.append(resolved)

        new_nodes[source] = {"title": title, "file_hash": file_hash, "tags": tags}
        new_edges[source] = sorted(set(nbrs))

    graph["nodes"] = new_nodes
    graph["edges"] = new_edges
    write_json(args.graph, graph)

    print(f"Done.")
    print(f"- collection='{args.collection}' db='{args.db}'")
    print(f"- graph='{args.graph}' nodes={len(graph['nodes'])} edges={sum(len(v) for v in graph['edges'].values())}")
    print(f"- indexed(updated/new) docs={len(to_index)}")


if __name__ == "__main__":
    main()