# Improve-v5：记忆系统向量化（Embedding）与噪声控制（设计草案）

本目录用于承接 `plan/improve-v3/memory-technique.md` 的记忆系统，并在保持“可控、可解释、可遗忘”的前提下，引入 **embedding** 做更稳定的召回与去重。

- 记忆系统 v3（无向量、先表格化）：`plan/improve-v3/memory-technique.md`
- v5：Embedding 引入范围、哪些内容向量化、以及噪声控制：`plan/improve-v5/memory-embedding.md`
- v5（后续评估清单）：何时需要 `content_embedding` / LLM rerank / 更激进的 query_text 生成：`plan/improve-v5/memory-embedding-advanced.md`
- v5（可选扩展）：接入外部 Markdown/Obsidian 文档（`doc_search` + LlamaIndex 清洗 + `MarkdownNodeParser`）：`plan/improve-v5/external-markdown-corpus.md`
