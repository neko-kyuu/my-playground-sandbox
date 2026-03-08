# Demo v0 范围（Discord 风格 DM × PCs）
已暂停，部份需求有修改。

## 1. 定位

- 原型/学习性质 demo：先做可跑通的“DM（系统模型）对话驱动 + 多 PC 反应 + 合并结算”的最小闭环。
- 世界模拟（tick/日程/关键事件/地图演进等）延后，后续再模块化组装。

### 1.1 计划
- 先用假数据跑通流程
- 接入openai兼容API

## 2. 术语与称呼（避免歧义）

- **DM**：系统模型（类似 Discord 的 bot），职责类似跑团 DM：世界事务、对 PC 的系统性引导、以及“自然语言 → 结构化”的翻译官，对接人类用户。
- **PC**：扮演具体角色的模型（每个 PC=一个模型）。

## 2. 核心用户流程（最小闭环）

1. 用户向 DM 提交自然语言输入（注入指令/剧情/话术等）。
2. DM 把用户输入格式化为“广播消息”或“私聊消息（`channel=direct`）”，并发送给目标 PC（广播=全体可见；私聊=仅受众可见）。
3. PC 读取可见消息，产出反应：
   - 自然语言回复（用于 UI 展示）
   - 结构化对话结果（最小：`relationship_delta[]`、`info_shared[]`）
4. Resolver 合并多 PC 反应并结算（由系统自动触发，不要求用户手动点“结算”）：
   - 写入事件流 JSON
   - 更新角色状态（关系/记忆/HP 等）

## 3. 必须有（Must-have）

### 3.1 Web UI（Discord 风格）
- 布局：
  - 左侧：频道/会话列表（Discord 风格）
  - 右侧：消息流（滚动查看历史）
- 主消息样式（broadcast 与 direct 通用）：
  - 左：头像
  - 右：第一行 `PC 名字 + 时间戳`（格式：`yyyy-mm-dd hh:mm` / `昨日 hh:mm` / `hh:mm`）
  - 右：第二行起为消息内容（支持多行）
- 输入：
  - 用户注入输入框（发给 DM）
  - 目标选择：`#broadcast` 或 1..N 个 PC（direct）
  - 当选择多个 PC 时，DM 发送的 direct 消息会被**复制**到各自的 `DM → PC` 会话（可用 `send_batch_id` 关联同一批次）
  - 发送后由 DM 决定产出 `channel=broadcast|direct` 的消息并投递到对应会话
- “正在输入…”提示：
  - 当 1..N 个 PC 请求正在发送/PC 正在生成回复时，会话底部显示 `... + <PC 名> 正在输入…`
- 会话与频道：
  - 频道分两类：
    - **纯聊天（Chat）**：当前形态，所有对话都直接发生在频道消息流中。
    - **论坛（Forum）**：频道下包含多个 thread；对话发生在 thread 内。
  - `#broadcast`：闲聊/广播频道（全员可见），**固定存在且不可删除**
    - 用途偏“即时聊天/随手说”，不强调 thread 组织
    - 当前 demo 中 PC 的默认广播也在这里发生
  - 论坛频道（`kind="forum"`，**不要求 id 固定为 `"forum"`**）：
    - 表达一种“主题/相关性集合”，例如 `#trade` 聚合交易相关内容
    - 论坛频道的创建/管理：当前先由**人类用户在设置界面**控制（DM 是否可创建论坛频道待定）
    - 论坛频道下的 thread 由 DM 创建（LLM 接入后），thread 为广播形式（全员可见）
    - thread 的公开贴只能落在对应的 `kind="forum"` 论坛频道中
  - direct：私聊会话列表（`channel=direct`）
    - **DM→PC direct**：每个 PC 一个会话；DM 对多个 PC 私聊时复制投递到各自会话（避免会话数失控）；频道命名格式`@pc名`
    - **PC↔PC direct**：允许 PC 之间自由发生私聊社交，与DM私聊PC的频道共用；复制投递到各自会话（避免会话数失控），如`Alice`和`Bob`的会话投递到`@Alice`和`@Bob`频道
    - 私聊频道需标注 from:..（入信）/ to:..（出信）
  - 现有的 `#broadcast` 与私聊频道保持为 **纯聊天**（现状）
  - 通过**图标**区分频道是纯聊天还是论坛
  - 论坛频道交互（先做 UI + 假数据）：
    - 点击论坛频道后，右侧默认展示 **thread 列表**
    - 点击某个 thread 后，右侧变为 **两列布局**：左侧 thread 列表 / 右侧 thread 详情
    - thread 详情支持切换到**完整视图**（收起 thread 列表，仅显示详情）
  - thread 由谁创建：
    - 由 DM 自主创建
    - thread 只能是**广播形式**（全员可见）
    - 以上创建逻辑需要接入 LLM 后再做；当前 demo 阶段只用假数据展示，不讨论技术细节
  - 发言权限与路由（关键约束）：
    - **PC 只有发言权限**（不创建频道/不创建 thread）
    - PC 在哪里发言由 DM 决定：**DM 在哪里广播，PC 就在哪里发言**
      - DM 广播到纯聊天频道 → PC 回复到该纯聊天频道消息流
      - DM 广播到论坛频道的某个 thread → PC 回复为该 thread 下的帖子
