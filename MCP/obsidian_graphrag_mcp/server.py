from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple, Literal, Union

import chromadb
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from llama_index.core import VectorStoreIndex
from llama_index.core.retrievers import BaseRetriever
from llama_index.core.schema import NodeWithScore, QueryBundle
from llama_index.core.vector_stores import (
    MetadataFilter,
    MetadataFilters,
    FilterCondition,
    FilterOperator,
)
from llama_index.embeddings.openai_like import OpenAILikeEmbedding
from llama_index.llms.openai_like import OpenAILike
from llama_index.vector_stores.chroma import ChromaVectorStore

from chromadb.config import Settings as ChromaSettings


JsonDict = Dict[str, Any]


def _read_json(path: str, default: Any) -> Any:
    if not path:
        return default
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_frontmatter_key(key: str) -> str:
    raw = str(key).strip().lower()
    if not raw:
        return ""
    raw = "_".join(raw.split())
    out: List[str] = []
    for ch in raw:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out).strip("_")


def _normalize_tags(raw_tags: Optional[List[str]]) -> List[str]:
    tags: List[str] = []
    for raw in raw_tags or []:
        for part in str(raw).split(","):
            t = part.strip()
            if not t:
                continue
            if t.startswith("#"):
                t = t[1:]
            tags.append(t.lower())
    return sorted(set(tags))


JsonScalar = Union[str, int, float, bool, None]
JsonValue = Union[JsonScalar, Dict[str, "JsonValue"], List["JsonValue"]]


def _jsonable(value: Any) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        out: Dict[str, JsonValue] = {}
        for k, v in value.items():
            out[str(k)] = _jsonable(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, set):
        return [_jsonable(v) for v in sorted(value, key=lambda x: str(x))]
    return str(value)


def _build_inbound_edges(graph_edges: Dict[str, List[str]]) -> Dict[str, List[str]]:
    inbound: Dict[str, Set[str]] = {}
    for src, neighbors in (graph_edges or {}).items():
        inbound.setdefault(src, set())
        for nb in neighbors or []:
            inbound.setdefault(nb, set()).add(src)
    return {k: sorted(v) for k, v in inbound.items()}


def _select_sources_by_tags(nodes_meta: Dict[str, Dict[str, Any]], tags: List[str], mode: Literal["any", "all"]) -> Set[str]:
    if not tags:
        return set(nodes_meta.keys())

    tag_set = set(tags)
    matched: Set[str] = set()

    for source, meta in (nodes_meta or {}).items():
        source_tags = set(str(x).lower() for x in ((meta or {}).get("tags") or []))
        if mode == "all":
            ok = tag_set.issubset(source_tags)
        else:
            ok = bool(source_tags & tag_set)
        if ok:
            matched.add(source)

    return matched


def _build_frontmatter_filters(
    frontmatter: Optional[Dict[str, Any]],
    mode: Literal["any", "all"],
) -> Optional[MetadataFilters]:
    if not frontmatter:
        return None

    filters: List[MetadataFilter] = []
    for k, v in frontmatter.items():
        key = _normalize_frontmatter_key(k)
        if not key:
            continue
        if v is None or v == "":
            continue
        filters.append(MetadataFilter(key=f"fm_{key}", value=v, operator=FilterOperator.EQ))

    if not filters:
        return None

    condition = FilterCondition.AND if mode == "all" else FilterCondition.OR
    return MetadataFilters(filters=filters, condition=condition)


def _expand_sources(
    graph_edges: Dict[str, List[str]],
    graph_inbound_edges: Dict[str, List[str]],
    seeds: Set[str],
    hops: int,
    direction: Literal["out", "in", "both"] = "both",
) -> Set[str]:
    if hops <= 0:
        return set(seeds)

    seen = set(seeds)
    q: deque[Tuple[str, int]] = deque([(s, 0) for s in seeds])

    while q:
        s, d = q.popleft()
        if d >= hops:
            continue

        out_nbrs = graph_edges.get(s, []) or []
        in_nbrs = graph_inbound_edges.get(s, []) or []

        if direction == "out":
            candidates = out_nbrs
        elif direction == "in":
            candidates = in_nbrs
        else:
            candidates = list(out_nbrs) + list(in_nbrs)

        for nb in candidates:
            if nb not in seen:
                seen.add(nb)
                q.append((nb, d + 1))

    return seen


