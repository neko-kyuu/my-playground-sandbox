obsidian库路径：/Users/nekokyuu/genAI/genAI

## 启用虚拟环境
```bash
uv venv
source .venv/bin/activate
```

## genAI vault辅助脚本
脚本以该格式输出：
```json
{
  "topic": "RAG",
  "candidates": [
    {"path":"03_Notes/Chunking.md","type":"note","status":"evergreen","facets":["chunking"],"summary":"...","mtime":"2026-02-10"},
    ...
  ],
  "common_facets": ["embeddings","vector_db","chunking", "..."]
}
```
LLM 基于这个 JSON 做 MOC Key Links 选择

```bash
uv pip install python-dotenv pyyaml openai 
```

```bash
cd obsidian/scripts
python export_topic_candidates.py --topic RAG
python llm_select_key_links.py
```

llm 侧使用 prompt 见 `key_links_prompt.md`

一键脚本 `export_topic_candidates.py` + `llm_select_key_links.py`
```bash
python run_topic_key_links.py --topic RAG

#可选输出文件：
python run_topic_key_links.py \
  --topic RAG \
  --json-out rag_candidates.json \
  --out rag_key_links.md
```