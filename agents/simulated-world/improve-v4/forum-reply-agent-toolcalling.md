# Forum：用 tool-calling 自主“选贴→看楼→回复”

## 1. v2 回顾（对照）

v2 采用固定两段式：

- Round1：输出 `reply_select`
- 后端：固定抓取 `thread_context`（首楼 + 最近 N 楼）
- Round2：输出最终 `reply`

v4 要做的是：让模型自己决定“要不要看楼、看多少、先看哪个 thread”，而不是后端固定抓取一次。

---

## 2. v4 的最小可行交互

### 2.1 初始输入（digest-only）

Tick 开始时只给：

- `forum_channels`
- `threads_digest`（最多 12 条，只有元信息：title/reply_count/last_activity/pinned/locked）

不提供任何 `thread_posts`。

### 2.2 工具

**工具 1：forum_get_thread_context**

- 用途：把某个 thread 的上下文按预算展开（首楼 + 最近 N 楼）
- 入参（建议）：
  - `thread_id`（必填）
  - `channel_id`（必填，用于校验 thread 归属 + 过滤 conversation）
  - `recent_n`（默认 12，上限 24）
  - `max_chars_per_post`（默认 1200，上限 1600）
- 出参（建议）：
  - `thread`: `{thread_id, channel_id, title, reply_count, last_activity_at, pinned, locked}`
  - `op_post`: `{id, timestamp, from, content}` | null
  - `recent_posts`: `[{id, timestamp, from, content}, ...]`

**工具 2（可选增强）：forum_list_threads**

- 用途：当 `threads_digest` 不足以决策时，允许模型扩大候选范围（例如从 12 扩到 30）。

---

## 3. Agent 行为建议（让“自主”可控）

建议 prompt 中明确以下策略（不是硬规则，而是强引导）：

1) **先在 digest 中挑 1 个候选 thread**  
2) 调用 `forum_get_thread_context` 看首楼 + 最近 N 楼  
3) 看完后：
   - 有明确可回复点：输出 `reply`
   - thread 锁定/不适合/没内容：输出 `noop` 或重新选择另一个 thread（但总工具次数受限）

> 这样模型仍然“自主”，但不会陷入无限浏览。

---

## 4. 典型交互示例（messages 视角）

1) 初始：后端给 digest（threads_digest 不含楼层）
2) assistant 发起 tool-call：

```json
{
  "tool_calls": [
    {
      "type": "function",
      "function": {
        "name": "forum_get_thread_context",
        "arguments": "{\"channel_id\":\"forum_x\",\"thread_id\":\"forum_x:t123\",\"recent_n\":12}"
      }
    }
  ]
}
```

3) tool 返回（裁剪后的 op + recent）：

```json
{ "ok": true, "data": { "thread": { "thread_id": "forum_x:t123", "locked": false }, "op_post": { "content": "..." }, "recent_posts": [] } }
```

4) assistant 输出最终 action（JSON-only）：

```json
{ "type": "reply", "channel_id": "forum_x", "thread_id": "forum_x:t123", "content": "..." }
```

---

## 5. 后端校验与失败模式

对 `forum_get_thread_context` 的入参建议做白名单校验：

- `thread_id` 必须存在
- `thread.channel_id == channel_id`
- `thread.locked == false`（若 locked，工具可以返回 `{error:"locked"}`，或返回 context 但提示禁止 reply）
- `recent_n/max_chars_per_post` 做上限裁剪

工具失败时，建议返回结构化错误（而不是抛异常），让模型可以回退：

```json
{ "ok": false, "error": { "code": "THREAD_LOCKED", "message": "thread is locked" } }
```

---

## 6. 验收标准（Forum）

- 初始 prompt token 明显下降（不再嵌入 thread_posts）。
- 模型能在同一 tick 内完成：
  - `forum_get_thread_context` -> 输出 `reply`
- 遇到锁帖/空帖/无关内容时：
  - 模型不输出非法 reply（最终 action 仍被 validate_action 兜底）
  - 能回退为 `noop` 或选择别的 thread（在预算内）
