## Obsidian GraphRAG MCP（stdio）

把 `RAG/llama-index/obsidian_graph_ingest_and_query/agentic_query_graphrag.py` 的“向量 + 图扩展召回”能力封装为 MCP server（默认 stdio），供 Claude Code / Codex / Gemini CLI / roo code 等 MCP 客户端调用。

### 能力

- Tool: `graphrag_search`：纯检索，返回 top chunks（可选 tags / frontmatter 过滤）
- Tool: `graphrag_generate`（可选）：最简生成（基于 `graphrag_search` 的上下文 + DMX chat）
- Resource: `config://graphrag`：返回已加载配置（已脱敏）
- Resource: `stats://graphrag`：返回索引/图的基础统计

### Tool 入参格式（更适合 MCP）

`graphrag_search` 典型调用参数（示例）：

```json
{
  "query": "对比向量检索和 GraphRAG 的优缺点",
  "tags": ["rag", "graphrag"],
  "tag_match": "any",
  "frontmatter": {"status": "evergreen", "facets": "retrieval"},
  "fm_match": "any",
  "top_k": 5,
  "hops": 1,
  "direction": "both",
  "max_results": 15,
  "text_chars": 1200
}
```

返回结构（简化）：
- `results[]`: `{rank,node_id,score,source,title,text,metadata}`

### 目录约定

- 通过 `config.json` 固定一套路径与默认参数（db/collection/graph、模型配置等）
- 不在 tool 调用里允许传 `db_path/graph_path`，避免任意文件读取风险

### 安装（uv）

在本目录创建虚拟环境：

```bash
cd MCP/obsidian_graphrag_mcp
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

准备配置文件：

```bash
cp config.example.json config.json
```

然后编辑 `config.json`，至少填好：
- `db_path` / `collection`
- `graph_path`
- `dmx.api_key`（或使用 `dmx.api_key_env` + 环境变量 / .env）
- `dmx.embedding_model`（或使用 `dmx.embedding_model_env` + 环境变量 / .env）

如需启用 `graphrag_generate`，还需要 `dmx.chat_model`。

`*_env` 字段放的是“环境变量名”，不是值本身。例如：

```json
{
  "dmx": {
    "api_key_env": "DMX_API_KEY",
    "embedding_model_env": "DMX_EMBEDDING_MODEL",
    "chat_model_env": "DMX_CHAT_MODEL"
  }
}
```

如果你想把值直接写进 `config.json`（不推荐放密钥，但可用于本地实验），用不带 `_env` 的字段：

```json
{
  "dmx": {
    "api_key": "sk-***",
    "embedding_model": "Qwen/Qwen3-Embedding-8B",
    "chat_model": "GLM-4.7-Flash"
  }
}
```

### 运行（stdio）

```bash
cd MCP/obsidian_graphrag_mcp
source .venv/bin/activate
python server.py --config ./config.json
```

### 在 Terminal 里测试（不依赖 Claude Code 等客户端）

用 MCP Python SDK 写一个“本地 client”去通过 stdio 拉起 server 并调用 tools/resources。

先做无费用的连通性检查（不会调用 embedding/LLM）：

```bash
cd MCP/obsidian_graphrag_mcp
source .venv/bin/activate

# 列出 tools/resources
python client_test.py --config ./config.json --list

# 读 stats resource
python client_test.py --config ./config.json --stats
```

然后再做真实检索（会调用 embedding，可能产生费用）：

```bash
python client_test.py --config ./config.json --search "弗洛温家每日可能的饮食？"
```

如果配置了 `dmx.chat_model`，也可以测试最简生成（会调用 LLM，可能产生费用）：

```bash
python client_test.py --config ./config.json --generate "弗洛温家每日可能的饮食？"
```

### MCP 客户端配置示例

不同客户端的 JSON 字段名可能略有差异，但核心是给它一个 stdio 命令。一个常见结构如下：

```json
{
  "mcpServers": {
    "obsidian-graphrag": {
      "command": ["/ABS/PATH/TO/python", "/ABS/PATH/TO/MCP/obsidian_graphrag_mcp/server.py", "--config", "/ABS/PATH/TO/MCP/obsidian_graphrag_mcp/config.json"]
    }
  }
}
```

如果你用的是本目录的 venv，`python` 一般是：
- macOS/Linux：`/ABS/PATH/TO/MCP/obsidian_graphrag_mcp/.venv/bin/python`

### 注意事项

- `graphrag_search` 会调用 embedding（DMX/OpenAI-compatible），会产生网络请求与费用。
- 为避免离线环境启动卡住，本 server 会关闭 Chroma 的 anonymized telemetry（不影响功能）。
- 本 server 不包含 ingest；请先在 `RAG/llama-index/obsidian_graph_ingest_and_query` 跑完 `ingest_graphrag.py`，确保 `db_path` 与 `graph_path` 指向对应产物。
