# Improve-v5：Embedding 版记忆检索——“非目标”的边界实现 + 向量化内容选择 + 噪声控制

本文聚焦三个问题（来自 `plan/improve-v3/memory-technique.md` 的 1.2 非目标延伸）：

1) “非目标”不是一句话：**工程上怎么做，才能保证系统不会滑向 RAG/世界状态图谱？**
2) 引入 embedding：**到底向量化哪些内容（写入侧/检索侧）？**
3) 噪声控制：**怎么避免把无关记忆召回进 prompt，把模型带偏？**

> 约定：v5 的 embedding 只用于“记忆条目（memory items）的相似度检索/去重”，不引入外部知识库；召回结果仍是结构化的、可追溯到 `memories.id` 与来源 refs 的片段。

---

## 1) “非目标”如何实现（用边界而不是口号）

v3 的 1.2 列了两条非目标：
- 向量检索 / embeddings / RAG
- 复杂的“世界状态图谱”推理与一致性校验

到了 v5，我们**允许 embeddings**，但要把“RAG 化/图谱化”的风险关在边界外。推荐用下面这些“硬边界”实现：

### 1.1 只做 Memory-Item Retrieval，不做 Document RAG

**边界定义（必须满足）**
- 检索对象只能是 `memories` 表中的条目（或其派生视图/索引），而不是任意外部文档/网页/长对话全文。
- 每条记忆必须带来源引用（`meta_json` 内的 `source_ref_*` 或 `message_id/thread_id/conversation_id`），可回放、可审计。
- 召回的输出必须可控：有条数/字符预算上限，且可以落到 `summary` 降级。

**工程落地**
- `memory_search` 只返回“记忆条目摘要 + id + scope/kind + updated_at +（可选）相似度分数”，不返回长原文拼接，不返回“推理链”。
- 召回仅作为 prompt 里的“背景材料”，在提示词中明确它们是“可疑但有用的线索”，不能当作指令执行。

### 1.2 不做世界状态图谱：坚持“事实片段 + 轻约束”，不做全局一致性

**边界定义（必须满足）**
- 不引入跨条目强一致性校验（例如“同一人物年龄只能一个值”这种全局约束）。
- 不要求系统能推导出唯一世界状态；只提供“可能相关的记忆片段”给 LLM，自行在生成时取舍。

**工程落地**
- 关系/自传类记忆依旧是“可读文本 + subject_id + merge_key（可选）”，最多做“同主题合并/替换”，不做多跳推理。
- 允许同主题存在多条版本（revision），用 `updated_at/score/pinned` 选择，不做全自动裁决。

### 1.3 “Embedding ≠ 允许噪声”：检索必须可解释、可回滚

**边界定义（必须满足）**
- 每次召回必须能解释“为什么选它”（至少能输出：相似度、关键词命中、score、时间衰减后的分值等）。
- 必须能通过配置把向量召回关掉，回退到 v3 的关键词/FTS 方案（确保可控性与可调试性）。

---

## 2) 引入 embedding：应该向量化哪些内容？

核心原则：**向量化“稳定且信息密度高的语义单元”**，避免向量化“噪声大、模板化、或强时效流水账”的文本。

### 2.1 写入侧（Memory Item）向量化：优先 `summary`，谨慎 `content`

推荐把每条记忆拆成“用于检索的文本（canonical）”与“用于提示词拼接的文本（prompt_text）”：

- `canonical_text`（用于 embedding / 去重 / 相似检索）
  - **首选：`summary`**
  - 原因：更短、更稳定、少模板词、噪声更少；同时避免把长文本直接送入 embedding（成本/泄露/漂移都更大）。

- `prompt_text`（用于最终拼进 prompt）
  - **首选：`content`**（预算不够时降级到 `summary`）
  - 原因：content 保留细节，但细节不一定适合做“语义索引”。

什么时候也需要向量化 `content`？
- 仅当 `summary` 过度抽象导致“召回不准/同主题混淆”时，才考虑给少数 kind（例如 `recent_event`）增加第二路 embedding（`content_embedding`），并通过更严格阈值使用。

### 2.2 检索侧（Query）向量化：不要直接把 thread_context 全量 embed

**错误示范**：把 `thread_context`（标题+楼层+引用+署名+格式）全文拼一起做 query embedding。
- 噪声来源：模板化措辞、引用块、角色签名、无关楼层、格式符号，会把向量“拉向论坛语体”，导致相似度失真。

**推荐做法：两段式 query 构造**
1) **结构化抽取**（确定性优先）
   - 从当前回合可用上下文里抽取：thread 标题、OP 核心诉求句、出现的 PC 名称/地点/物件名（少量）。
2) **生成“检索用 query_text”**（可选：隐藏模型/规则）
   - 输出 1–3 句“要找什么记忆”的自然语言短句（≤ 240 chars），再 embed。

> 若暂时不想引入额外模型：先用规则生成 query_text（标题 + 关键人名 + 关键名词 top-n），依旧能显著降低噪声。

### 2.3 哪些内容不要向量化（避免污染索引）

不建议进入 embedding 的文本来源：
- 工具输出的长 JSON（高熵但低语义一致性）。
- 完整 prompt、系统提示词、或带强模板/格式的拼接文本。
- 纯流水日志（例如“XX 回复了 YY”、“tick id=...”、“时间戳…”）。
- **跨 scope 的混合文本**（尤其是把 public + pc secret 混在一起做一个 embedding）。

