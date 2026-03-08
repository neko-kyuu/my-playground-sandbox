# Improve-v3：记忆系统优化（无向量 / 先表格化）

本文目标：在不引入向量化 / RAG 的前提下，为每个 PC 提供**可写入、可检索、可遗忘**的“长期/中期记忆”，并把可控的记忆片段拼接进 TickRunner 的 LLM 上下文中。

参考与现状依据：
- v1：`plan/improve-v1/forum-technique.md`（TickRunner / pc_activity / ticks 等数据层思路）
- v2：`plan/improve-v2/forum-reply-multistep.md`（多步骤 LLM 与“先 digest 后按需抓取”的上下文控量思路）
- 现状：后端已有 `ticks`、`pc_activity`（`backend/app/db.py` / `backend/app/models.py`），TickRunner 目前 `recall` 仅依赖 `pc_activity`（`backend/app/tick_runner.py`）。

---

## 0. 痛点（为什么需要 improve-v3）

当前 `pc_activity` 更像“行动日志索引”，缺少：
- **可长期保留的事实**：自传信息（偏好、背景、承诺等）、关系变化、关键事件（以及其后果）。
- **检索可控性**：无法按“本回合相关性”挑出少量最重要的记忆；也没有“被检索一次 +1”的反馈信号。
- **遗忘机制**：没有衰减与淘汰，导致（未来）上下文膨胀不可控。
- **隐私边界**：秘密/私密信息缺少明确归属与可见性规则（至少要做到“只在该 PC 内部可用”）。

---

## 1. 目标与非目标

### 1.1 目标（MVP 必做）
- 记忆按“可见范围（scope）”分层（SQLite）：
  - **PC 私有记忆**：仅该 PC 可用（用于自传/关系/秘密等）。
  - **共享记忆池（public）**：来自 `Message.channel="broadcast"` 的信息，所有 PC 可用。
  - **私聊共享记忆（direct）**：来自 `Message.channel="direct"` 的信息，归属“私聊双方”（`from_actor` 与 `to` 里的 PC 参与者）共同可用。
- 支持 4 类记忆：
  - **自传（autobiography）**：稳定事实（长期）
  - **关系（relationship）**：对其他 PC 的看法/关系变化（长期/中期）
  - **近期事件（recent_event）**：近若干天/若干回合的重要事件（中期）
  - **秘密（secret）**：对外不可见、仅该 PC 可用（长期/中期）
- 基础检索（先易后难）：
  - MVP：先仅在 Round2（`reply_write`）为当前 PC 选出“少量记忆片段”拼进 prompt，观察收益与副作用。
  - 后续增强：再扩展到 Round1（`forum_action`）的 action 决策阶段（收益更大，但更难落地与调参）。
- 遗忘：每条记忆被检索一次 `+1`；每 N ticks（默认 50）执行一次衰减 `-k`；低于阈值删除（可配置）。
- 上下文预算：**拼进 prompt 的记忆内容有硬上限**；先按字符/条数控量，后续再精确 token 估算。

### 1.2 非目标（本阶段不做 / 可延后）
- 向量检索 / embeddings / RAG。
- 复杂的“世界状态图谱”推理与一致性校验。
- 前端可视化记忆编辑器（先提供后端 debug API/工具即可）。
- 更复杂的可见性策略（例如分组可见、权限继承、可撤回共享等）。

---

## 2. 总体方案（最小可落地）

将“记忆”视为 TickRunner 的**后台附属管线**：

1) **写入（Memory Write）**：每次行动落库后，提取/生成“记忆增量”，按 scope 写入 `memories`：
   - `Message.channel="broadcast"` → `scope="public"`（共享记忆池）
   - `Message.channel="direct"` → `scope="direct"`（私聊双方共享）
   - 角色内在信息（自传/秘密/关系）→ `scope="pc"`（PC 私有）
2) **检索（Memory Recall）**：在模型决策前，从“本次决策允许访问的 scope 集合”里选出少量片段，拼到 prompt（带预算）。
3) **遗忘（Memory Decay/GC）**：每 50 ticks 触发一次衰减与清理（可配置）。

其中，“写入/检索”可以先做规则版（确定性、可控），再引入“对前端不可见的模型”做更高质量的归纳与去重。

落地策略（重要）：
- **先接入 Round2（`reply_write`）**：上下文更聚焦（已选定 thread + 有 thread_context），更容易看到“记忆”对回复质量/一致性的增益。
- **Round1（`forum_action`）接入更好但更难**：因为 action 决策阶段更发散，且错误召回更容易把模型带偏；建议在 Round2 验证可控后再扩展。

