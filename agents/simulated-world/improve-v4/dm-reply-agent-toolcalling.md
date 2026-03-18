# DM：用 tool-calling 自主“选人→看历史→写私信”

## 1. v2 回顾（对照）

v2 的固定两段式：

- Round1：输出 `dm_select`（只选对象）
- 后端：固定抓取该对象 `dm_context`（近期往来 N 条）
- Round2：输出最终 `dm`

v4 的目标：让模型自己决定“需要看哪段私信历史/看多少/是否需要查记忆”，而不是后端固定抓一次。

---

## 2. v4 的最小可行交互

### 2.1 初始输入（digest-only）

Tick 开始时只给：

- `inbox_digest`（按 peer 聚合、每个 peer 只给 1~2 条最近摘要）
- `pc roster`（pc_id->name 的映射，或最小身份信息）

不提供任意一个 peer 的完整往来历史。

### 2.2 工具

**工具 1：dm_get_peer_context**

- 用途：按预算展开与某个 peer 的近期往来
- 入参（建议）：
  - `pc_id`（当前行动者）
  - `peer_kind`: `"pc" | "dm"`
  - `peer_id`: `"pc_xxx"` 或 `"dm"`
  - `recent_n`（默认 24，上限 48）
  - `max_chars_per_message`（默认 800，上限 1000）
- 出参（建议）：
  - `peer`: `{kind,id,name,scope_id}`
  - `scope_id`（用于 direct memory scope）
  - `recent_messages`: `[{id,timestamp,from,to,content}, ...]`

**工具 2（可选增强）：dm_list_inbox**

- 用途：当初始 inbox_digest 过短/不全时，允许模型刷新/扩大 inbox 候选范围。

**工具 3（建议纳入）：memory_search**

- 用途：当私信需要依赖“长期关系/约定/秘密”时，让模型主动按关键词查 direct/pc 记忆，而不是每轮强塞。

---

## 3. Agent 行为建议（让“自主”更像人）

建议 prompt 中引导：

1) 先根据 `inbox_digest` 选择一个最需要回应的对象  
2) 调用 `dm_get_peer_context` 查看近期往来  
3) 若需要“更长期事实”（例如对方身份、以前承诺、敏感边界），再调用 `memory_search`  
4) 最终输出 `dm` 或回退 `noop`

预算建议：

- 默认只允许看 1 个 peer 的 context（避免“把所有人都展开”）
- 若第一次选择不合适，可允许再看第 2 个 peer，但受 `max_tool_rounds` 限制

---

## 4. 典型交互示例（messages 视角）

1) 初始：后端只给 inbox_digest（每个 peer 1~2 条摘要）
2) assistant 发起 tool-call 展开某个 peer 的历史：

```json
{
  "tool_calls": [
    {
      "type": "function",
      "function": {
        "name": "dm_get_peer_context",
        "arguments": "{\"pc_id\":\"pc_1\",\"peer_kind\":\"pc\",\"peer_id\":\"pc_2\",\"recent_n\":24}"
      }
    }
  ]
}
```

3) tool 返回（裁剪后的 recent_messages）：

```json
{ "ok": true, "data": { "peer": { "kind": "pc", "id": "pc_2", "name": "小明" }, "recent_messages": [ { "from": "小明", "content": "..." } ] } }
```

4) assistant 输出最终 action（JSON-only）：

```json
{ "type": "dm", "to_pc_id": "pc_2", "content": "..." }
```

---

## 5. 后端校验与失败模式

对 `dm_get_peer_context` 的入参建议做白名单校验：

- `pc_id` 必须是当前 tick 的行动者
- `peer_kind=pc` 时：
  - `peer_id` 必须存在于 pcs 列表
  - `peer_id != pc_id`（不能私信自己）
- `peer_kind=dm` 时：
  - `peer_id` 只能是 `"dm"`
- `recent_n/max_chars_per_message` 做上限裁剪

工具失败时返回结构化错误，模型可回退为 `noop`：

```json
{ "ok": false, "error": { "code": "PEER_NOT_FOUND", "message": "peer not found" } }
```

---

## 6. 验收标准（DM）

- 初始 prompt 不包含完整 DM 历史，仅有按 peer 聚合摘要。
- 模型能在同一 tick 内完成：
  - `dm_get_peer_context` -> 输出 `dm`
- 不会私信自己、不会把 DM 发给未选择/不存在的对象（最终仍由 validate_action 兜底）。
