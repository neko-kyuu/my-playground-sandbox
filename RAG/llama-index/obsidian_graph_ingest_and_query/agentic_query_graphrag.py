# query_graphrag.py
from __future__ import annotations

import os
import json
import re
import sys
import time
import argparse
from collections import deque
from typing import Dict, List, Set, Optional, Any, Tuple

from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv
import chromadb

from llama_index.core import VectorStoreIndex
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.openai_like import OpenAILikeEmbedding
from llama_index.llms.openai_like import OpenAILike

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


def now_ms() -> int:
    return int(time.time() * 1000)


class Logger:
    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._t0_ms = now_ms()

    def log(self, msg: str) -> None:
        if not self._enabled:
            return
        dt = now_ms() - self._t0_ms
        print(f"[{dt:>6}ms] {msg}", file=sys.stderr)


def _strip_code_fence(text: str) -> str:
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    # ```json ... ```
    parts = s.split("```")
    if len(parts) >= 3:
        content = parts[1].strip()
        if content.lower().startswith("json"):
            content = content[4:].lstrip()
        return content.strip()
    return s.strip("`").strip()


def parse_json_lenient(text: str) -> Any:
    s = _strip_code_fence(text)
    try:
        return json.loads(s)
    except Exception:
        pass

    # 尝试截取最外层 JSON 对象/数组
    for left, right in (("{", "}"), ("[", "]")):
        i = s.find(left)
        j = s.rfind(right)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(s[i : j + 1])
            except Exception:
                continue
    raise ValueError("Cannot parse JSON from LLM output.")


def llm_complete_text(llm: Any, prompt: str) -> str:
    """
    兼容不同版本 LlamaIndex LLM 的接口，尽量拿到纯文本输出。
    """
    if hasattr(llm, "complete"):
        resp = llm.complete(prompt)
        if hasattr(resp, "text"):
            return resp.text
        return str(resp)

    if hasattr(llm, "chat"):
        try:
            from llama_index.core.llms import ChatMessage  # type: ignore
            resp = llm.chat([ChatMessage(role="user", content=prompt)])
            msg = getattr(resp, "message", None)
            if msg is not None and getattr(msg, "content", None) is not None:
                return msg.content
            return str(resp)
        except Exception as e:
            raise RuntimeError(f"llm.chat() is not available in this llama-index version: {e}") from e

    raise RuntimeError("Unsupported LLM object: missing complete()/chat().")


def post_json(url: str, payload: Dict[str, Any], api_key: Optional[str], timeout_s: int = 60) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = Request(url=url, data=body, headers=headers, method="POST")
    with urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def build_api_url(api_base: str, path: str) -> str:
    base = (api_base or "").rstrip("/")
    p = path if path.startswith("/") else f"/{path}"
    return f"{base}{p}"