---

## 3. 数据模型（SQLite，先表格化）

新增表：`memories`（按 scope 统一存）

建议字段（MVP）：
- `id` TEXT PK
- `scope` TEXT NOT NULL  // pc | public | direct
- `scope_id` TEXT NULL   // scope=direct 时必填：建议直接使用 Message.conversation_id（可兼容 1:1 与小群）
- `owner_pc_id` TEXT NULL  // scope=pc 时必填
- `kind` TEXT NOT NULL  // autobiography | relationship | recent_event | secret
- `created_at` TEXT NOT NULL
- `updated_at` TEXT NOT NULL
- `content` TEXT NOT NULL      // 可直接拼进 prompt 的内容（中等长度）
- `summary` TEXT NOT NULL      // 更短，用于预算不足时替代 content
- `subject_type` TEXT NULL     // relationship 可用：pc / npc / topic
- `subject_id` TEXT NULL       // relationship 可用：to_pc_id
- `importance` INTEGER NOT NULL DEFAULT 0  // 人为/模型给的“重要性”
- `score` INTEGER NOT NULL DEFAULT 0       // 遗忘/检索主分值（可从 importance 初始化）
- `access_count` INTEGER NOT NULL DEFAULT 0
- `last_accessed_at` TEXT NULL
- `meta_json` TEXT NOT NULL DEFAULT '{}'   // 来源 refs、关键词、可见性等

一致性规则（MVP 建议）：
- `scope="pc"`：`owner_pc_id` 非空；`scope_id` 为空
- `scope="public"`：`owner_pc_id/scope_id` 均为空
- `scope="direct"`：`scope_id` 非空；`owner_pc_id` 为空
- `kind="secret"`：强制 `scope="pc"`（秘密永不进入 public/direct）

索引建议：
- `(scope, owner_pc_id, kind, score)`（PC 私有检索）
- `(scope, kind, score)`（public 检索）
- `(scope, scope_id, kind, score)`（direct 检索：scope_id=conversation_id，可兼容 1:1 与群聊）
- `(scope, owner_pc_id, last_accessed_at)` / `(scope, scope_id, last_accessed_at)`（衰减/清理）
- `(scope, owner_pc_id, subject_id)` / `(scope, scope_id, subject_id)`（关系）

可选（第二阶段）：
- 使用 SQLite FTS5：`memories_fts(content, summary, scope, scope_id, owner_pc_id, kind, subject_id)`，用于简单关键词匹配（仍属“表格化”，非向量）。

与现有表关系：
- `pc_activity` 继续作为“行动索引”（recall 的兜底与可回放），`memories` 作为“可长期使用的知识/经历库”（含共享池）。

---

## 4. 写入策略（Memory Write）

写入触发点（推荐）：
- PC tick 成功落库之后（`backend/app/tick_runner.py:_apply_action` 完成后），拿到：
  - `action`（create_thread/reply/dm/noop）
  - `refs`（message_id/thread_id 等）
  - 本回合 `inbox_digest.new` / `threads_digest` / `reply_select.thread_context`（若有）
  - 由 `Message.channel` 与参与者（`from_actor` / `to`）决定写入的 scope（public/direct/pc）

写入方式（两阶段演进）：

### 4.1 阶段 A：规则版（先打通闭环）
- A0（最简闭环，但避免“原文入库的重复”）：引入一个**对前端不可见**的“记忆写入 LLM”，把本回合内容提炼成少量结构化记忆再入库：
  - 输入：action + 本回合新增消息（PC 自己发出的 message content、以及（可选）thread_context 摘要）
  - 输出（JSON-only）：`upserts[]`（≤ 1~3 条），每条含 `kind/summary/content/subject_id/importance/keywords[]/ref`
  - scope 决策（MVP 规则）：
    - 若来源 `Message.channel="broadcast"`：写入 `scope="public"`（共享记忆池）
    - 若来源 `Message.channel="direct"`：
      - 写入 `scope="direct"`，并设置 `scope_id = Message.conversation_id`（1:1 与小群统一处理）
      - 若对话参与者不全是 PC（例如 pc↔dm / pc↔user）：依然可以写入 direct（PC 会在同一 conversation_id 下回忆），也可按策略退化为 `scope="pc"`（两者都可接受，先选简单一致的实现）
    - `kind="secret"`：强制 `scope="pc"`（秘密不允许进入 public/direct）
  - 入库约束：
    - 每条 `content` 截断（例如 ≤ 300~500 chars），`summary` 更短（例如 ≤ 80~120 chars）
    - 去重：同一 `ref_type/ref_id` 不重复写；或同 `kind+subject_id+summary` 近似重复时做 upsert 合并
    - `meta_json` 记录来源引用（message_id/thread_id/action_type）与少量 `source_excerpt`（可选，≤ 200 chars）用于回放
