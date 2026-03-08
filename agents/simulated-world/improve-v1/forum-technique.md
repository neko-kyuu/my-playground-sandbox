# Forum 技术方案（后端）：离散时间（1min tick）单 PC 行动 + DM 增量回顾

本文基于对当前后端实现的快速梳理（`backend/app/main.py`、`backend/app/engine.py`、`backend/app/db.py`、`backend/app/llm.py`），提出一份更贴近“论坛化增量日志”的简化技术方向。

## 0. 现状速览（当前代码在做什么）

### 0.1 通信与状态同步

- 后端入口：FastAPI + WebSocket（`/ws`）。
- 连接建立时，服务端会下发一次全量 state：conversations + 每个 conversation 最近 200 条消息；forum 频道还会附带 threads 列表与每个 thread 最近 200 条帖子（`backend/app/main.py:_send_state`）。
- 后续新增消息通过 WS 广播 `{"type":"message"}` 推送（`backend/app/ws.py`）。

### 0.2 “用户注入 → DM → PC 反应”的执行模型

- `user_inject`：
  1) 写入一条 user→DM 的 Message（可带 `thread_id` / `send_batch_id`）。
  2) 写入一条 DM 消息（broadcast 或复制到每个 `dm_to_<pc_id>` 会话）。
  3) 对 broadcast：**为所有 PC enqueue reaction**；对 direct：为目标 PC enqueue reaction（`backend/app/main.py`）。
- `DemoEngine`：
  - 内部有一个 `asyncio.Queue[Job]`，并启动 `n = len(pcs)` 个 worker 并发消费（`backend/app/engine.py:start/_worker`）。
  - 每个 job 生成一条 PC Message 并写入 DB，再 WS broadcast（`backend/app/engine.py:_worker`）。
  - 这就是“DM 发言后 PC 并发刷屏”的根源。

### 0.3 数据层（SQLite）

- `messages` 表有可查询列：`conversation_id/timestamp/thread_id/send_batch_id`；但 `from_actor/to/content` 等都在 `payload` JSON 里（`backend/app/db.py`）。
- `forum_threads` 表存 thread 元数据（last_activity/reply_count 等字段也在 payload 中，只有 `channel_id/created_at` 为列）。
- 目前 thread 的 `last_activity_at/reply_count` 主要在 `forum_post` 与 `user_inject` 的路径里更新；PC 在 thread 中发帖时（engine worker 写 message）不会同步更新 thread 元数据（可读性/排序会受影响）。

## 1. 问题与目标（为什么要改）

### 1.1 当前痛点

- **阅读冲突**：N worker 并发生成导致消息乱序、刷屏、难读。
- **行为模型偏“即时通讯”**：PC 只能“被动反应”，很难自然演进成“论坛化的增量日志”。
- **回顾/摘要困难**：`messages.payload` 中的 actor 信息难以做索引级检索；要做 PC 的“我做过什么”往往需要扫消息并解析 JSON。
- **状态下发会越来越重**：每次连接都全量下发最近 N 条；thread/消息增长后会变慢（但这属于后续优化，不必第一步解决）。

### 1.2 目标（对齐简化版方向）

- 固定 `tick=60s`，每个 tick **只允许一个 PC 行动一次**（single actor turn）。
- PC 行动遵循固定流程：`recall -> decide_action -> apply_action`，一次最多产出一条增量内容。
- DM 用 `digest(from,to)` 查看一段时间内新增的内容（thread/reply/dm），不追实时流，随后可选择在`#broadcast`频道发言总结。
- 暂不做“编辑既有 post/thread”（append-only）。

## 2. 后端方案概览（最小可落地架构）

建议新增一个与 `DemoEngine` 并行/逐步替换的“离散时间执行器”：

- `TickRunner`（后台任务）
  - 周期性触发（60s）或可手动触发。
  - 全局单锁：保证同一时刻最多一个 tick 在运行（避免重入/重叠）。
  - 选 PC：默认 round-robin（可持久化 cursor，重启不乱）。
- `TurnContextBuilder`（构建 PC 决策上下文）
  - `recall(pc_id, since)`：回顾“我做过什么”。
  - `inbox_digest(pc_id, since)`：回顾“我收到什么”（可选）。
  - `threads_digest(...)`：提供活跃 thread 列表摘要（可选，先少量字段，或至少摘要）。
- `ActionDecider`（LLM 或 fake）
  - 只输出一个 action（JSON）。
- TODO: 后续优化。如果是reply，则需要额外一步工具调用。
  - 首先`ActionDecider`的action（JSON）会返回选择的thread_id。
  - 随后抓取该thread首楼+最近n条内容，PC将根据具体详情，决定reply的文本具体内容。
- `ActionApplier`（把 action 落库并 WS 推送）
  - create_thread / reply / dm / noop
  - 统一处理 “写 message + 更新 thread 元数据”。

> 关键点：执行器模型从“DM 触发并发反应”变为“时间驱动 + 单 PC 行动”，阅读冲突自然消失。

## 3. 数据结构建议（为 recall / digest 做索引）

