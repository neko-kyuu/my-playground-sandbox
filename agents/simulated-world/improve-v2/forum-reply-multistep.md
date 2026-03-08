# Forum reply 多步骤（选贴→抓取→回复）规划

目标：解决 thread 数量与帖子增长后，把 `thread_posts` 全塞进初始上下文导致的注意力涣散/上下文膨胀问题；同时为 `reply` action 引入“先选贴、再看楼层、再回复”的多步骤决策流程。

本规划以 `plan/forum-technique.md` 为参考，并基于当前后端实现快速扫描（`backend/app/tick_runner.py`、`backend/pc_config/prompts.json`、`backend/app/state.py`、`backend/app/llm.py`）。

---

## 0. 当前实现（现状摘录）

### 0.1 TickRunner 的 forum_action（LLM 一步到位）

- 入口：`backend/app/tick_runner.py:_llm_action`
- 目前会构建 `threads_digest`（最多 12 个 thread），并且**为每个 thread 追加 `thread_posts`**（每个 thread 取最近 8 条、每条最多 1200 chars 的 line summary）。
  - 代码上已有 TODO：后续用 function tool 做“按需浏览 thread_posts”（但尚未实现）。
- prompt：`backend/pc_config/prompts.json:tick_runner.forum_action`
  - 强制 **JSON-only**：输出一个 action object（create_thread/reply/dm/noop）。
  - reply 的约束：`thread_id` 必须来自 `threads_digest[].thread_id`。

### 0.2 前端 state 下发（与 LLM 上下文无关，但也会变重）

- `backend/app/state.py:build_state_message` 会给每个 forum thread 下发最近 200 条帖子到 `forum_posts_by_thread`。
- 这主要影响 WS 初始同步性能；与本次“LLM 上下文瘦身”是两个问题，但可在同一阶段顺手做性能分层（后续项）。

### 0.3 LLM 工具能力现状

- `backend/app/llm.py:LlmService.chat(..., tools=...)` 已支持 OpenAI-compatible `tools` 参数。
- 但 `TickRunner._llm_action` 当前传 `tools=None`，且没有 tool-call 循环执行器。

---

## 1. 设计目标与约束

### 1.1 核心目标

- 初始上下文中 **不再附带任何 thread 的楼层内容**（不传 `thread_posts`）。
- `reply` 变为多步骤：
  1) 先决定“想回复哪个 thread（或不回复）”
  2) 再按需抓取该 thread 的关键楼层上下文（首楼 + 最近 N 楼 + 必要的中间楼）
  3) 最终返回符合现有 `ReplyAction` 的 JSON（`{"type":"reply","channel_id","thread_id","content"}`）

### 1.2 兼容性约束（保持最小改动）

- 对外/最终可执行 action schema（`backend/app/actions.py`）优先不动：仍然是 `create_thread/reply/dm/noop`。
- 仍保持 JSON-only（便于后端稳定解析与校验）。
- 多步骤过程允许在后端内部发生（多次 LLM 调用 / 或 tool-call 循环），中间产物可使用 `reply_select`；但最终落库/广播的 action 仍是现有四选一。

### 1.3 顺序（按你的设想：只有选中 reply 才触发第二轮）

建议把 **“第一轮 action”** 明确为“意图决策/选贴”，而不是一步到位写 reply 内容：

1) Round 1：`tick_runner.forum_action`（直接覆盖现有模板）
   - 只能输出：`create_thread` / `reply_select` / `dm` / `noop`（JSON-only）
   - 其中 `reply_select` 是**中间产物**：本轮不直接落库，不走 `validate_action`。
2) 仅当 Round 1 输出 `reply_select`：
   - 后端抓取该 thread 的楼层上下文（首楼 + 最近 N 楼等）
3) Round 2：`tick_runner.reply_write`
   - 输出最终可执行的 `ReplyAction`：`{"type":"reply","channel_id","thread_id","content"}`（JSON-only）

这样就满足“第一轮只选贴/新发帖/私信/轮空；选中 reply 才触发第二轮请求”的预期。

---

## 2. 推荐落地方案（两段式 LLM：选择→写作）

这是最“boring & reliable”的版本：不用 OpenAI tool-calling，也能实现“多步骤思考”的效果；第二步的“抓取楼层”由后端执行。

### 2.1 Round 1：ActionDecider（新发帖/选贴回复/私信/轮空）

输入（上下文瘦身后的 digest）：

- `forum_channels`：频道列表（同现有）
- `threads_digest`：只包含 thread 元信息（**不包含 thread_posts**）
  - 建议字段：`channel_id, thread_id, title, reply_count, last_activity_at, pinned, locked`
- `inbox_digest` / `recall`：同现有

输出（JSON-only）：

