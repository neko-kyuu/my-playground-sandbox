# Obsidian GraphRAG（轻量：向量 + 图）

这个目录演示如何把 Obsidian vault ingest 成 Chroma 向量库 + 简单的笔记链接图，并在查询阶段做“图扩展召回 +（可选）改写/重排/Agent Loop”。

## 环境变量（从 `RAG/.env` 读取）

基础（必需）：
- `DMX_API_KEY`
- `DMX_BASE_URL`（默认 `https://www.dmxapi.cn/v1/`）
- `DMX_EMBEDDING_MODEL`

生成/改写/Agent（需要 LLM）：
- `DMX_CHAT_MODEL`

rerank（可选，OpenAI 兼容 rerank 接口）：
- `RERANK_MODEL`
- `RERANK_API_BASE`（默认继承 `DMX_BASE_URL`）
- `RERANK_API_KEY`（默认继承 `DMX_API_KEY`）

## Ingest

```bash
python ingest_graphrag.py --vault /path/to/your/vault
```

输出：
- Chroma DB（默认 `./llama_chroma_db`）
- 图文件（默认 `./graphrag/obsidian_graph.json`）

## Query（仅检索）

```bash
python query_graphrag.py "你的问题"
```

常用过滤：
- `--tag "#tag1" --tag "tag2,tag3" --tag_match any|all`
- `--fm key=value --fm_match any|all`

## Query（RAG 生成）

```bash
python query_graphrag.py "你的问题" --rag
```

## Query Rewriting（可选）

对“对比/多约束/多目标”问题更有效：

```bash
python agentic_query_graphrag.py "对比A和B在xxx的优缺点，并考虑约束C" --rewrite --rag
```

## rerank（可选）

脚本会请求 `POST {RERANK_API_BASE}/rerank`，payload 形如：
`{"model": "...", "query": "...", "documents": ["..."], "top_n": 30}`

启用方式：

```bash
python agentic_query_graphrag.py "你的问题" --rerank --rag
```

## Agent Loop（可选）

当判定“资料不足/不确定”时，自动生成补充检索词/子问题，并进行下一轮召回：

```bash
python agentic_query_graphrag.py "你的问题" --agent --agent_iters 2 --rag
```

## 日志

关键节点默认输出到 stderr（rewrite / retrieve / rerank / agent）。如需关闭：

```bash
python agentic_query_graphrag.py "你的问题" --quiet
```