class ObsidianGraphRAGRetriever(BaseRetriever):
    def __init__(
        self,
        index: VectorStoreIndex,
        graph: Dict[str, Any],
        top_k: int = 5,
        hops: int = 1,
        per_source_k: int = 2,
        neighbor_boost: float = 0.85,
        max_sources: int = 10,
        final_top_k: int = 15,
        max_seed_sources: int = 5,
        direction: Literal["out", "in", "both"] = "both",
        allowed_sources: Optional[Set[str]] = None,
        metadata_filters: Optional[MetadataFilters] = None,
    ):
        super().__init__()
        self._index = index
        self._edges = (graph or {}).get("edges", {}) or {}
        self._inbound_edges = _build_inbound_edges(self._edges)
        self._top_k = int(top_k)
        self._hops = int(hops)
        self._per_source_k = int(per_source_k)
        self._neighbor_boost = float(neighbor_boost)
        self._max_sources = int(max_sources)
        self._final_top_k = int(final_top_k)
        self._max_seed_sources = int(max_seed_sources)
        self._direction = direction
        self._allowed_sources = set(allowed_sources) if allowed_sources is not None else None
        self._metadata_filters = metadata_filters

    def _get_embed_model(self):
        em = getattr(self._index, "_embed_model", None)
        if em is not None:
            return em
        sc = getattr(self._index, "_service_context", None)
        if sc is not None and getattr(sc, "embed_model", None) is not None:
            return sc.embed_model
        raise RuntimeError("Cannot find embed_model on index; check llama-index version.")

    def _retrieve(self, query_bundle) -> List[NodeWithScore]:
        if isinstance(query_bundle, str):
            query_bundle = QueryBundle(query_str=query_bundle)

        query_str = query_bundle.query_str
        if query_bundle.embedding is None:
            embed_model = self._get_embed_model()
            query_bundle.embedding = embed_model.get_query_embedding(query_str)

        primary_top_k = self._top_k
        if self._allowed_sources is not None:
            primary_top_k = max(self._top_k * 4, self._top_k)

        base_retriever = self._index.as_retriever(
            similarity_top_k=primary_top_k,
            filters=self._metadata_filters,
        )
        primary = base_retriever.retrieve(query_bundle)
        if self._allowed_sources is not None:
            primary = [
                r
                for r in primary
                if (r.node.metadata or {}).get("source") in self._allowed_sources
            ]

        best_seed_score: Dict[str, float] = {}
        for r in primary:
            s = (r.node.metadata or {}).get("source")
            if not s:
                continue
            if self._allowed_sources is not None and s not in self._allowed_sources:
                continue
            sc = float(r.score or 0.0)
            best_seed_score[s] = max(best_seed_score.get(s, 0.0), sc)

        seed_sources_sorted = sorted(best_seed_score.keys(), key=lambda s: best_seed_score[s], reverse=True)
        seed_sources_sorted = seed_sources_sorted[: self._max_seed_sources]
        seed_set = set(seed_sources_sorted)

        expanded_set = _expand_sources(
            graph_edges=self._edges,
            graph_inbound_edges=self._inbound_edges,
            seeds=seed_set,
            hops=self._hops,
            direction=self._direction,
        )

        if self._allowed_sources is not None:
            expanded_set &= self._allowed_sources

        if not seed_sources_sorted and self._allowed_sources:
            ordered_sources = sorted(self._allowed_sources)
            if self._max_sources > 0:
                ordered_sources = ordered_sources[: self._max_sources]
        else:
            neighbor_sources = sorted([s for s in expanded_set if s not in seed_set])
            ordered_sources = seed_sources_sorted + neighbor_sources
            if self._max_sources > 0:
                ordered_sources = ordered_sources[: self._max_sources]

        secondary: List[NodeWithScore] = []
        for s in ordered_sources:
            source_filter = MetadataFilter(key="source", value=s, operator=FilterOperator.EQ)
            filters = MetadataFilters(filters=[source_filter])
            if self._metadata_filters is not None:
                filters = MetadataFilters(filters=[self._metadata_filters, filters], condition=FilterCondition.AND)

            r = self._index.as_retriever(similarity_top_k=self._per_source_k, filters=filters)
            got = r.retrieve(query_bundle)

            if s not in seed_set:
                for x in got:
                    if x.score is not None:
                        x.score *= self._neighbor_boost
            secondary.extend(got)

        best: Dict[str, NodeWithScore] = {}
        for item in (primary + secondary):
            nid = item.node.node_id
            if nid not in best or (item.score or 0) > (best[nid].score or 0):
                best[nid] = item

        merged = sorted(best.values(), key=lambda x: (x.score or 0), reverse=True)
        return merged[: self._final_top_k]


