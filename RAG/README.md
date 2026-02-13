
[Openai 请求格式 - Embedding 文本向量化 API](https://doc.dmxapi.cn/embedding.html)
DMXAPI 兼容 OpenAI 的接口格式作为嵌入服务，结合本地 ChromaDB 存储，.env保存环境变量

## 环境准备
python 3.12
chromadb 1.5.0
llama-index 0.14.13

## 安装指定python版本
```bash
uv python install 3.12
uv venv --python 3.12 .venv
```

## 启用虚拟环境
```bash
source .venv/bin/activate
```

## 停用虚拟环境
```bash
deactivate
```

## 向量嵌入
```bash
uv pip install -U openai chromadb llama-index

cd embedding

python build_db_api.py
python build_db_api_chunked.py #分块
```

## 检索
```bash
cd retrieval

python query_db.py
```

## LlamaIndex 向量嵌入与检索 
```bash
uv pip install -U llama-index-core llama-index-readers-file llama-index-llms-openai-like llama-index-embeddings-openai-like llama-index-vector-stores-chroma
```

### 基础ingest
```bash
cd llama-index/ingest_and_query

# 首次建库（推荐加 --reset）
python ingest.py --vault /path/to/markdown-notes --db /path/to/llama_chroma_db --reset

# 文档更新后想重建（同样 --reset）
python ingest.py --reset
```

### 增强版ingest（带增量更新与删除）
```bash
cd llama-index/ingest_and_query

# 首次：建议 reset
python ingest_advanced.py --reset

# 增量更新：不加 reset
python ingest_advanced.py

# 增量 + 删除已不存在的文件（prune）
python ingest_advanced.py --prune
```

### 检索
```bash
cd llama-index/ingest_and_query

# 纯检索（不需要 LLM）
python query.py "莱文哈特与弥亚的关系？" --top_k 5
python query.py "弗洛温家每日可能的饮食？" --top_k 5

# RAG（需要设置 DMX_CHAT_MODEL）
export DMX_CHAT_MODEL="GLM-4.7-Flash"
python query.py "莱文哈特与弥亚的关系？" --rag
python query.py "弗洛温家每日可能的饮食？" --rag
```

### obsidian + 图RAG版ingest（增量建库 + 构建/更新图谱）
```bash
uv pip install llama-index-readers-obsidian
```
```bash
cd llama-index/obsidian_graph_ingest_and_query

# 首次：重建向量库 + 重建图谱
python ingest_graphrag.py --reset

# 增量更新（按 file_hash 判断变更）
python ingest_graphrag.py

# 同时清理 vault 中已删除文件对应的数据
python ingest_graphrag.py --prune
```

> `ingest_graphrag.py` 已内置 Obsidian 清洗流程：移除 Frontmatter 正文噪音、Dataview 代码块、`![[...]]` 嵌入，并将 `[[...]]` 转为自然语言后再按 Markdown 结构切块。

### 图RAG版检索（GraphRAG：向量检索 + 图扩展 + 可选 RAG 合成）
```bash
cd llama-index/obsidian_graph_ingest_and_query

# 纯检索（看 GraphRAG 扩展后的上下文）
python query_graphrag.py "莱文哈特与弥亚的关系？" --top_k 5 --hops 1 --per_source_k 2
python query_graphrag.py "弗洛温家每日可能的饮食？" --top_k 5 --hops 1 --per_source_k 2

# 标签过滤 + 双向图扩展
python query_graphrag.py "本周工作重点" --tag 工作 --tag 项目A --tag_match any --direction both

# Frontmatter 过滤（示例：YAML 里的 维度分类: [组织]）
python query_graphrag.py "组织相关决策" --fm "维度分类=组织"
python query_graphrag.py "精灵角色列举" --fm "种族=精灵"

# RAG（LLM 合成）
python query_graphrag.py "莱文哈特与弥亚的关系？" --rag
python query_graphrag.py "弗洛温家每日可能的饮食？" --rag
```
