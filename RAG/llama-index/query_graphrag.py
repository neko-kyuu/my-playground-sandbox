# query_graphrag.py
from __future__ import annotations

import os
import json
import argparse
from collections import deque
from typing import Dict, List, Set, Optional

from dotenv import load_dotenv
import chromadb

from llama_index.core import VectorStoreIndex
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.openai_like import OpenAILikeEmbedding
from llama_index.llms.openai_like import OpenAILike

from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.retrievers import BaseRetriever
from llama_index.core.schema import NodeWithScore
from llama_index.core.vector_stores import (
    MetadataFilter,
    MetadataFilters,
    FilterCondition,
    FilterOperator,
)

from llama_index.core.schema import QueryBundle

def read_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_inbound_edges(graph_edges: Dict[str, List[str]]) -> Dict[str, List[str]]:
    inbound: Dict[str, Set[str]] = {}
    for src, neighbors in (graph_edges or {}).items():
        inbound.setdefault(src, set())
        for nb in neighbors or []:
            inbound.setdefault(nb, set()).add(src)
    return {k: sorted(v) for k, v in inbound.items()}


def normalize_cli_tags(raw_tags: List[str]) -> List[str]:
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


def normalize_frontmatter_key(key: str) -> str:
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


def build_frontmatter_filters(raw_filters: List[str], mode: str) -> Optional[MetadataFilters]:
    if not raw_filters:
        return None

    filters: List[MetadataFilter] = []
    for raw in raw_filters:
        item = str(raw).strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"Invalid --fm value {item}, expected key=value")

        key_raw, value_raw = item.split("=", 1)
        key = normalize_frontmatter_key(key_raw)
        value = value_raw.strip()
        if not key:
            raise ValueError(f"Invalid --fm key {key_raw}")
        if value == "":
            raise ValueError("Empty --fm value is not supported yet (this project drops empty YAML values at ingest time)")

        filters.append(MetadataFilter(key=f"fm_{key}", value=value, operator=FilterOperator.EQ))

    if not filters:
        return None

    condition = FilterCondition.AND if mode == "all" else FilterCondition.OR
    return MetadataFilters(filters=filters, condition=condition)


def select_sources_by_tags(nodes_meta: Dict[str, Dict], tags: List[str], mode: str) -> Set[str]:
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