def _llm_complete_text(llm: Any, prompt: str) -> str:
    if hasattr(llm, "complete"):
        resp = llm.complete(prompt)
        if hasattr(resp, "text"):
            return resp.text
        return str(resp)

    if hasattr(llm, "chat"):
        from llama_index.core.llms import ChatMessage  # type: ignore

        resp = llm.chat([ChatMessage(role="user", content=prompt)])
        msg = getattr(resp, "message", None)
        if msg is not None and getattr(msg, "content", None) is not None:
            return msg.content
        return str(resp)

    raise RuntimeError("Unsupported LLM object: missing complete()/chat().")


@dataclass
class DmxConfig:
    base_url: str
    api_key: str
    embedding_model: str
    chat_model: Optional[str]


@dataclass
class DefaultsConfig:
    top_k: int = 5
    hops: int = 1
    per_source_k: int = 2
    direction: Literal["out", "in", "both"] = "both"
    neighbor_boost: float = 0.85
    max_sources: int = 10
    final_top_k: int = 15
    max_seed_sources: int = 5
    max_results: int = 15
    text_chars: int = 1200
    context_k: int = 8


@dataclass
class AppConfig:
    dotenv_path: Optional[str]
    db_path: str
    collection: str
    graph_path: str
    dmx: DmxConfig
    defaults: DefaultsConfig


@dataclass
class AppState:
    cfg: AppConfig
    graph: JsonDict
    graph_nodes: Dict[str, Dict[str, Any]]
    index: VectorStoreIndex
    embed_model: OpenAILikeEmbedding
    llm: Optional[OpenAILike]


_STATE: Optional[AppState] = None


def _resolve_cfg_value(obj: JsonDict, key: str, env_key: str) -> Optional[str]:
    direct = str(obj.get(key) or "").strip()
    if direct:
        return direct
    env_name = str(obj.get(env_key) or "").strip()
    if env_name:
        v = str(os.getenv(env_name, "")).strip()
        if v:
            return v
    return None


def _redact_secret(value: str) -> str:
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= 10:
        return "***"
    return f"{s[:4]}…{s[-4:]}"


def _looks_like_api_key(value: str) -> bool:
    s = str(value or "").strip()
    if not s:
        return False
    if s.startswith("sk-") and len(s) >= 20:
        return True
    # Heuristic: very long token-ish string (user may paste provider key)
    if len(s) >= 30 and any(ch.isdigit() for ch in s) and any(ch.isalpha() for ch in s):
        return True
    return False


def _looks_like_model_name(value: str) -> bool:
    s = str(value or "").strip()
    if not s:
        return False
    # Common: "org/model", "Qwen/Qwen3-Embedding-8B"
    if "/" in s and " " not in s and len(s) >= 5:
        return True
    # Also allow plain model IDs like "gpt-4.1-mini"
    if "-" in s and " " not in s and len(s) >= 5:
        return True
    return False


