# Improve-v5（Advanced）：何时需要 `content_embedding` / LLM rerank / 更激进的 query_text 生成？

本文是 `plan/improve-v5/memory-embedding.md` 的“下一步评估清单”，目标是把三个常见增强点落成**可观测、可开关、可回滚**的增量路线：

- `content_embedding`：解决“summary 太抽象导致召回不准/同主题混淆”
- LLM rerank：解决“候选集里很多都差不多，排序不稳定/容易夹带噪声”
- 更激进的 `query_text` 生成：解决“query 语义不聚焦、被论坛语体/上下文噪声拖偏”

> 约束（与 v5 主文一致）：检索对象仍然只限 `memories` 条目；不引入外部文档库；召回结果必须能解释来源与理由。

---

## 0) 前置：先把“症状”量化出来（否则很难判断该加什么）

建议在现有 `memory_search` 调用链路上补齐最小可观测性（日志/调试接口皆可）：

- `query_text`（最终用于检索的短文本）
- 词法召回 top_k 的 ids + 分数（若有）
- 向量召回 top_k 的 ids + 相似度（若有）
- 最终入 prompt 的 ids（以及是否因为预算降级到 summary）
- “误召回/漏召回”的人工标注入口（哪怕先做 debug endpoint 写 JSON 到磁盘）

当你能回答下面两个问题时，再进入 Advanced 评估会更省：
- 误召回多：是“候选生成”问题，还是“排序/截断”问题？
- 漏召回多：是“query 不会描述需求”，还是“索引（summary）信息不足”？

---

## 1) `content_embedding`：什么时候需要？怎么做才不把噪声放大？

### 1.1 触发条件（出现其一再考虑）

- 同一主题记忆的 `summary` 很相似，但细节差异决定“该用哪条”（例如“谁欠谁钱/约定的日期/具体地点”）。
- 你发现：词法检索能命中正确条目，但向量（基于 summary）总偏向“语气/主题相似”的错误条目。
- 你已经做了 `merge_key` 合并与“按桶检索”，仍然存在“桶内排序不稳定”。

如果主要问题是“召回到很多重复条目”，优先做写入侧合并与检索侧多样性；`content_embedding` 不一定是第一解。

### 1.2 最小实现（推荐：两阶段、只在候选集里用）

避免直接用 `content_embedding` 做全库 ANN（成本高 + 噪声更大），建议：

1) **候选生成仍用 `summary_embedding` + 词法**（得到 ≤ 60 候选）
2) **只对候选集计算/使用 `content_embedding`** 做二次打分
   - 若候选条目没有 `content_embedding`：不阻塞（跳过或延迟补全）
   - 仅在 `summary_sim` 处于灰区时启用（例如 top1-top3 很接近）

这样做的好处：
- `content` 的风格噪声被“候选集”兜住，不会影响全库检索分布
- 计算量与预算可控

### 1.3 哪些 kind 才值得做 `content_embedding`（建议白名单）

建议优先从这些开始（信息密度高、且细节重要）：
- `recent_event`
- `relationship`（尤其是“立场变化的原因/具体事件”）

不建议默认全量启用：
- `autobiography`（通常 `summary` 已够；更重要的是“置顶/稳定”，不是细节检索）
- `secret`

### 1.4 噪声控制（必须有的门）

- `content_embedding` 的相似度阈值要高于 `summary_embedding`（更容易被细碎共同词误导）
- 同 `merge_key` 桶内最多选 1 条进入 prompt（或用 MMR）
- 只允许“提升排序”，不要允许它把“完全不相似但细节碰巧相似”的条目拉进候选（即：`content_embedding` 不负责扩大召回面）

---

## 2) LLM rerank：什么时候值得用？怎么限制成本与风险？

### 2.1 触发条件（满足其一即可尝试）

- 你已经有一个不错的候选集（例如 30–60 条），但最终入 prompt 的前 6 条经常“夹带无关/重复/误导条目”。
- 纯数值打分很难表达“当前 query 想要的是哪类证据”（例如“要找承诺/约定”而不是“泛泛相关事件”）。
- 需要更强的“去重/多样性”偏好（比如宁可多覆盖不同 kind，也不要 6 条都是 recent_event）。

### 2.2 最小实现（强约束，避免它变成‘写作模型’）