- 直接可执行（可走 `validate_action`）：
  - `create_thread` / `dm` / `noop`
- reply 分支（中间产物，不直接落库）：
  - `reply_select`

`reply_select` 示例：

```json
{
  "type": "reply_select",
  "channel_id": "forum_x",
  "thread_id": "forum_x:t123",
  "selection_reason": "一句话（可选）"
}
```

规则：

- 若 thread.locked = true，必须禁止选择（或选择后端会强制回退 noop）。
- `selection_reason` 建议严格限制为“短理由”，避免把长链路 CoT 写进日志（`llm_logs` 会落库 request/response）。

### 2.2 Round 1.5：ThreadContextFetcher（抓取楼层，后端执行）

后端基于 Round 1（`reply_select`）选定的 `thread_id` 拉取上下文，建议统一一个“thread 视图”结构：

- `op_post`：首楼（p1）
- `recent_posts`：最近 N 楼（例如 12）
- `stats`：reply_count、last_activity_at

建议取数策略（最小可行）：

- `op_post`：从 `store.list_messages_by_thread(thread_id, limit=...)` 中取时间最早的那个（或按 id `:p1` 规则定位）。
- `recent_posts`：取最新 N 条；过滤 `conversation_id == channel_id`（避免混入其它会话的同 thread_id）。
- 单条内容做长度裁剪（例如每楼 <= 1200 chars），保持可控 token。

### 2.3 Round 2：ReplyWriter（写回复）

输入：

- PC persona + writing_style（复用现有）
- `selected_thread_digest`（title/reply_count/last_activity_at）
- `thread_context`（op + recent）
- 可选：`recall.new` / `inbox.new`（减少噪音）

输出（JSON-only，现有 `ReplyAction`）：

```json
{
  "type": "reply",
  "channel_id": "forum_x",
  "thread_id": "forum_x:t123",
  "content": "..."
}
```

后端随后走现有 `validate_action` + `apply_action` 落库/广播。

---

## 3. “自主抓取”版本（OpenAI tool-calling，后续增强）

如果希望 Round 1.5 是“模型自主决定要看哪些楼层”，可引入 tool-calling 循环：

### 3.1 工具定义（示例）

- `get_thread_context`：
  - 输入：`thread_id`, `include_op: bool`, `tail: int`, `max_chars_per_post: int`
  - 输出：`{ "thread": {...}, "posts": [...] }`

### 3.2 执行循环（TickRunner 内）

1) 首次 `chat(..., tools=[get_thread_context])`
2) 若返回 `tool_calls`：
   - 后端执行工具，得到结果
   - 追加一条 `{"role":"tool","name":"get_thread_context","content": "<json>"}` 到 messages
   - 再次 `chat(...)`
3) 直到 assistant 返回 JSON action（reply/noop/create_thread/dm）

实现要点：

- 设置 tool-call 最大轮数（例如 2~3），防止死循环。
- 对工具输入做白名单校验（thread_id 必须存在且未 locked）。
- `llm_logs` 会记录 tool-call 的输入/输出，仍建议避免输出长 CoT。

---

## 4. 后端改动清单（对应文件）

### 4.1 必做（最小落地）

- `backend/app/tick_runner.py`
  - 移除 `threads_digest` 里的 `thread_posts` 拼装逻辑（现有 TODO 位置）。
  - Round 1 先调用 `tick_runner.forum_action`：
    - 若输出 `create_thread/dm/noop`：走现有 `validate_action` + `apply_action`
    - 若输出 `reply_select`：抓取 thread_context，进入 Round 2
  - Round 1.5：后端抓取 thread_context（首楼 + 最近 N 楼等）
  - Round 2 调用 `tick_runner.reply_write`，并对其输出走现有 `validate_action` + `apply_action`
- `backend/pc_config/prompts.json`
  - 修改/覆盖 `tick_runner.forum_action`（Round 1：只做意图决策/选贴，不带 `thread_posts`）
  - 新增 `tick_runner.reply_write`（Round 2：基于 thread_context 产出最终 reply JSON）

### 4.2 可选（提高质量）

- `backend/app/db.py` / `SqliteStore`
  - 新增一个 store helper：`get_thread_context(thread_id, channel_id, ...)`，集中做过滤与裁剪，避免逻辑分散在 TickRunner。

---

## 5. 验收标准（可演示）

- TickRunner 生成 `threads_digest` 时不再携带 `thread_posts`，初始 prompt token 显著下降。
- 当 PC 选择 reply：
  - 会触发一次“抓取 thread 上下文”的步骤（后端或工具）。
  - 最终仍产出合法 `ReplyAction`（JSON-only），并能正确落库/更新 thread 元数据。
- 当 thread 很多/很长时，模型不会因为上下文膨胀而频繁跑偏（noop 重复、reply 选错贴等）。