def _load_config(config_path: str) -> AppConfig:
    raw: JsonDict = _read_json(config_path, default={}) or {}

    dotenv_path = str(raw.get("dotenv_path") or "").strip() or None
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path)

    db_path = str(raw.get("db_path") or "").strip()
    collection = str(raw.get("collection") or "quickstart").strip()
    graph_path = str(raw.get("graph_path") or "").strip()
    if not db_path:
        raise ValueError("Missing config.db_path")
    if not graph_path:
        raise ValueError("Missing config.graph_path")

    dmx_raw: JsonDict = (raw.get("dmx") or {}) if isinstance(raw.get("dmx"), dict) else {}
    base_url = str(dmx_raw.get("base_url") or os.getenv("DMX_BASE_URL") or "https://www.dmxapi.cn/v1/").strip()
    api_key = _resolve_cfg_value(dmx_raw, "api_key", "api_key_env") or str(os.getenv("DMX_API_KEY") or "").strip()
    embedding_model = _resolve_cfg_value(dmx_raw, "embedding_model", "embedding_model_env") or str(os.getenv("DMX_EMBEDDING_MODEL") or "").strip()
    chat_model = _resolve_cfg_value(dmx_raw, "chat_model", "chat_model_env") or str(os.getenv("DMX_CHAT_MODEL") or "").strip() or None

    api_key_env = str(dmx_raw.get("api_key_env") or "").strip()
    embedding_model_env = str(dmx_raw.get("embedding_model_env") or "").strip()
    chat_model_env = str(dmx_raw.get("chat_model_env") or "").strip()

    if not api_key:
        if api_key_env and _looks_like_api_key(api_key_env):
            raise ValueError(
                "Missing DMX api key. It looks like you put a literal key into config.dmx.api_key_env "
                f"({_redact_secret(api_key_env)}). Use config.dmx.api_key for literal values, "
                "or set config.dmx.api_key_env to an environment variable name (e.g. DMX_API_KEY)."
            )
        if api_key_env and not os.getenv(api_key_env):
            raise ValueError(
                "Missing DMX api key. config.dmx.api_key_env is set but that environment variable is empty. "
                "Either export it (or load from dotenv), or set config.dmx.api_key."
            )
        raise ValueError("Missing DMX api key. Set config.dmx.api_key, or env DMX_API_KEY / config.dmx.api_key_env.")
    if not embedding_model:
        if embedding_model_env and _looks_like_model_name(embedding_model_env) and not os.getenv(embedding_model_env):
            raise ValueError(
                "Missing embedding model. It looks like you put a literal model name into config.dmx.embedding_model_env "
                f"({embedding_model_env}). Use config.dmx.embedding_model for literal values, "
                "or set config.dmx.embedding_model_env to an environment variable name (e.g. DMX_EMBEDDING_MODEL)."
            )
        if embedding_model_env and not os.getenv(embedding_model_env):
            raise ValueError(
                "Missing embedding model. config.dmx.embedding_model_env is set but that environment variable is empty. "
                "Either export it (or load from dotenv), or set config.dmx.embedding_model."
            )
        raise ValueError(
            "Missing embedding model. Set config.dmx.embedding_model, or env DMX_EMBEDDING_MODEL / config.dmx.embedding_model_env."
        )

    if chat_model_env and _looks_like_model_name(chat_model_env) and not os.getenv(chat_model_env):
        # Chat model is optional, but this hint is helpful if user intended to enable generation.
        pass

    defaults_raw: JsonDict = (raw.get("defaults") or {}) if isinstance(raw.get("defaults"), dict) else {}
    direction_raw = str(defaults_raw.get("direction", DefaultsConfig.direction)).strip().lower()
    if direction_raw not in ("out", "in", "both"):
        direction_raw = "both"
    defaults = DefaultsConfig(
        top_k=int(defaults_raw.get("top_k", DefaultsConfig.top_k)),
        hops=int(defaults_raw.get("hops", DefaultsConfig.hops)),
        per_source_k=int(defaults_raw.get("per_source_k", DefaultsConfig.per_source_k)),
        direction=direction_raw,  # type: ignore[arg-type]
        neighbor_boost=float(defaults_raw.get("neighbor_boost", DefaultsConfig.neighbor_boost)),
        max_sources=int(defaults_raw.get("max_sources", DefaultsConfig.max_sources)),
        final_top_k=int(defaults_raw.get("final_top_k", DefaultsConfig.final_top_k)),
        max_seed_sources=int(defaults_raw.get("max_seed_sources", DefaultsConfig.max_seed_sources)),
        max_results=int(defaults_raw.get("max_results", DefaultsConfig.max_results)),
        text_chars=int(defaults_raw.get("text_chars", DefaultsConfig.text_chars)),
        context_k=int(defaults_raw.get("context_k", DefaultsConfig.context_k)),
    )

    dmx = DmxConfig(base_url=base_url, api_key=api_key, embedding_model=embedding_model, chat_model=chat_model)
    return AppConfig(
        dotenv_path=dotenv_path,
        db_path=db_path,
        collection=collection,
        graph_path=graph_path,
        dmx=dmx,
        defaults=defaults,
    )


