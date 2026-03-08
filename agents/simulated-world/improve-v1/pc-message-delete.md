# 规划：人类用户删除任意 PC 消息（Message）

## 0. 范围（本次只做）

- **允许人类用户删除任意 `from_actor.kind="pc"` 的消息**（不区分哪个 PC 发的）。
- **先不做**：删除 DM（`from_actor.kind="dm"`）消息、删除用户自己（`from_actor.kind="user"`）消息、以及更细粒度的权限（例如“只能删自己的”）。
- 重要语义：**PC↔PC 私信是双写**，删除时需要**两份一起删**。

## 1. 现状梳理（与删除强相关）

- PC 自动行动写入消息的入口：`backend/app/tick_runner.py::_apply_action()`。
- PC↔PC DM 的双写逻辑：`backend/app/tick_runner.py::_apply_dm()`  
  - 同一条 PC↔PC 私信会写入两条 `messages` 记录：`dm_to_<sender>` + `dm_to_<receiver>`。
  - 两条记录共享同一个 `send_batch_id`（用于关联同批复制消息）。
  - tick 会在 `ticks.result_refs_json` 里记录 message_id / send_batch_id（用于 debug/追溯）。
- PC 活动索引：`pc_activity`  
  - 例如发送 DM 会写 `ref_type="message", ref_id=<msg_sender.id>`（目前只记录发送方那条）。

## 2. 删除语义（建议）

1. 删除请求以 `message_id` 为入口。
2. 后端读取该 message 的 payload，校验 `from_actor.kind=="pc"`；否则拒绝（或直接 no-op 返回 ok，避免前端分支过多）。
3. 若该 message 命中 **PC↔PC 双写私信**：
   - 依据 `send_batch_id` 找到同批次、且 `from_actor.kind=="pc"` 的所有 message（通常是 2 条），**批量删除**。
4. 其它 PC 消息：仅删除该 `message_id`。
5. 删除同时清理派生数据：
   - `pc_activity`：删除 `ref_type="message"` 且 `ref_id in <被删message_ids>` 的记录。
   - `forum_threads` 元数据：若删除的是 forum thread 内的公开帖，需要重新计算 thread 的 `reply_count / last_activity_at`。
   - `ticks`：存在两种策略（需确认选哪种）：
     - A) **保留 ticks**（建议默认）：ticks 作为审计/调试记录，不因消息删除而联动删除；当前系统的“状态下发/前端展示/上下文拼接”都**不读取 ticks**，因此 tick 引用到“已不存在的 message_id”不会导致被删内容再进入上下文，只会影响未来如果做“tick 详情页/后端 debug 输出”时的可读性（需要容错处理引用缺失）。
     - B) **级联删除 ticks（可选）**：删除 `result_refs_json` 中引用了被删 message_id 的 tick（数据量小可用 Python 扫描；更规范的方案是新增 tick↔message 映射表或在 ticks 增加可索引列）。

## 3. 后端改动点（实现顺序）

1. `backend/app/db.py`（SqliteStore）补齐删除能力（建议事务化）：
   - `get_message(message_id)`：读出 message payload + `thread_id / conversation_id / send_batch_id`。
   - `delete_messages_by_ids(message_ids)`（或 `delete_messages_by_send_batch_id(send_batch_id)` + 过滤 from_actor.kind）。
   - `delete_pc_activity_by_message_ids(message_ids)`。
   - `rebuild_forum_thread_meta(thread_id)`：基于剩余 messages 重新计算并 upsert。
2. API 层（任选其一，倾向 WebSocket 以匹配现有交互）：
   - WebSocket：扩展 `WsClientToServer` 增加 `delete_message`；服务端执行删除后 `broadcast` 一个 `message_deleted`（包含被删的 message_ids + 可选 send_batch_id）。
   - 或 HTTP：`DELETE /api/messages/{message_id}`；前端删完后 `request_state` 刷新。
3. 兼容性与幂等：
   - 重复删除同一条 message：返回 `{ok: true}`（避免前端并发/多端同步导致错误）。
   - 删除 forum 帖时同步广播 thread 元数据（或让前端二次 `request_state`）。

## 4. 前端改动点（实现顺序）

1. `frontend/src/types.ts`：
   - 扩展 `WsClientToServer`：`{ type: "delete_message"; message_id: string }`（若走 WS）。
   - 扩展 `WsServerToClient`：`{ type: "message_deleted"; payload: { message_ids: string[] } }`（以及可选 thread 变更）。
2. UI：
   - 在消息气泡旁加删除按钮（仅对 `from_actor.kind==="pc"` 显示；本次无权限限制所以不区分 PC）。
   - 删除后前端更新本地状态：从 `messagesByConv`、`forumPostsByThread` 移除；并更新 thread `reply_count/last_activity_at`（或简单 `request_state` 全量刷新以减少易错分支）。
3. PC↔PC DM 双写：
   - 若后端返回/广播多个被删的 message_ids，前端按 ids 批量移除。
   - `dmTargetsByBatchId` 如出现“批次已无消息”的情况，可选择延迟到下一次 `request_state` 时重建（最省心）。

## 5. 最小验证清单

- 删除 forum thread 内某条 **PC 发言**：帖子消失，thread 回复数与最后活跃时间合理更新。
- 删除 PC↔PC 私信任意一侧的那条：两边 inbox 都消失（2 条一起删），且相关 `pc_activity` 被清理。
- 删除不存在的 message_id：接口返回 ok，不影响其它数据。
- `python3 -m compileall backend`（最小后端验证）。
