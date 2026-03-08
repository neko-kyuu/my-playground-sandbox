# Improve-v3：记忆全量修改/删除计划（仅方案，不实施）

本文只讨论一个前提下的后续方案：

- **前端允许修改已有记忆**
- **前端允许删除已有记忆**
- **适用范围扩大到所有 `scope`（`pc/public/direct`）与所有 `kind`（`autobiography/relationship/recent_event/secret`）**

本文件是计划稿，**在确认前不进入开发实现**。

---

## 1. 背景与问题

当前调试器的能力是偏保守的：

- 浏览全部记忆
- `pin/unpin`
- 手工录入 / 修改 `pc` 范围下的 `autobiography/secret`

如果要把“可编辑范围”放开到所有 scope / kind，会带来新的系统性问题：

1. **一致性风险变高**
   - `public` / `direct` 记忆会被多个 PC 共用或间接共用
   - 修改一条共享记忆，本质是在改“多个后续 prompt 的共同事实”

2. **来源链会被打断**
   - 当前记忆很多来自消息提炼或隐藏模型合并
   - 一旦允许人工改写/删除，后续需要区分“模型生成的原始结论”与“人工修订版”

3. **merge / rewrite 逻辑会更复杂**
   - 记忆已经支持隐藏模型 upsert/合并
   - 若用户手工改了某条记忆，后续模型再写入时，是否允许覆盖、如何避开误覆盖，需要明确规则

4. **删除语义不再只是 UI 行为**
   - 删除一条 `recent_event` 比较直接
   - 删除 `relationship` / `autobiography` / `secret` 可能影响角色长期稳定性
   - 删除 `public/direct` 还涉及共享信息的撤销语义

因此，这不是单纯“开放编辑表单”的前端工作，而是一次**记忆生命周期与权限语义的补全**。

---

## 2. 目标与非目标

### 2.1 目标

- 在调试器中**查看、修改、删除任意记忆**
- 对所有 `scope` / `kind` 提供统一入口
- 保留足够的审计/元信息，避免“改完之后不知道改了什么”
- 不破坏现有 recall / decay / hidden writer 主流程

### 2.2 非目标

- 不在这一轮解决复杂权限系统（如多用户、RBAC）
- 不在这一轮做“版本回滚 UI”
- 不在这一轮做“批量编辑 / 批量删除”
- 不在这一轮做“软删除恢复站”完整产品形态（可先留后端字段）

---

## 3. 设计原则

1. **先做可审计，再做可编辑**
   - 在允许全量修改前，必须补足来源与人工操作标记

2. **人工修改优先级高于模型自动写入**
   - 否则用户刚改完，下一轮 hidden writer 又覆盖回去，体验会很差

3. **删除优先软删除，而不是直接物理删除**
   - 特别是 `public/direct` 共享记忆与 `relationship/secret` 这类长期条目

4. **UI 上明确区分“原始来源”与“人工修订”**
   - 否则调试器会迅速变成一个“看不懂状态”的黑箱

---

## 4. 需要先补的数据语义

若要支持“所有记忆可修改/删除”，建议先补以下字段或等价语义：

### 4.1 推荐新增字段

在 `memories` 表中新增：

- `deleted_at TEXT NULL`
  - 软删除时间；非空表示该记忆对 recall 不可见
- `edit_state TEXT NOT NULL DEFAULT 'normal'`
  - 可选值：`normal | user_edited | user_locked | deleted`
- `source_type TEXT NULL`
  - 例如：`manual | llm_write | deterministic_write | migrated`
- `source_memory_id TEXT NULL`
  - 若本条由另一条合并/改写而来，可追踪来源
- `revision INTEGER NOT NULL DEFAULT 0`
  - 每次人工修改 `+1`

### 4.2 若不想扩很多列，可退化为 `meta_json`

最低也应保证 `meta_json` 里有这些键：

- `manual`
- `edited_by`
- `edited_at`
- `revision`
- `deleted_at`
- `user_locked`
- `source_type`

但从后续查询、过滤和调试角度，**关键生命周期字段仍建议单独成列**。

---

## 5. 后端行为规则（这是核心）

### 5.1 Recall 规则

- recall 查询必须默认过滤：
  - `deleted_at IS NULL`
- 若 `edit_state=user_edited`：
  - 使用人工修改后的内容参与 recall
- 若 `edit_state=user_locked`：
  - hidden writer 不得覆盖该条记忆

### 5.2 Hidden writer 与人工修改冲突规则

建议定义为：

1. `normal`
   - 允许 hidden writer 按 `merge_key` 正常合并/覆盖

2. `user_edited`
   - hidden writer 可以继续命中该条，但只能做**受限更新**：
   - 默认仅允许提升 `score/access_count/last_accessed_at`
   - 不允许直接覆盖 `summary/content`

3. `user_locked`
   - hidden writer 不得覆盖内容
   - 如命中相同 `merge_key`，应新建候选条或放弃写入（建议先放弃）

4. `deleted`
   - recall 忽略
   - hidden writer 若同主题再次生成，允许创建新条目（相当于“重新形成记忆”）

### 5.3 删除规则

建议默认做**软删除**：

- `deleted_at = now`
- `edit_state = 'deleted'`

理由：

- 保留审计与调试可能性
- 避免误删后无法分析问题
- 便于未来做“恢复”

物理删除可作为后台维护工具，而不暴露在第一版 UI 中。

### 5.4 scope 特定规则