def init_app_state(config_path: str) -> None:
    global _STATE
    cfg = _load_config(config_path)

    graph: JsonDict = _read_json(cfg.graph_path, default={"nodes": {}, "edges": {}}) or {"nodes": {}, "edges": {}}
    graph_nodes = (graph.get("nodes") or {}) if isinstance(graph.get("nodes"), dict) else {}

    chroma_client = chromadb.PersistentClient(
        path=cfg.db_path,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    chroma_collection = chroma_client.get_or_create_collection(cfg.collection)

    embed_model = OpenAILikeEmbedding(
        model_name=cfg.dmx.embedding_model,
        api_base=cfg.dmx.base_url,
        api_key=cfg.dmx.api_key,
    )

    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    index = VectorStoreIndex.from_vector_store(vector_store=vector_store, embed_model=embed_model)

    llm = None
    if cfg.dmx.chat_model:
        llm = OpenAILike(
            model=cfg.dmx.chat_model,
            api_base=cfg.dmx.base_url,
            api_key=cfg.dmx.api_key,
            context_window=128000,
            is_chat_model=True,
        )

    _STATE = AppState(
        cfg=cfg,
        graph=graph,
        graph_nodes=graph_nodes,  # type: ignore[assignment]
        index=index,
        embed_model=embed_model,
        llm=llm,
    )


def _require_state() -> AppState:
    if _STATE is None:
        raise RuntimeError("Server is not initialized. Start with: python server.py --config /path/to/config.json")
    return _STATE


def _node_to_result(node: NodeWithScore, rank: int, text_chars: int) -> JsonDict:
    meta = node.node.metadata or {}
    text = (node.node.get_text() or "").strip()
    if text_chars > 0:
        text = text[:text_chars]
    return {
        "rank": rank,
        "node_id": node.node.node_id,
        "score": node.score,
        "source": meta.get("source"),
        "title": meta.get("title"),
        "text": text,
        "metadata": _jsonable(meta),
    }


_S_REF_RE = re.compile(r"\[s(\d+)\]", re.IGNORECASE)


def replace_s_refs_with_filenames(answer: str, used_chunks: List[JsonDict]) -> str:
    """
    Replace [S1]/[S2]/... markers in the answer back to real file path/name.

    Example:
      ".... [S1]" -> ".... [03_Notes/Chunking.md]"
    """
    if not answer or not used_chunks:
        return answer

    index_to_source: Dict[str, str] = {}
    for item in used_chunks:
        try:
            rank = int(item.get("rank"))
        except Exception:
            continue
        src = str(item.get("source") or "").strip()
        if not src:
            continue
        index_to_source[str(rank)] = src

    if not index_to_source:
        return answer

    def _repl(m: re.Match) -> str:
        idx = m.group(1)
        src = index_to_source.get(idx)
        if not src:
            return m.group(0)
        return f"[{src}]"

    return _S_REF_RE.sub(_repl, answer)


def _create_mcp() -> FastMCP:
    """
    FastMCP constructor params changed across MCP Python SDK versions.
    Keep this server compatible with older/newer releases.
    """
    kwargs = dict(
        name="Obsidian GraphRAG (Search)",
        instructions="Obsidian vault GraphRAG retrieval tools backed by Chroma + lightweight link graph expansion.",
    )
    try:
        return FastMCP(**kwargs, version="0.1.0")  # type: ignore[arg-type]
    except TypeError:
        return FastMCP(**kwargs)  # type: ignore[arg-type]


mcp = _create_mcp()


@mcp.resource("config://graphrag")
def graphrag_config() -> str:
    st = _require_state()
    raw = {
        "dotenv_path": st.cfg.dotenv_path,
        "db_path": st.cfg.db_path,
        "collection": st.cfg.collection,
        "graph_path": st.cfg.graph_path,
        "dmx": {
            "base_url": st.cfg.dmx.base_url,
            "api_key": "***redacted***",
            "embedding_model": st.cfg.dmx.embedding_model,
            "chat_model": st.cfg.dmx.chat_model,
        },
        "defaults": st.cfg.defaults.__dict__,
    }
    return json.dumps(raw, ensure_ascii=False, indent=2)


@mcp.resource("stats://graphrag")
def graphrag_stats() -> str:
    st = _require_state()
    nodes = st.graph_nodes or {}
    edges = (st.graph.get("edges") or {}) if isinstance(st.graph.get("edges"), dict) else {}
    stats = {
        "graph_nodes": len(nodes),
        "graph_edges_sources": len(edges),
        "db_path": st.cfg.db_path,
        "collection": st.cfg.collection,
    }
    return json.dumps(stats, ensure_ascii=False, indent=2)


@mcp.tool()
def graphrag_search(
    query: str,
    tags: Optional[List[str]] = None,
    tag_match: Literal["any", "all"] = "any",
    frontmatter: Optional[Dict[str, Any]] = None,
    fm_match: Literal["any", "all"] = "any",
    top_k: Optional[int] = None,
    hops: Optional[int] = None,
    per_source_k: Optional[int] = None,
    direction: Optional[Literal["out", "in", "both"]] = None,
    max_sources: Optional[int] = None,
    final_top_k: Optional[int] = None,
    max_seed_sources: Optional[int] = None,
    neighbor_boost: Optional[float] = None,
    max_results: Optional[int] = None,
    text_chars: Optional[int] = None,
) -> JsonDict:
    st = _require_state()
    d = st.cfg.defaults

    top_k_v = int(top_k if top_k is not None else d.top_k)
    hops_v = int(hops if hops is not None else d.hops)
    per_source_k_v = int(per_source_k if per_source_k is not None else d.per_source_k)
    direction_v = direction if direction is not None else d.direction
    max_sources_v = int(max_sources if max_sources is not None else d.max_sources)
    final_top_k_v = int(final_top_k if final_top_k is not None else d.final_top_k)
    max_seed_sources_v = int(max_seed_sources if max_seed_sources is not None else d.max_seed_sources)
    neighbor_boost_v = float(neighbor_boost if neighbor_boost is not None else d.neighbor_boost)
    max_results_v = int(max_results if max_results is not None else d.max_results)
    text_chars_v = int(text_chars if text_chars is not None else d.text_chars)

    tags_norm = _normalize_tags(tags)
    allowed_sources: Optional[Set[str]] = None
    if tags_norm:
        allowed_sources = _select_sources_by_tags(st.graph_nodes, tags_norm, tag_match)
        if not allowed_sources:
            return {
                "query": query,
                "results": [],
                "debug": {
                    "reason": "no_sources_match_tags",
                    "tags": tags_norm,
                    "tag_match": tag_match,
                },
            }

    fm_filters = _build_frontmatter_filters(frontmatter=frontmatter, mode=fm_match)

    retriever: BaseRetriever = ObsidianGraphRAGRetriever(
        index=st.index,
        graph=st.graph,
        top_k=top_k_v,
        hops=hops_v,
        per_source_k=per_source_k_v,
        direction=direction_v,
        allowed_sources=allowed_sources,
        metadata_filters=fm_filters,
        max_sources=max_sources_v,
        final_top_k=final_top_k_v,
        max_seed_sources=max_seed_sources_v,
        neighbor_boost=neighbor_boost_v,
    )

    nodes = retriever.retrieve(QueryBundle(query_str=query))
    if max_results_v > 0:
        nodes = nodes[:max_results_v]

    results = [_node_to_result(n, rank=i, text_chars=text_chars_v) for i, n in enumerate(nodes, 1)]
    return {
        "query": query,
        "results": results,
        "debug": {
            "allowed_sources_count": len(allowed_sources) if allowed_sources is not None else None,
            "params": {
                "top_k": top_k_v,
                "hops": hops_v,
                "per_source_k": per_source_k_v,
                "direction": direction_v,
                "neighbor_boost": neighbor_boost_v,
                "max_sources": max_sources_v,
                "final_top_k": final_top_k_v,
                "max_seed_sources": max_seed_sources_v,
                "max_results": max_results_v,
                "text_chars": text_chars_v,
                "tag_match": tag_match,
                "fm_match": fm_match,
            },
            "tags": tags_norm,
            "frontmatter": frontmatter or {},
        },
    }


@mcp.tool()
def graphrag_generate(
    query: str,
    context_k: Optional[int] = None,
    tags: Optional[List[str]] = None,
    tag_match: Literal["any", "all"] = "any",
    frontmatter: Optional[Dict[str, Any]] = None,
    fm_match: Literal["any", "all"] = "any",
    top_k: Optional[int] = None,
    hops: Optional[int] = None,
    per_source_k: Optional[int] = None,
    direction: Optional[Literal["out", "in", "both"]] = None,
    max_sources: Optional[int] = None,
    final_top_k: Optional[int] = None,
    max_seed_sources: Optional[int] = None,
    neighbor_boost: Optional[float] = None,
    max_results: Optional[int] = None,
    text_chars: Optional[int] = None,
) -> JsonDict:
    """
    最简生成：先用 graphrag_search 取上下文，再用 DMX chat 合成答案。

    注意：这会触发 LLM 调用与费用；如只需检索请使用 graphrag_search。
    """
    st = _require_state()
    if st.llm is None:
        raise ValueError("Missing chat model. Set config.dmx.chat_model (or env DMX_CHAT_MODEL) to use graphrag_generate.")

    d = st.cfg.defaults
    context_k_v = int(context_k if context_k is not None else d.context_k)

    search = graphrag_search(
        query=query,
        tags=tags,
        tag_match=tag_match,
        frontmatter=frontmatter,
        fm_match=fm_match,
        top_k=top_k,
        hops=hops,
        per_source_k=per_source_k,
        direction=direction,
        max_sources=max_sources,
        final_top_k=final_top_k,
        max_seed_sources=max_seed_sources,
        neighbor_boost=neighbor_boost,
        max_results=max_results,
        text_chars=text_chars,
    )
    results = search.get("results") or []
    top = results[: max(0, context_k_v)]

    ctx_lines: List[str] = []
    for item in top:
        rank = item.get("rank")
        title = item.get("title") or ""
        source = item.get("source") or ""
        score = item.get("score")
        text = (item.get("text") or "").strip()
        ctx_lines.append(f"[S{rank}] title={title} source={os.path.basename(str(source))} score={score}")
        ctx_lines.append(text)
        ctx_lines.append("")
    context = "\n".join(ctx_lines).strip()

    prompt = f"""
你是一个基于 Obsidian 笔记的轻量级 RAG 助手。请只依据给定资料回答；如果资料不足，请明确指出缺口，并给出下一步你希望检索的关键词。

用户问题：
{query}

资料：
{context}

回答要求：
- 优先给出结论，然后用要点解释
- 引用资料时用 [S1]/[S2] 这样的标记
""".strip()

    answer = _llm_complete_text(st.llm, prompt)
    answer = replace_s_refs_with_filenames(answer, used_chunks=top)
    return {
        "query": query,
        "answer": answer,
        "used_chunks": top,
        "search_debug": search.get("debug") or {},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to config.json")
    args = parser.parse_args()

    init_app_state(args.config)
    mcp.run()  # stdio by default


if __name__ == "__main__":
    main()
