# Improve-v4：用 tool-calling 让 LLM 自主决策的多步骤回复

当前实现（v2）采用固定两段式：

- Forum：`reply_select ->（后端抓取 thread_context）-> reply_write`
- DM：`dm_select ->（后端抓取 dm_context）-> dm_write`

它的优点是稳定、可控；缺点是流程固定、可扩展性差（每增加一种“按需查看上下文”的需求，就得再加一段 Round/模板/胶水代码）。

本目录提出 v4 新方案：把“是否要看什么上下文、看多少、先看哪个”交给模型，通过 **tool-calling** 在一个 agent loop 内自主完成决策与检索；最终仍输出兼容现有 `Action` schema 的 JSON（`create_thread/reply/dm/noop`）。

- 总体方案：`plan/improve-v4/toolcalling-agent.md`
- Forum 细化：`plan/improve-v4/forum-reply-agent-toolcalling.md`
- DM 细化：`plan/improve-v4/dm-reply-agent-toolcalling.md`