---

## 3) 噪声如何控制（写入侧 + 检索侧双保险）

把噪声控制拆成两层：**先减少“脏记忆”进入库**，再减少“错误召回”进入 prompt。

### 3.1 写入侧：控制“脏记忆”进入库

**写入约束（强烈建议硬性执行）**
- 每条记忆 `summary/content` 有长度上限（v3 已有），并尽量写成“可验证的事实/关系变化/事件后果”，少写情绪化修辞。
- `meta_json` 必须记录来源 refs（message/thread/conversation），必要时保留短 `source_excerpt`（≤200 chars）。
- 引入 `merge_key`（主题键）优先：同一主题尽量 upsert 合并，避免“同义重复”堆叠。

**向量去重/合并（v5 的高价值点）**
- 新写入条目先对同 scope + 同 kind（可选：同 subject_id）做近邻搜索：
  - 相似度 ≥ `dup_threshold`：走“合并/改写 summary”的路径，而不是新建一条。
  - 相似度在灰区：保留两条，但打上 `meta_json.duplicate_of` 或降低 importance，避免两条同时被召回。

#### 3.1.1 `merge_key` 如何结合向量做“合并”（推荐策略）

`merge_key` 的价值在于：**把“该合并谁”从纯语义相似（不稳定）变成“先分桶再相似”（更可控）**。建议把合并拆成两阶段：

1) **候选桶选择（硬规则）**
   - 若新条目有 `merge_key`：只在同 `(scope, kind, subject_id?, merge_key)` 桶内寻找合并对象（强一致、低噪声）。
   - 若没有 `merge_key`：退化为同 `(scope, kind, subject_id?)` 桶，用向量近邻找“可能同主题”的旧条目，并在合并后为其补上/修正 `merge_key`。

2) **桶内相似度判定（软规则）**
   - 在桶内做向量近邻（优先 `summary_embedding`），取 top 3–5。
   - 仅当相似度 ≥ `dup_threshold` 才合并；否则保留为新条目（但可写 `meta_json.suspected_merge_key`，供后续人工/模型修正）。

**合并产物（upsert 行为）**
- 更新同一条 `memories.id`（不新增）：
  - `summary/content`：把“新事实”增量合入（避免越来越长，仍受长度上限约束）
  - `updated_at`：更新
  - `importance/score`：按策略轻微上调（例如 `score += 1` 或 `score = max(score, new_importance)`）
  - `revision += 1`，并把旧版本 id 记入 `meta_json`（可选）
- 或者保留多版本但只“置顶一个”：把被合并的旧条目标记 `deleted_at`（软删）或 `edit_state=merged`（如果你已有该状态枚举）。

> 实操经验：**不要**在没有 `merge_key` 且相似度只是“看起来有点像”的情况下强行合并；宁可多留两条，再靠检索侧的多样性/merge_key 规则避免同时召回。

### 3.2 检索侧：从“可访问集合”开始，先过滤再相似度

**第一道硬过滤（必须）**
- scope：严格按访问规则（pc 私有 + 可选 public + 已校验的 direct_scope_id）。
- kind：按场景白名单（例如 forum reply 默认不召回 direct；secret 仅 pc 私有）。
- deleted/pinned/edit_state：先过滤，避免召回不可用条目。

**第二道候选生成（推荐：混合检索）**
- 向量 top_k：从 `summary_embedding` 取 top 30（按 scope 分开取，避免 public 抢占）。
- 词法 top_k：FTS5/BM25（或 v3 的 LIKE）取 top 30。
- 合并去重后得到候选集（例如最多 60）。

> 这里的“混合检索”就是：**词法检索（关键词/FTS） + 语义检索（向量）并行取候选**，再合并排序；不是二选一。

**第三道排序与截断（可解释）**
建议用一个简单可解释的打分函数（示例）：
- `final_score = w_sim * sim + w_mem * f(memory.score, pinned, recency) + w_lex * lex_score`
并加“上限/下限”：
- `sim < min_sim` 直接丢弃（减少纯噪声召回）。
- 分 kind 分桶 top-k（例如 autobiography/relationship/recent_event/secret），避免 recent_event 淹没一切。

**第四道多样性（避免相似条目扎堆）**
- 基于 MMR 或“同 merge_key 只留 1 条”的硬规则，防止重复召回。

### 3.3 召回进入 prompt 的最后一道保险：预算与降级

即使召回正确，也要避免把 prompt 撑爆或引入太多次要信息：
- 总字符预算 + 条数上限（已有 settings）。
- 先 content 后 summary，必要时 summary 截断。
- 对低重要性/低相似度条目更激进地降级为 summary 或直接丢弃。

---

## 4) v5 建议的最小落地路径（不大改架构）

1) **先只向量化 `summary`**（避免一次做太重）
2) `memory_search` 改为“混合候选 + 可解释排序”，保留“禁用向量”的回退开关
3) 引入“写入时近邻去重/合并”（降低长期噪声与库膨胀）
4) 再评估是否需要 `content_embedding`、LLM rerank、或更激进的 query_text 生成