- A1（可选增强，仍不做向量）：在 A0 的基础上，让记忆写入 LLM 输出更稳定的 `keywords[]`（用于 recall 的关键词匹配），并把关键词写入 `meta_json`（或单独列）。
- `relationship`：暂时只支持“显式指向”的变化（例如 DM/对话中提到某 PC 且有强情绪词），也可先不做。
- `autobiography/secret`：先不自动生成（避免错误写入），仅提供后端 debug API 手动写入用于验证管线。

### 4.2 阶段 B：隐藏模型版（质量提升，前端不可见）
引入“记忆代理”模型（独立于 PC 行动模型，且对前端不可见）：
- 输入：本回合 action + 新消息片段 + 该 PC 的已知关键自传/关系摘要（少量）
- 输出（JSON-only）：`upserts[]` + `deletes[]` + `relationship_delta[]`（可选）
  - `upserts` 支持“同主题合并/改写 summary/content”
  - `secret` 仅允许在满足明确规则时写入（例如“PC 内心独白/自述秘密”这种结构化来源；否则禁止）

约束：
- 产物必须短、结构化、可验证（避免把长 CoT 写进日志）。
- 单回合写入条数/长度上限（例如 ≤ 3 条，每条 content ≤ 400 字）。

---

## 5. 检索策略（Memory Recall）

目标：在模型决策前，为该 PC 提供少量最相关记忆（来自允许访问的 scope），且不会把 prompt 撑爆。

MVP 接入范围：
- 先仅用于 Round2（`reply_write`）。Round1（`forum_action`）后续再做。

### 5.1 召回输入（query context）
用于粗相关性判断的信号（无需向量）：
- Round2：`reply_select.thread_context` 的关键信号（thread 标题、OP 与 recent_posts 文本片段、出现的角色名）。
- Round2：本回合与该 thread 相关的对象（`thread_id`、`channel_id`、可能的对话对象 pc_id）。
- 兜底：本 PC 最近 `pc_activity`（用于保持连贯与避免重复）。

关键词确认（MVP 先不用 LLM）：
- 使用“确定性关键词”做 `LIKE` 模糊匹配：`thread.title`、参与者 PC 名字、以及（可选）OP/最近楼层中出现频次高的短词。
- MVP：仅关键词匹配召回；不依赖 LLM 生成 query，不做向量检索。
- 可选兜底（若担心“记忆饥饿”）：命中为空时仅补 1~2 条 autobiography/置顶记忆（避免把无关 recent_event 硬塞进上下文）。

scope 选择（MVP 规则）：
- forum reply（Round2：`reply_write`）：召回 `scope="pc"(owner=当前PC)` + `scope="public"`；不召回 `scope="direct"`。
- direct 私聊（后续扩展）：召回 `scope="pc"(owner=当前PC)` + `scope="direct"(scope_id=当前 direct conversation_id)`；通常不召回 `public`（或仅少量作为常识背景）。

### 5.2 命中后排序与截断（MVP）
MVP 先坚持“关键词匹配召回”，因此排序只发生在**已命中**的候选集合里：
- 先按 `score DESC, updated_at DESC` 排序。
- 再按预算截断（条数/字符数）。
- 如需更稳定的覆盖面（可选增强）：再做按 kind 分桶的 top-k（autobiography/relationship/recent_event/secret），但这属于第二步调参，不作为第一版硬依赖。

可选（第二阶段）：
- FTS5 关键词：用 `threads_digest.title + inbox.new` 拼 query，在 `memories_fts` 上做 match，给“命中”额外加分。

### 5.3 “被检索一次 +1”
只要某条记忆被拼进 prompt（无论模型是否真正使用），就视为“检索到”：
- `access_count += 1`
- `score += 1`
- `last_accessed_at = now`

### 5.4 上下文预算（硬上限）
已知约束：记忆拼接进 prompt 的上限为 **最多 8096 tokens**（更准确的全 prompt token 约束可后续补齐）。

MVP 先用更稳的工程约束：
- 以“总字符数”做近似预算（中文 token 与字符近似但不等价，先保守取值）。
- 为每个 kind 分配预算（例如 autobiography 20%、relationship 20%、recent_event 50%、secret 10%）。
- 超出时的降级策略：
  1) 优先丢弃低 score 的条目
  2) `content -> summary`
  3) 再裁剪 summary 到更短（例如 60 字）

