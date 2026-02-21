## genAI vault辅助脚本
用于在 genAI 库中挑选MOC key links，使用方式为Bash执行python脚本。

`export_topic_candidates.py`脚本以该格式输出：
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

### 依赖安装
```bash
uv pip install python-dotenv pyyaml openai 
```

### 执行
```bash
cd obsidian/scripts
python export_topic_candidates.py --topic RAG
python llm_select_key_links.py
```

### 一键脚本 （`export_topic_candidates.py` + `llm_select_key_links.py`）
```bash
python run_topic_key_links.py --topic RAG

#可选输出文件：
python run_topic_key_links.py \
  --topic RAG \
  --json-out rag_candidates.json \
  --out rag_key_links.md
```