def openai_compatible_rerank(
    api_base: str,
    api_key: Optional[str],
    model: str,
    query: str,
    documents: List[str],
    top_n: int,
    logger: Logger,
) -> List[Tuple[int, float]]:
    """
    OpenAI 兼容的 rerank：约定 POST {api_base}/rerank
    返回 (doc_index, score) 列表，按 score 降序。

    兼容常见返回格式：
    - {"results":[{"index":0,"relevance_score":0.9}, ...]}
    - {"data":[{"index":0,"score":0.9}, ...]}
    """
    url = build_api_url(api_base, "/rerank")
    payload = {
        "model": model,
        "query": query,
        "documents": documents,
        "top_n": int(top_n),
        "return_documents": False,
    }

    t0 = now_ms()
    try:
        resp = post_json(url, payload, api_key=api_key, timeout_s=int(os.getenv("RERANK_TIMEOUT_S", "60")))
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"rerank HTTPError: status={e.code} url={url} body={detail[:400]}") from e
    except URLError as e:
        raise RuntimeError(f"rerank URLError: url={url} err={e}") from e

    results = resp.get("results") or resp.get("data") or []
    pairs: List[Tuple[int, float]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        try:
            idx_i = int(idx)
            score_f = float(score)
        except Exception:
            continue
        pairs.append((idx_i, score_f))

    pairs.sort(key=lambda x: x[1], reverse=True)
    logger.log(f"rerank: {len(documents)} docs -> {len(pairs)} scored ({now_ms()-t0}ms)")
    return pairs


def rewrite_queries(llm: Any, user_query: str, n: int, logger: Logger) -> List[str]:
    n = max(2, min(int(n), 10))
    prompt = f"""
你是一个 RAG 检索规划器。请把用户问题改写为更适合检索的多个查询（特别针对：对比/多约束/多目标）。

要求：
- 输出严格 JSON（不要 Markdown、不要解释）
- 只输出一个 JSON 对象
- queries: 2~{n} 条中文短查询（每条尽量 < 20 字），覆盖不同侧重点/约束/拆分子问题
- 必须保留原问题里的专有名词、缩写、人名、项目名
- 避免无意义同义改写；要“可检索”的关键词组合

用户问题：
{user_query}

输出 JSON 格式：
{{"queries":["...","..."]}}
""".strip()

    try:
        raw = llm_complete_text(llm, prompt)
        obj = parse_json_lenient(raw)
        queries = [str(x).strip() for x in (obj or {}).get("queries", []) if str(x).strip()]
        queries = [q for q in queries if q]
        if not queries:
            raise ValueError("empty queries")
        # 去重但保序
        seen = set()
        out: List[str] = []
        for q in [user_query] + queries:
            k = q.strip()
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(k)
        logger.log(f"rewrite: {len(out)} queries -> {out}")
        return out
    except Exception as e:
        logger.log(f"rewrite: failed, fallback to original query. err={e}")
        return [user_query]


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


def _short_source(meta: Dict[str, Any]) -> str:
    src = str((meta or {}).get("source") or "")
    if not src:
        return ""
    # 只打印文件名，避免日志太长
    base = os.path.basename(src)
    return base or src


S_REF_RE = re.compile(r"\[S(\d+)\]")


def replace_s_refs_with_filenames(answer: str, nodes: List[NodeWithScore], logger: Logger) -> str:
    if not answer or not nodes:
        return answer

    index_to_file: Dict[str, str] = {}
    for i, r in enumerate(nodes, 1):
        meta = r.node.metadata or {}
        fn = _short_source(meta)
        if fn:
            index_to_file[str(i)] = fn

    if not index_to_file:
        return answer

    replaced = 0

    def _repl(m: re.Match) -> str:
        nonlocal replaced
        idx = m.group(1)
        fn = index_to_file.get(idx)
        if not fn:
            return m.group(0)
        replaced += 1
        return f"[{fn}]"

    out = S_REF_RE.sub(_repl, answer)
    if replaced:
        logger.log(f"post: replaced {replaced} [S#] refs with filenames")
    return out


def retrieve_multi(retriever: BaseRetriever, queries: List[str], logger: Logger) -> List[NodeWithScore]:
    merged: Dict[str, NodeWithScore] = {}
    for q in queries:
        t0 = now_ms()
        got = retriever.retrieve(q)
        logger.log(f"retrieve: q='{q}' -> {len(got)} nodes ({now_ms()-t0}ms)")
        for item in got:
            nid = item.node.node_id
            prev = merged.get(nid)
            if prev is None or (item.score or 0.0) > (prev.score or 0.0):
                merged[nid] = item
    out = sorted(merged.values(), key=lambda x: (x.score or 0.0), reverse=True)
    logger.log(f"retrieve: merged -> {len(out)} unique nodes")
    return out


def rerank_nodes(
    nodes: List[NodeWithScore],
    query: str,
    api_base: str,
    api_key: Optional[str],
    model: str,
    rerank_top_n: int,
    logger: Logger,
) -> List[NodeWithScore]:
    if not nodes:
        return nodes
    if not model:
        logger.log("rerank: skipped (missing model)")
        return nodes

    top_n = max(1, min(int(rerank_top_n), len(nodes)))
    docs: List[str] = []
    for r in nodes[:top_n]:
        meta = r.node.metadata or {}
        title = str(meta.get("title") or "")
        text = (r.node.get_text() or "").strip()
        if title:
            doc = f"{title}\n{text}"
        else:
            doc = text
        docs.append(doc[: int(os.getenv("RERANK_DOC_CHARS", "2200"))])

    try:
        scored = openai_compatible_rerank(
            api_base=api_base,
            api_key=api_key,
            model=model,
            query=query,
            documents=docs,
            top_n=top_n,
            logger=logger,
        )
    except Exception as e:
        logger.log(f"rerank: failed, keep original order. err={e}")
        return nodes

    if not scored:
        logger.log("rerank: empty scores, keep original order")
        return nodes

    idx_to_score = {i: s for i, s in scored}
    ordered: List[NodeWithScore] = []
    used: Set[int] = set()

    for i, score in scored:
        if 0 <= i < top_n and i not in used:
            item = nodes[i]
            try:
                item.score = score
            except Exception:
                pass
            ordered.append(item)
            used.add(i)

    # 补齐 top_n 中未返回的
    for i in range(top_n):
        if i in used:
            continue
        ordered.append(nodes[i])

    # 追加其余（未 rerank）
    ordered.extend(nodes[top_n:])

    logger.log(
        "rerank: top5=" + ", ".join(
            f"{idx_to_score.get(i, None)}:{_short_source((ordered[i].node.metadata or {}))}"
            for i in range(min(5, len(ordered)))
        )
    )
    return ordered


def build_evidence_brief(nodes: List[NodeWithScore], k: int) -> str:
    k = max(1, min(int(k), len(nodes)))
    lines: List[str] = []
    for i, r in enumerate(nodes[:k], 1):
        meta = r.node.metadata or {}
        title = str(meta.get("title") or "")
        snippet = (r.node.get_text() or "").strip().replace("\n", " ")
        snippet = snippet[:240]
        lines.append(f"[S{i}] title={title} source={_short_source(meta)} :: {snippet}")
    return "\n".join(lines)


def judge_need_more(llm: Any, user_query: str, evidence_brief: str, logger: Logger) -> Tuple[bool, str, List[str]]:
    prompt = f"""
你是一个检索-生成 Agent 的“检索充分性判定器”。

给你用户问题和当前召回到的资料摘要，请判断：
1) 这些资料是否足以回答问题（尤其是对比/多约束/多目标）
2) 如果不够：生成补充检索词/子问题，用于下一轮召回

输出严格 JSON（不要 Markdown、不要解释），格式：
{{"need_more": true/false, "reason": "...", "followup_queries": ["...","..."]}}

约束：
- 如果 need_more=false，followup_queries 置空数组
- followup_queries 最多 5 条，尽量短、可检索、包含关键实体/约束

用户问题：
{user_query}

资料摘要：
{evidence_brief}
""".strip()

    raw = llm_complete_text(llm, prompt)
    obj = parse_json_lenient(raw) or {}
    need_more = bool(obj.get("need_more", False))
    reason = str(obj.get("reason") or "").strip()
    followups = [str(x).strip() for x in (obj.get("followup_queries") or []) if str(x).strip()]

    # 去重但保序
    seen: Set[str] = set()
    uniq: List[str] = []
    for q in followups:
        if q in seen:
            continue
        seen.add(q)
        uniq.append(q)
    followups = uniq[:5]

    logger.log(f"agent: need_more={need_more} followups={followups} reason={reason[:80]}")
    return need_more, reason, followups


def main():
    load_dotenv(dotenv_path="/Users/nekokyuu/vscode/playground-sandbox/RAG/.env")

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
    parser.add_argument("--rewrite", action="store_true", help="启用 Query Rewriting（需要 DMX_CHAT_MODEL）")
    parser.add_argument("--rewrite_n", type=int, default=int(os.getenv("REWRITE_N", "6")))
    parser.add_argument("--rerank", action="store_true", help="启用 rerank（OpenAI 兼容 /rerank）")
    parser.add_argument("--rerank_model", default=os.getenv("RERANK_MODEL", ""), help="rerank 模型名（也可用 env: RERANK_MODEL）")
    parser.add_argument("--rerank_base", default=os.getenv("RERANK_API_BASE", ""), help="rerank API base（默认继承 DMX_BASE_URL）")
    parser.add_argument("--rerank_key", default=os.getenv("RERANK_API_KEY", ""), help="rerank API key（默认继承 DMX_API_KEY）")
    parser.add_argument("--rerank_top_n", type=int, default=int(os.getenv("RERANK_TOP_N", "30")))
    parser.add_argument("--agent", action="store_true", help="启用 Agent Loop：不足则生成补充检索词并二次召回")
    parser.add_argument("--agent_iters", type=int, default=int(os.getenv("AGENT_ITERS", "2")))
    parser.add_argument("--context_k", type=int, default=int(os.getenv("CONTEXT_K", "8")))
    parser.add_argument("--quiet", action="store_true", help="关闭关键节点日志")
    args = parser.parse_args()

    logger = Logger(enabled=(not args.quiet))

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
        logger.log(f"filter: tags={selected_tags} mode={args.tag_match} -> allowed_sources={len(allowed_sources)}")

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

    llm = None
    if args.rag or args.rewrite:
        DMX_CHAT_MODEL = os.getenv("DMX_CHAT_MODEL")
        if not DMX_CHAT_MODEL:
            raise ValueError("To use --rag/--rewrite, set DMX_CHAT_MODEL in env")
        llm = OpenAILike(
            model=DMX_CHAT_MODEL,
            api_base=DMX_BASE_URL,
            api_key=DMX_API_KEY,
            context_window=128000,
            is_chat_model=True,
        )

    queries = [args.q]
    if args.rewrite:
        queries = rewrite_queries(llm=llm, user_query=args.q, n=args.rewrite_n, logger=logger)

    rerank_base = (args.rerank_base or "").strip() or DMX_BASE_URL
    rerank_key = (args.rerank_key or "").strip() or DMX_API_KEY

    if not args.rag:
        nodes = retrieve_multi(graphrag_retriever, queries, logger=logger)
        if args.rerank:
            nodes = rerank_nodes(
                nodes=nodes,
                query=args.q,
                api_base=rerank_base,
                api_key=rerank_key,
                model=str(args.rerank_model or "").strip(),
                rerank_top_n=args.rerank_top_n,
                logger=logger,
            )
        for i, r in enumerate(nodes[:20], 1):
            meta = r.node.metadata or {}
            print(f"\n[{i}] score={r.score} source={meta.get('source')} title={meta.get('title')}")
            print(r.node.get_text()[:800])
        return

    # ---- RAG：先做（可选）rewrite/agent/rerank 召回，再用轻量 prompt 合成答案 ----
    # ---- Agent Loop（可选）：资料不足 -> 生成补充检索词 -> 回到召回 ----
    iters = max(1, min(int(args.agent_iters), 5))
    nodes: List[NodeWithScore] = []
    for step in range(iters):
        logger.log(f"agent: iter={step+1}/{iters} queries={len(queries)}")
        nodes = retrieve_multi(graphrag_retriever, queries, logger=logger)
        if args.rerank:
            nodes = rerank_nodes(
                nodes=nodes,
                query=args.q,
                api_base=rerank_base,
                api_key=rerank_key,
                model=str(args.rerank_model or "").strip(),
                rerank_top_n=args.rerank_top_n,
                logger=logger,
            )

        if not args.agent:
            break

        if not nodes:
            evidence = "(empty)"
        else:
            evidence = build_evidence_brief(nodes, k=min(args.context_k, 8))

        try:
            need_more, _reason, followups = judge_need_more(llm, user_query=args.q, evidence_brief=evidence, logger=logger)
        except Exception as e:
            logger.log(f"agent: judge failed, stop loop. err={e}")
            break

        if (not need_more) or (not followups) or (step >= iters - 1):
            break

        # merge followups
        seen = set(queries)
        added = 0
        for q in followups:
            if q in seen:
                continue
            queries.append(q)
            seen.add(q)
            added += 1
        logger.log(f"agent: add_followups={added} -> queries={len(queries)}")

    top_nodes = nodes[: min(len(nodes), max(1, int(args.context_k)))]
    ctx_lines: List[str] = []
    for i, r in enumerate(top_nodes, 1):
        meta = r.node.metadata or {}
        title = str(meta.get("title") or "")
        ctx_lines.append(f"[S{i}] title={title} source={_short_source(meta)} score={r.score}")
        ctx_lines.append(r.node.get_text()[:1200].strip())
        ctx_lines.append("")
    context = "\n".join(ctx_lines).strip()

    prompt = f"""
你是一个基于 Obsidian 笔记的轻量级 RAG 助手。请只依据给定资料回答；如果资料不足，请明确指出缺口，并给出下一步你希望检索的关键词。

用户问题：
{args.q}

资料：
{context}

回答要求：
- 优先给出结论，然后用要点解释
- 引用资料时用 [S1]/[S2] 这样的标记
""".strip()

    logger.log(f"generate: context_nodes={len(top_nodes)}")
    answer = llm_complete_text(llm, prompt)
    answer = replace_s_refs_with_filenames(answer, top_nodes, logger=logger)
    print(answer)


if __name__ == "__main__":
    main()