---

## 6. 遗忘机制（Decay + GC）

### 6.1 触发时点
- 每 **50 ticks** 执行一次（与用户设想一致）。
- 触发位置建议：
  - 优先做一个 `MemoryJanitor` 后台任务（避免 TickRunner 主流程被慢查询拖住）
  - 若先做最小实现：也可在 TickRunner 单锁内，`turn_no % 50 == 0` 时触发一次（要控制耗时）

### 6.2 衰减规则（MVP）
- 对所有非 pinned 记忆：`score -= k`（k 可配置，默认 1）
- 删除条件：`score < threshold`（threshold 可配置，默认 -3 或 0）
- 保护规则（建议）：
  - autobiography 默认 pinned 或更低衰减
  - secret 不一定 pinned，但衰减更慢/阈值更低（避免“秘密一闪而过”）

### 6.3 审计与安全
建议保留最小审计信息：
- 每次 decay 统计：处理条数、删除条数、耗时（写 `events` 或 `ticks.result_refs` 也可）
- 可提供后端 debug API：手动触发 decay / 查看某 PC 当前记忆列表（前端先不暴露）

---

## 7. 接入点（落地改动位置建议）

需要改动/新增的核心点（按先后顺序）：
- `backend/app/db.py`：新增 `memories` 表（含 scope=pc/public/direct）+ store 方法（按 scope list/upsert/increment_access/decay）
- `backend/app/models.py`：新增 `MemoryEntry` Pydantic 模型（与 store 对齐）
- `backend/app/tick_runner.py`：
  - MVP：在 Round2（`_run_reply_write_round2`）构建 `tick_runner.reply_write` prompt 前：
    - 调用 `recall_memories(pc_id, context)` 得到 `memories_json`
    - 把 `<memories>` 段加入 `render_prompt_messages("tick_runner.reply_write", ...)` 的模板变量
    - 对“被拼进 prompt 的记忆条目”执行 `+1`（access_count/score）
  - 备注：Round1（`_llm_action` / `tick_runner.forum_action`）接入收益更大，但更难控量与防带偏；建议在 Round2 验证后再扩展。
  - 在 `_apply_action` 后触发写入（先规则版；按 `Message.channel` 决定 public/direct/pc）
  - 每 50 ticks 触发一次 decay（或启动独立后台任务）
- `backend/pc_config/prompts.json`：新增 `memories` 段的模板占位与简短规则（禁止长篇复述记忆、避免 CoT）
- `backend/app/settings.py`：增加可配置项（decay_k / threshold / interval_ticks / memory_budget_chars 等）

---

## 8. 落地顺序（建议 1~2 天可演示）

1) Schema：新增 `memories`（含 scope 字段）+ store 最小读写（含索引）。
2) Recall（先 Round2）：`tick_runner.reply_write` prompt 加 `<memories>`，先用 `LIKE` 关键词匹配召回，按字符预算拼接；拼接即 `+1`（可选：无命中时仅补 1~2 条置顶/自传）。
3) Write（最简闭环）：新增“记忆写入 LLM”（对前端不可见），对本回合 action/新消息做提炼后写入 `memories`（按 `Message.channel` 决定 scope；强约束条数与长度，带 ref 去重）。
4) Forget：每 50 ticks 跑一次 decay + 删除，提供 debug 入口验证效果。
5) Write（隐藏模型版，可选）：用独立模型做 upsert/合并与关系/秘密写入。

---

## 9. 验收标准（可演示）

- 每个 PC 都能在后端看到：
  - 自己的私有记忆（scope=pc）
  - 共享记忆池（scope=public）
  - （如有私聊）与对话对象共享的 direct 记忆（scope=direct）
- TickRunner 每回合 prompt 都会带上“少量记忆”，且长度受控（不会无限增长）。
- 被拼进 prompt 的记忆会 `access_count/score +1`。
- 每 50 ticks 触发衰减，低于阈值的记忆会被清理（同时 autobiography/secret 不会被过快清空）。
- 不引入向量化依赖，纯 SQLite + Python 即可运行。

---

## 10. 后续扩展（不属于 improve-v3 但留接口）

- 向量检索：在 `memories` 上加 `embedding` 表或外部向量库，召回阶段替换/融合排序。
- 统一事件系统：`events` 表可作为“公共事件记忆源”，再按可见性分发到各 PC 私有记忆。
- 前端记忆调试器：只读浏览 + 手动 pin/unpin + 手工录入自传/秘密（避免自动误写）。