- 控制：
  - 暂停/继续（用于配合全局限流排队；排队时引擎整体暂停）
  - “收集反应/结算”由系统自动触发

### 3.2 模型与调用
- 接入：OpenAI 兼容格式 API
- 需要支持：function calling（用于产出结构化反应、结构化对话结果、事件写入等）
- 频控：全局限流 + 排队等待；排队时引擎整体暂停（不丢请求）

### 3.3 数据与日志
- 消息（Message）最小字段：
  - `id, timestamp, channel(broadcast|direct), from(dm|pc:<id>), to[] (dm时), content`
  - 论坛相关扩展字段：
    - `thread_id?: string`：消息归属的 thread（可用于“删除 thread 时级联删除该 thread 产生的私聊/公开贴”）
    - 约束：公开贴仅出现在 `kind="forum"` 的频道（并带 `thread_id`）；同一 thread 可派生出 direct 消息副本（同样带 `thread_id`）
    - 删除策略（只删 thread，不删频道）：按 `thread_id` 删除跨会话消息；频道（含 `#broadcast`）不支持删除
- 事件（Event）最小字段：
  - `timestamp, location, pc, type, summary, visibility(public|private), consequences`
- 对话记录：
  - 控制台保存完整 transcript（用于查看与回溯）
  - 推理时不保证每次携带完整历史（窗口化 + 记忆检索拼接）

### 3.4 PC 能力（最小）
- 属性：`social/mobility/labor/combat` 各 `0–100`
- 战斗：HP 固定初始值；回合制；动作仅 `attack/retreat`（防御/闪避被动）
- 结构化对话结果（最小）：
  - `relationship_delta[]`
  - `info_shared[]`

## 4. 用户注入（Demo）

- 用户以自然语言输入（发给 DM）
- 由 DM 抽取字段并广播/私聊给受影响 PC：
  - `content, location, affected_characters, visibility`

## 5. 非目标（Not in demo v0）

- 完整世界模拟闭环（tick/日程/关键事件日更/地图移动耗时等）
- 复杂冲突系统细则（冲突识别、排序、可复现随机种子规则等细节延后）
- 传闻失真概率/可信度字段
- 记忆容量硬上限（仅控制检索拼接 token 上限）

## 6. 约束与默认值（当前）

- 初始 PC 数：4（后续可控上限不超过 20）
- 检索拼接预算：每次最多 8096 tokens（总上下文上限随具体模型配置自动适配）
- canon：外部 Markdown，通过 MCP 语义检索（参考 `MCP/obsidian_graphrag_mcp/README.md`）

## 7. 开放问题（后续再定）

- 合并结算的具体规则（对话结果如何影响关系/记忆/行动/战斗）
- Action schema（按类型区分字段）与 JSON Schema 细则
- 记忆遗忘参数：`k` 与删除阈值
- 多 PC 同时发言的 UI 呈现与节奏（是否需要“逐个显示”或“合并摘要”）

## 8. 技术栈与交付形态（PC 端）

### 8.1 结论

- 后端：Python（FastAPI）+ WebSocket（用于消息流与“正在输入…”状态）
- 存储：本地 `SQLite`（事件流/消息/会话/索引元数据）+ 可导出 `JSON`
- 前端：React（Vite）+ 路由/状态管理（轻量即可）
- 配置：本地 `config.json` 或环境变量（OpenAI 兼容 `base_url/api_key/model`）
- 包管理：python侧 `uv`，前端 `npm`