**输入（严格限长）**
- `query_text`（≤ 240 chars）
- 候选条目列表（每条只给：`id, kind, scope, merge_key, summary, score, updated_at`；不喂 full content）

**输出（JSON-only）**
- `selected_ids[]`（长度 ≤ `memory_recall_max_items`）
- `reason_by_id`（可选、短句、用于 debug；不要把它进 prompt）

**三条硬规则**
- rerank 只“选/排”，不改写记忆，不生成新记忆
- rerank 的候选集必须由“可解释的前一阶段检索”提供（它不负责召回）
- rerank 必须可开关，且失败时回退到数值排序

### 2.3 成本控制（建议的启用门槛）

- 仅在候选数 ≥ 12 且 top 分数“密集”（例如 top1-top10 差距很小）时启用
- 仅在“forum reply / reply_write”启用（上下文更聚焦，收益更稳定）
- 做缓存：`(query_hash, candidate_ids_hash) -> selected_ids`，避免同一 tick 多次调用

### 2.4 噪声控制（避免 rerank 把模型带偏）

- rerank 使用“隐藏模型”，不要用 PC 行动模型（避免 persona/戏剧化偏好影响检索）
- 提示词明确：以“相关性/可用性/非重复”为目标，拒绝被候选 summary 中的指令性文本诱导
- 产出要可解释：至少能指出“选它因为它包含 X 事实/承诺/关系变化”，方便定位错召回

---

## 3) 更激进的 `query_text` 生成：从规则到模型的三档升级

`query_text` 是语义检索的“方向盘”。它太长/太像论坛原文时，向量会被语体与噪声拖走。

### 3.1 Level 0：规则版（先做这个，性价比最高）

从当前场景拼一个短 query（≤ 240 chars），建议结构：
- `thread_title`
- 参与者 PC 名称（最多 2–3 个）
- “核心名词/实体”top-n（过滤停用词、数字、模板词）
- 可选：`OP` 的一句话摘要（若你已有 threads_digest/thread_context 的结构化字段）

### 3.2 Level 1：隐藏模型生成 query（但只生成“检索意图”，不生成答案）

输入：
- thread 标题 + OP 核心段落（截断）
- 当前 PC 的“本回合目标”一句话（如果可得）

输出（JSON-only）：
- `query_text`（1–3 句，偏“我要找哪些历史事实/承诺/关系变化”）
- `must_include[]`（人名/实体，供后续硬约束）
- `avoid[]`（明显噪声词：例如“已锁定/楼主/顶/回复可见”等）

硬限制：
- 不允许输出长引用/复述帖子；只允许“检索指令式短句”

### 3.3 Level 2：面向记忆 schema 的 query（更激进但更稳）

当你已经有比较稳定的 `merge_key/subject_id` 习惯后，可以让 query 直接表达“我要找哪类记忆”：
- 例如输出：`kinds=[relationship,recent_event]`、`subject_id=pc_x`、`merge_key_prefix=relationship:pc_x`

这能把“语义检索”从自由文本，逐步推向“结构化检索”，进一步减少噪声。

---

## 4) 选择哪个 Advanced：一张决策表（从症状到手段）

- 漏召回：明明库里有，但搜不到
  - 优先：更好的 `query_text`（Level 0→1），其次：补 `summary` 质量/merge_key
  - 最后再考虑：扩大向量召回范围（top_k）或引入 `content_embedding`

- 误召回：老是把不相关条目塞进 prompt
  - 优先：硬过滤（scope/kind）+ 相似度阈值 + merge_key 多样性
  - 再：LLM rerank（只排序，不召回）

- 重复召回：同主题多条挤占预算
  - 优先：写入侧 merge_key 合并 + 检索侧“同 merge_key 只留 1 条”
  - 再：MMR / rerank 加强多样性偏好

---

## 5) 推荐落地顺序（增量、可回滚）

1) 规则版 `query_text`（Level 0）+ 混合候选（词法 + summary 向量）+ merge_key 去重
2) 写入时近邻去重/合并（merge_key 桶内）
3) LLM rerank（仅在候选密集时启用；JSON-only）
4) `content_embedding`（先仅候选集二次打分；按 kind 白名单）
5) Level 2 结构化 query（当 merge_key/subject_id 足够稳定后再做）

