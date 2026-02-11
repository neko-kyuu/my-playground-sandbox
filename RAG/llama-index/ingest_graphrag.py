# ingest_graphrag.py
from __future__ import annotations

import os
import re
import json
import argparse
import hashlib
from typing import Dict, Any, Optional, List, Set, Tuple

from dotenv import load_dotenv
import chromadb

from llama_index.core import VectorStoreIndex, SimpleDirectoryReader
from llama_index.core import StorageContext
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.openai_like import OpenAILikeEmbedding

try:
    from llama_index.core.node_parser import MarkdownNodeParser
except Exception:
    MarkdownNodeParser = None


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
PIPELINE_VERSION = "graphrag-v3-obsidian-clean-fm-filter"

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
TAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_\-/]+)")  # 简易 tag 规则

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
DATAVIEW_BLOCK_RE = re.compile(r"```(?:dataview|dataviewjs)\b[\s\S]*?```", re.IGNORECASE)
EMBED_WIKILINK_RE = re.compile(r"!\[\[([^\]]+)\]\]")
WIKILINK_ALIAS_RE = re.compile(r"(?<!!)\[\[[^\]|]+\|([^\]]+)\]\]")
WIKILINK_SIMPLE_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
CALLOUT_RE = re.compile(r"(?m)^\s{0,3}>\s*\[![^\]]+\][+-]?\s*")