def expand_sources(
    graph_edges: Dict[str, List[str]],
    graph_inbound_edges: Dict[str, List[str]],
    seeds: Set[str],
    hops: int,
    direction: str = "both",
) -> Set[str]:
    """
    Graph 扩展：支持 out/in/both 三种方向。
    """
    if hops <= 0:
        return set(seeds)

    seen = set(seeds)
    q = deque([(s, 0) for s in seeds])

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
        graph: Dict,
        top_k: int = 5,
        hops: int = 1,
        per_source_k: int = 2,
        neighbor_boost: float = 0.85,
        max_sources: int = 10,
        final_top_k: int = 15,
        max_seed_sources: int = 5,
        direction: str = "both",
        allowed_sources: Optional[Set[str]] = None,
        metadata_filters: Optional[MetadataFilters] = None,
    ):
        super().__init__()
        self._index = index
        self._edges = (graph or {}).get("edges", {}) or {}
        self._inbound_edges = build_inbound_edges(self._edges)
        self._top_k = top_k
        self._hops = hops
        self._per_source_k = per_source_k
        self._neighbor_boost = neighbor_boost
        self._max_sources = max_sources
        self._final_top_k = final_top_k
        self._max_seed_sources = max_seed_sources
        self._direction = direction
        self._allowed_sources = set(allowed_sources) if allowed_sources is not None else None
        self._metadata_filters = metadata_filters

    def _get_embed_model(self):
        # 兼容你当前 0.14.x 的常见结构（尽量不写死）
        em = getattr(self._index, "_embed_model", None)
        if em is not None:
            return em
        sc = getattr(self._index, "_service_context", None)
        if sc is not None and getattr(sc, "embed_model", None) is not None:
            return sc.embed_model
        raise RuntimeError("Cannot find embed_model on index; pass embed_model explicitly or check llama-index version.")

    def _retrieve(self, query_bundle) -> List[NodeWithScore]:
        # 兼容：外部如果传了字符串进来
        if isinstance(query_bundle, str):
            query_bundle = QueryBundle(query_str=query_bundle)

        query_str = query_bundle.query_str

        # 关键：确保只算一次 query embedding，后面所有 retrieve() 都复用它
        if query_bundle.embedding is None:
            embed_model = self._get_embed_model()
            query_bundle.embedding = embed_model.get_query_embedding(query_str)

        # 1) primary retrieval（注意：传 QueryBundle，而不是 query_str）
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
                r for r in primary
                if (r.node.metadata or {}).get("source") in self._allowed_sources
            ]

        # 2) seed sources（按 primary 最佳分排序）
        best_seed_score = {}
        for r in primary:
            s = (r.node.metadata or {}).get("source")
            if not s:
                continue
            if self._allowed_sources is not None and s not in self._allowed_sources:
                continue
            sc = r.score or 0.0
            best_seed_score[s] = max(best_seed_score.get(s, 0.0), sc)

        seed_sources_sorted = sorted(best_seed_score.keys(), key=lambda s: best_seed_score[s], reverse=True)
        seed_sources_sorted = seed_sources_sorted[: self._max_seed_sources]
        seed_set = set(seed_sources_sorted)

        # 3) 图扩展
        expanded_set = expand_sources(
            graph_edges=self._edges,
            graph_inbound_edges=self._inbound_edges,
            seeds=seed_set,
            hops=self._hops,
            direction=self._direction,
        )

        if self._allowed_sources is not None:
            expanded_set &= self._allowed_sources

        # 如果 top_k 太小导致 seed 为空，退化成“标签过滤后的语义检索”。
        if not seed_sources_sorted and self._allowed_sources:
            ordered_sources = sorted(self._allowed_sources)
            if self._max_sources is not None and self._max_sources > 0:
                ordered_sources = ordered_sources[: self._max_sources]
            seed_set = set()
        else:
            # 4) Source pruning（稳定可复现）
            neighbor_sources = sorted([s for s in expanded_set if s not in seed_set])
            ordered_sources = seed_sources_sorted + neighbor_sources
            if self._max_sources is not None and self._max_sources > 0:
                ordered_sources = ordered_sources[: self._max_sources]

        # 5) secondary retrieval（每个 source 一次向量库查询；embedding 不会重复了）
        secondary: List[NodeWithScore] = []
        for s in ordered_sources:
            source_filter = MetadataFilter(key="source", value=s, operator=FilterOperator.EQ)
            filters = MetadataFilters(filters=[source_filter])
            if self._metadata_filters is not None:
                filters = MetadataFilters(
                    filters=[self._metadata_filters, filters],
                    condition=FilterCondition.AND,
                )

            r = self._index.as_retriever(similarity_top_k=self._per_source_k, filters=filters)
            got = r.retrieve(query_bundle)  # 关键：仍然传 QueryBundle

            if s not in seed_set:
                for x in got:
                    if x.score is not None:
                        x.score *= self._neighbor_boost
            secondary.extend(got)

        # 6) merge + 去重
        best = {}
        for item in (primary + secondary):
            nid = item.node.node_id
            if nid not in best or (item.score or 0) > (best[nid].score or 0):
                best[nid] = item

        merged = sorted(best.values(), key=lambda x: (x.score or 0), reverse=True)

        # 7) global pruning
        return merged[: self._final_top_k]