#### `scope=pc`
- 可直接编辑 / 删除
- 风险最低

#### `scope=public`
- 允许编辑 / 删除，但 UI 必须给出明显提示：
  - “这是共享记忆，修改会影响所有 PC 后续召回”

#### `scope=direct`
- 允许编辑 / 删除，但 UI 必须展示：
  - `scope_id`
  - 对应参与者 / 对话说明

### 5.5 kind 特定规则

#### `autobiography`
- 允许编辑 / 删除
- 但建议默认 `user_locked=true`，避免 hidden writer 后续误合并覆盖

#### `secret`
- 允许编辑 / 删除
- 推荐默认 `user_locked=true`
- UI 需强提示“仅该 PC 可见”

#### `relationship`
- 允许编辑 / 删除
- 需要显式展示 `subject_type/subject_id`
- 删除时建议提示“可能影响角色对某人的长期态度”

#### `recent_event`
- 最适合开放编辑 / 删除
- 风险相对最低

---

## 6. API 计划

若实施，建议补/改以下后端 API：

### 6.1 查询

`GET /api/memories`

新增过滤项：

- `deleted`（是否查看已删除）
- `edit_state`
- `source_type`

### 6.2 修改

`PATCH /api/memories/{memory_id}`

允许修改字段：

- `summary`
- `content`
- `importance`
- `pinned`
- `subject_type`
- `subject_id`
- `owner_pc_id`
- `scope_id`（是否开放需谨慎，建议第一版先不开放）
- `edit_state`
- `user_locked`（若不单独成列，则写进 meta）

### 6.3 删除

新增：

- `DELETE /api/memories/{memory_id}`
  - 默认软删除
- 可选：`POST /api/memories/{memory_id}/restore`
  - 第二阶段再做

### 6.4 审计

每次修改/删除建议写一条 `events`：

- `type = memory_manual_edit`
- `type = memory_manual_delete`

---

## 7. 前端调试器计划

### 7.1 浏览区

在现有“浏览”基础上补：

- 显示 `deleted` / `edit_state` / `source_type`
- 可切换“显示已删除”
- 列表项增加明确按钮：
  - `编辑`
  - `删除`
  - `Pin/Unpin`

### 7.2 详情区

需要明确展示：

- `scope`
- `kind`
- `owner_pc_id / scope_id`
- `subject_type / subject_id`
- `pinned`
- `edit_state`
- `revision`
- `source_type`
- `merge_key`（若存在）

### 7.3 编辑器

从“只支持自传/秘密手工编辑”扩展成“任何记忆都可进入编辑器”：

- 共享字段：`summary/content/importance/pinned`
- 高级字段：`subject_type/subject_id`
- 状态字段：`user_locked/edit_state`

### 7.4 删除交互

建议使用二次确认，不同 scope/kind 显示不同提示：

- `public`：提示“影响所有 PC”
- `direct`：提示“影响该对话共享记忆”
- `relationship`：提示“影响人物关系稳定性”
- `secret`：提示“删除后该秘密将不再参与召回”

---

## 8. 实施顺序（建议）

### 第一步：补生命周期字段与后端语义

- `memories` 增加 `deleted_at/edit_state/revision/source_type`（或最小版先落 `deleted_at + meta_json`）
- recall / decay / hidden writer 全部尊重这些字段

### 第二步：开放后端 API

- `PATCH /api/memories/{id}` 扩展到全量 kind/scope
- 新增 `DELETE /api/memories/{id}`（软删除）
- 写 `events` 审计

### 第三步：升级前端调试器

- 所有记忆显示 `编辑` / `删除`
- 详情区展示高级字段
- 编辑器支持所有 kind

### 第四步：处理 hidden writer 冲突

- `user_edited / user_locked` 与 hidden writer 的 merge 行为稳定下来
- 补 1~2 个验证场景

---

## 9. 风险与取舍

### 9.1 最大风险

**不是 UI，而是“人工修改后自动管线如何继续工作”。**

如果不先定义：

- 用户改过的记忆是否还能被 hidden writer 覆盖
- 删除后的主题是否允许重新生成
- `relationship/secret` 是否默认锁定

那么“开放全量编辑”会让系统状态很快变得不可预测。

### 9.2 推荐取舍

若想风险最小，建议采用：

- 所有记忆都可编辑 / 删除
- 但：
  - 删除一律软删除
  - `autobiography/secret` 人工编辑后默认 `user_locked`
  - `relationship` 人工编辑后默认 `user_edited`
  - `recent_event` 仍允许 hidden writer 后续自然覆盖或新建

---

## 10. 验收标准（若后续实施）

- 调试器中任意一条记忆都能进入编辑或删除流程
- `public/direct` 记忆修改后，后续 recall 能看到新内容
- 被软删除的记忆不再参与 recall / decay
- 人工编辑过的 `autobiography/secret` 不会被 hidden writer 立即覆盖回去
- `events` 中能看到手工修改/删除审计记录

---

## 11. 建议结论

如果只问“能不能开放所有记忆可修改/删除”，答案是：**能，但建议分两段做。**

推荐顺序：

1. 先补**生命周期语义 + 审计 + 软删除**
2. 再开放前端“全量编辑/删除”入口

否则会出现一个很常见的问题：

- UI 看起来能改
- 但 hidden writer / recall / decay 的后台语义没跟上
- 最终用户会觉得“改了，但系统又偷偷改回去了”

这会比“暂时不能改”更难调试。