### 3.1 新增表：`ticks`（审计与可回放）

用途：记录每个 tick 选择了谁、做了什么、产生了哪些引用。

建议字段（最小）：

- `id`（uuid）
- `started_at`（iso）
- `pc_id`
- `action_json`（决策输出，json string）
- `result_refs_json`（message_id/thread_id 等引用列表）
- `status`（running/done/failed）
- `duration_ms`（可选）

### 3.2 新增表：`pc_activity`（给 recall 用的“摘要索引”）

用途：不用扫描 `messages.payload`，直接按 `pc_id + time` 查询“我做过什么”。

- `id`（uuid）
- `pc_id`
- `timestamp`
- `kind`（thread_created/replied/dm_sent/noop…）
- `summary`（短摘要，80–200 字）
- `ref_type/ref_id`（message/thread）

写入策略：

- 在 `ActionApplier` 成功落库后同步写一条 `pc_activity`。
- 不需要对历史消息做一次性回填；缺了就从“有表之后”开始可用（符合 demo 增量哲学）。

### 3.3 修复/收敛：Thread 元数据更新应“集中化”

建议把“写一条 thread 内的 message”收敛成一个 store 方法，例如：

- `append_message(channel_id, thread_id, message)`：
  - 写入 `messages`
  - 计算并更新 `forum_threads.last_activity_at/reply_count`

避免目前分散在 `main.py` 的路径里，且 engine worker 不更新的问题。

## 4. Tick 执行循环（伪代码）

```text
every 60s:
  if paused or already_running: return
  pc = pick_next_pc_round_robin()

  recall = store.list_pc_activity(pc_id, since=last_pc_turn_at)
  inbox = store.list_messages(dm_to_pc_conversation, since=last_pc_turn_at)  (optional)
  threads = store.list_forum_threads(topN)                                   (optional)

  action = llm.decide_action(pc_persona, recall, inbox, threads)
  validate(action)

  refs = apply_action(action)  # write messages/threads, broadcast ws events
  store.add_tick_record(...)
  store.add_pc_activity_summary(...)
```

### 4.1 “选 PC”建议（可复现 + 不饿死）

- 默认 round-robin：在 `kv_settings` 存 `tick_cursor_pc_index`。
- 后续如果想“随机感”，可以在 RR 基础上加入轻微抖动，但仍保持“不会饿死”。

### 4.2 “一次只产出一条内容”的约束

- 在 action schema 上硬约束：只能返回单一 action。
- 在 applier 上硬保护：一次 tick 最多写入一个 message（create_thread = 1 个 thread + 1 条首帖，仍可视作“一个产出物”）。

## 5. API / WS 协议建议（最小改动优先）

### 5.1 控制面

- `pause/resume` 已有：可复用来暂停 tick runner。
- 建议新增（后续）：
  - `tick_now`：手动触发一次 tick（便于 demo/调试）。
  - `get_tick_state`：查看是否 running、下一次 tick 时间、cursor 等。

### 5.2 DM digest

实现上可以先用 `messages.timestamp` 做范围过滤，再在服务端生成摘要（例如取 content 前 N 字，附上 conversation_id/thread_id/from_actor）。
DM 查看一段时间内新增的内容（thread/reply/dm），随后可选择在`#broadcast`频道发言总结。

## 6. 与现有 `user_inject`/并发模型的兼容策略（逐步替换）

建议分两阶段，避免一次性大改：

### 阶段 A（并存）

- 保留 `user_inject` 写入 user_msg + dm_msg 的逻辑不变。
- **停止自动 enqueue 全体 PC reaction**（仅对 `#broadcast` 保留）。
- PC 的“回应/行动”交给 tick runner 在后续若干分钟内逐步产生。

### 阶段 B（收敛）

- `DemoEngine.enqueue_pc_reaction/_worker` 逐步退场，PC 发言统一走 `ActionApplier`。
- 统一将“PC 发言”视为 action 的结果，而不是即时反应消息。

## 7. 风险与注意事项

- **LLM 调用时长 > 60s**：tick runner 必须有单锁与“错过即跳过/不重叠”的策略；必要时把 tick 触发改成“上一次结束后延迟 60s”。
- **DB 索引不足**：不建议在 demo 早期就去解析 `messages.payload` 做复杂检索；先靠 `pc_activity` 表解决 recall 的可用性。
- **协议扩展**：前端目前只处理 `state/message/typing/queue/error`；如果加 digest/tick_state 事件，需要同步改前端（可后置）。

## 8. 建议落地顺序（1–2 天可出可演示版本）

1. 新增 `ticks/pc_activity` 表 + store API。
2. 新增 `TickRunner`，demo_fake 下先用 deterministic action（create_thread / reply / dm / noop）打通写库与 WS 推送链路。
3. 补齐 thread 内发帖的元数据更新（集中化 append）。
4. 接入 LLM `decide_action`（JSON schema + 校验），保持“一次一条”。
5. 加 `digest` 接口，DM 能快速浏览“过去 10 分钟发生了什么”。

