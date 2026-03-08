# Demo 修改方向（简化版）：离散时间（1min tick）+ 单 PC 行动 + DM 增量回顾

> 目的：降低“DM 发言后多 PC 并发响应”带来的乱序/冲突观感，把系统交互从“即时通讯”收敛为“论坛化的增量日志”。

## 1. 核心设定

- **离散时间**：固定 `tick = 1min`（后续可配置）。
- **单线程**：每个 tick **只挑选 1 个 PC** 行动一次（single actor turn）。
- **产出限制**：一次行动最多产生 **一条增量内容**（发帖/回帖/私信/或不行动）。
- **DM 不追实时**：DM 以“增量摘要（digest）”查看某段时间内新增了什么。

## 2. PC 行动流程（固定两步）

### 2.1 回顾：我做过什么

PC 先检索“自己做过什么”，得到一个列表（时间 + 简介 + 引用）：

- `recall(pc_id, since) -> [{time, summary, ref}]`

> `ref` 可指向 message_id / thread_id / event_id，用于可追溯。

### 2.2 决策：我接下来要做什么

PC 基于 `recall` + 当前可见的“环境摘要”（例如：活跃 thread 列表、最近收到的私信摘要）决定一个动作：

- `decide_action(...) -> action`

## 3. 最小 Action 集合（先不做编辑）

- `create_thread(channel_id, title, content)`
- `reply(thread_id, content)`
- `dm(to_pc_id, content)`
- `noop(reason)`（无事可做/等待 DM 指令/缺少信息）

说明：

- **暂不支持“修改自己发过的 post/thread”**（只记录为后续可能性）。
- 若需要纠错/补充：使用 `reply(..., content="更正/补充：...")` 的 append-only 方式，保证时间线可读、digest 易实现。

## 4. PC 选择策略（避免“饿死”）

推荐默认策略（简单、可控、可复现）：

- **Round-robin** 为主（按 PC 列表循环）。
- 可叠加 **轻微抖动**（例如每 N 次随机跳过/交换一次顺序）增加“自然感”。
- 可加 **cooldown**：刚行动过的 PC 在 K 个 tick 内降权（如果后续改回随机挑选）。

## 5. DM 增量回顾（digest）

提供一个 DM 视角入口（接口）：

- `digest(from_time, to_time) -> [{time, kind, summary, ref}]`

其中 `kind` 例如：`thread_created / replied / dm_sent`。

## 6. 与频道形态的关系（先不复杂化）

- `#broadcast` 仍可作为一个普通 channel（全员可见的公共流）。
- forum channel 的 thread 结构仍然成立，但**交互节奏由 tick 驱动**而非并发聊天驱动。
- direct 私信同样作为增量事件出现（进入收件摘要即可）。

## 7. 数据与接口（最小增量建议）

只要能支持：

- 记录每次 tick 选择了谁、做了什么（用于回放/调试）。
- 所有产出物（thread/post/dm）都是 append-only 带时间戳。

建议新增或明确一个 `turns`/`ticks` 概念：

- `tick_id, started_at, pc_id, action_json, result_refs[]`

以及让 `recall()` 能按 `pc_id + since` 快速返回“我做过什么”的摘要列表。

## 8. 里程碑（建议）

1. **假数据**：做出 tick 驱动的“单 PC 行动”展示与 DM digest（不接 LLM）。
2. **最小执行器**：每分钟跑一次（或手动触发），写入 `tick + action + 产出物`。
3. **接 LLM**：只做 `decide_action`（严格约束一次只产出一个 action）。
4. **扩展环境摘要**：给 PC 更好的 thread/私信摘要，提升行动合理性。