def main():
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("q")
    parser.add_argument("--db", default=os.getenv("GRAPH_DB_PATH", "./llama_chroma_db"))
    parser.add_argument("--collection", default=os.getenv("CHROMA_COLLECTION", "quickstart"))
    parser.add_argument("--graph", default=os.getenv("GRAPH_PATH", "./graphrag/obsidian_graph.json"))
    parser.add_argument("--top_k", type=int, default=int(os.getenv("TOP_K", "5")))
    parser.add_argument("--hops", type=int, default=int(os.getenv("GRAPH_HOPS", "1")))
    parser.add_argument("--per_source_k", type=int, default=int(os.getenv("PER_SOURCE_K", "2")))
    parser.add_argument("--direction", choices=["out", "in", "both"], default=os.getenv("GRAPH_DIRECTION", "both"))
    parser.add_argument("--tag", action="append", default=[], help="按标签过滤（可重复，支持逗号分隔）")
    parser.add_argument("--tag_match", choices=["any", "all"], default=os.getenv("TAG_MATCH", "any"))
    parser.add_argument("--fm", action="append", default=[], help="按 Frontmatter 过滤，格式 key=value，可重复")
    parser.add_argument("--fm_match", choices=["any", "all"], default=os.getenv("FM_MATCH", "all"))
    parser.add_argument("--rag", action="store_true")
    args = parser.parse_args()

    DMX_API_KEY = os.getenv("DMX_API_KEY")
    DMX_BASE_URL = os.getenv("DMX_BASE_URL", "https://www.dmxapi.cn/v1/")
    DMX_EMBEDDING_MODEL = os.getenv("DMX_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")

    if not DMX_API_KEY:
        raise ValueError("Missing DMX_API_KEY")

    graph = read_json(args.graph, default={"nodes": {}, "edges": {}})
    graph_nodes = (graph or {}).get("nodes", {}) or {}
    selected_tags = normalize_cli_tags(args.tag)

    allowed_sources: Optional[Set[str]] = None
    if selected_tags:
        allowed_sources = select_sources_by_tags(graph_nodes, selected_tags, args.tag_match)
        if not allowed_sources:
            print(f"No graph nodes match tags={selected_tags} with mode={args.tag_match}.")
            return

    try:
        fm_filters = build_frontmatter_filters(args.fm, args.fm_match)
    except ValueError as e:
        print(f"Invalid frontmatter filter: {e}")
        return

    chroma_client = chromadb.PersistentClient(path=args.db)
    chroma_collection = chroma_client.get_or_create_collection(args.collection)

    embed_model = OpenAILikeEmbedding(
        model_name=DMX_EMBEDDING_MODEL,
        api_base=DMX_BASE_URL,
        api_key=DMX_API_KEY,
    )

    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    index = VectorStoreIndex.from_vector_store(vector_store=vector_store, embed_model=embed_model)

    graphrag_retriever = ObsidianGraphRAGRetriever(
        index=index,
        graph=graph,
        top_k=args.top_k,
        hops=args.hops,
        per_source_k=args.per_source_k,
        direction=args.direction,
        allowed_sources=allowed_sources,
        metadata_filters=fm_filters,
        max_sources=10,
        final_top_k=15,
    )

    if not args.rag:
        nodes = graphrag_retriever.retrieve(args.q)
        for i, r in enumerate(nodes[:20], 1):
            meta = r.node.metadata or {}
            print(f"\n[{i}] score={r.score} source={meta.get('source')} title={meta.get('title')}")
            print(r.node.get_text()[:800])
        return

    DMX_CHAT_MODEL = os.getenv("DMX_CHAT_MODEL")
    if not DMX_CHAT_MODEL:
        raise ValueError("To use --rag, set DMX_CHAT_MODEL in env")

    llm = OpenAILike(
        model=DMX_CHAT_MODEL,
        api_base=DMX_BASE_URL,
        api_key=DMX_API_KEY,
        context_window=128000,
        is_chat_model=True
    )

    # 用 RetrieverQueryEngine 让 LlamaIndex 做合成
    engine = RetrieverQueryEngine.from_args(retriever=graphrag_retriever, llm=llm)
    resp = engine.query(args.q)
    print(resp)


if __name__ == "__main__":
    main()