def parse_scalar_token(token: str):
    v = token.strip()
    if not v:
        return ""
    if (v.startswith('\"') and v.endswith('\"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"[+-]?\d+", v):
        try:
            return int(v)
        except Exception:
            return v
    if re.fullmatch(r"[+-]?\d+\.\d+", v):
        try:
            return float(v)
        except Exception:
            return v
    return v


def parse_simple_frontmatter(block: str) -> Dict[str, Any]:
    """
    轻量 YAML 解析器（仅支持常见 top-level 字段），避免引入额外依赖。
    """
    data: Dict[str, Any] = {}
    current_list_key: Optional[str] = None

    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        stripped = line.lstrip()
        if stripped.startswith("- ") and current_list_key is not None:
            item = parse_scalar_token(stripped[2:])
            existing = data.get(current_list_key)
            if not isinstance(existing, list):
                existing = []
            existing.append(item)
            data[current_list_key] = existing
            continue

        if ":" not in line:
            current_list_key = None
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            current_list_key = None
            continue

        if not value:
            data[key] = []
            current_list_key = key
            continue

        if value.startswith("[") and value.endswith("]"):
            inside = value[1:-1].strip()
            if inside:
                items = [parse_scalar_token(x) for x in inside.split(",") if x.strip()]
            else:
                items = []
            data[key] = items
        else:
            data[key] = parse_scalar_token(value)
        current_list_key = None

    return data


def split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    m = FRONTMATTER_RE.match(text or "")
    if not m:
        return {}, text or ""
    fm_block = m.group(1)
    body = (text or "")[m.end():]
    return parse_simple_frontmatter(fm_block), body


def normalize_tags(values: Any) -> List[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]

    tags: List[str] = []
    for x in values:
        s = str(x).strip()
        if not s:
            continue
        if s.startswith("#"):
            s = s[1:]
        s = s.lower()
        tags.append(s)
    return sorted(set(tags))


def extract_frontmatter_tags(frontmatter: Dict[str, Any]) -> List[str]:
    tags = []
    for k in ("tags", "tag"):
        if k in frontmatter:
            tags.extend(normalize_tags(frontmatter.get(k)))
    return sorted(set(tags))


def clean_obsidian_text(text: str) -> str:
    s = text or ""

    # 移除 Dataview 查询块，避免无语义噪音进入 embedding。
    s = DATAVIEW_BLOCK_RE.sub("\n", s)
    # 移除 Obsidian 嵌入引用 ![[...]]。
    s = EMBED_WIKILINK_RE.sub("", s)

    # [[note|alias]] -> alias
    s = WIKILINK_ALIAS_RE.sub(lambda m: m.group(1).strip(), s)

    # [[note#section]] -> note（锚点对 embedding 通常是噪音）
    def _replace_plain_wikilink(m: re.Match) -> str:
        raw = m.group(1).strip()
        target = raw.split("|", 1)[0].strip()
        target = target.split("#", 1)[0].split("^", 1)[0].strip()
        return target or raw

    s = WIKILINK_SIMPLE_RE.sub(_replace_plain_wikilink, s)

    # Callout 标记（[!note]）去壳，保留正文。
    s = CALLOUT_RE.sub("", s)

    # 收敛空行，避免 chunk 中无意义换行过多。
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


def sanitize_metadata_value(value: Any) -> Optional[Any]:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        normalized = [str(v).strip() for v in value if str(v).strip()]
        return "|".join(normalized) if normalized else None
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def normalize_frontmatter_key(key: Any) -> str:
    raw = str(key).strip().lower()
    if not raw:
        return ""
    raw = re.sub(r"\s+", "_", raw)
    raw = re.sub(r"[^\w]+", "_", raw, flags=re.UNICODE)
    return raw.strip("_")


def merge_frontmatter_metadata(meta: Dict[str, Any], frontmatter: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(meta)
    for key, value in (frontmatter or {}).items():
        normalized_key = normalize_frontmatter_key(key)
        if not normalized_key or normalized_key in {"tag", "tags"}:
            continue
        out[f"fm_{normalized_key}"] = sanitize_metadata_value(value)
    return out


def set_doc_text(doc, value: str) -> None:
    """
    兼容不同版本的 Document：新版本 text 是只读属性，需要 set_content()。
    """
    if hasattr(doc, "set_content"):
        doc.set_content(value)
        return
    doc.text = value


def build_ingest_transformations() -> Optional[List[Any]]:
    if MarkdownNodeParser is None:
        return None
    try:
        return [MarkdownNodeParser()]
    except Exception:
        return None

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
    return sorted(set(x.lower() for x in TAG_RE.findall(text or "")))

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


def iter_all_sources(collection, batch_size: int = 2000) -> Set[str]:
    offset = 0
    sources: Set[str] = set()

    while True:
        got = collection.get(include=["metadatas"], limit=batch_size, offset=offset)
        metas = got.get("metadatas") or []
        if not metas:
            break

        for m in metas:
            if not m:
                continue
            source = m.get("source")
            if source:
                sources.add(source)

        offset += len(metas)
        if len(metas) < batch_size:
            break

    return sources


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
    source_to_raw_text: Dict[str, str] = {}
    source_to_tags: Dict[str, List[str]] = {}
    to_index = []

    for doc in documents:
        source = get_doc_source(doc)
        current_sources.add(source)
        source_to_doc[source] = doc

        title = get_doc_title(source)
        raw_text = doc.text or ""
        source_to_raw_text[source] = raw_text

        frontmatter, body_text = split_frontmatter(raw_text)
        cleaned_text = clean_obsidian_text(body_text)
        if cleaned_text:
            set_doc_text(doc, cleaned_text)
        else:
            set_doc_text(doc, (body_text or "").strip() or raw_text)

        tags = sorted(set(extract_tags(raw_text) + extract_frontmatter_tags(frontmatter)))
        source_to_tags[source] = tags

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
                "pipeline_version": PIPELINE_VERSION,
            }
        )

        if tags:
            meta["tags_csv"] = "|".join(tags)

        meta = merge_frontmatter_metadata(meta, frontmatter)
        sanitized_meta = {}
        for k, v in meta.items():
            sv = sanitize_metadata_value(v)
            if sv is not None:
                sanitized_meta[str(k)] = sv
        doc.metadata = sanitized_meta

        old_meta = chroma_get_one_meta(chroma_collection, source)
        old_hash = (old_meta or {}).get("file_hash")
        old_pipeline = (old_meta or {}).get("pipeline_version")

        if old_meta is None:
            to_index.append(doc)
        elif old_hash != file_hash or old_pipeline != PIPELINE_VERSION:
            chroma_collection.delete(where={"source": source})
            to_index.append(doc)

    # prune：删除 vault 中不存在的 source
    if args.prune:
        # 1) prune chroma（用 graph.nodes 里的 source 来做轻量遍历）
        known_sources = set(graph.get("nodes", {}).keys()) | iter_all_sources(chroma_collection)
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
        ingest_transformations = build_ingest_transformations()
        index_kwargs = {}
        if ingest_transformations:
            index_kwargs["transformations"] = ingest_transformations

        _ = VectorStoreIndex.from_documents(
            to_index,
            storage_context=storage_context,
            embed_model=embed_model,
            show_progress=True,
            **index_kwargs,
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

        text = source_to_raw_text.get(source, "")
        links = extract_wikilinks(text)
        tags = source_to_tags.get(source, [])

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
