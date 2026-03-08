LLM 接入基础工具方法（backend）

目标：先把“发请求 / 解析返回 / 记录日志 / 取消并发请求 / 全局排队+RPM 限速”的基础打好，供后续 engine 或 API 调用。

## 入口与位置

- 主要工具：`backend/app/llm.py`
  - `LlmService.chat(...)`：发请求 + 自动落库 + 返回解析结果
  - `LlmService.cancel_inflight()`：取消当前所有 **in-flight** 请求（不影响队列里尚未开始的请求）
  - `parse_llm_response(raw)`：解析返回（结构化 JSON 原样保留；否则当作 Markdown）
  - `GLOBAL_LLM_MANAGER`：全局队列/限速器（RPM 默认 5）
  - `openai_chat_completions_url(base_url)`：把 base_url 规范成 `/chat/completions` 完整 URL

## DB 日志

- 表：`llm_logs`（创建于 `backend/app/db.py` 的 SCHEMA_SQL）
- 存储内容：
  - `request_json`：完整请求体（不包含 url / apikey；这些作为参数传入，不会写入 payload）
  - `response_json`：返回体（JSON；若非 JSON，则会被包装为 `{ "_non_json": "..." }`）
  - `status_code` / `duration_ms` / `error`
- 读取：`SqliteStore.list_llm_logs(limit=...)`

## 解析规则（parse）

- 优先识别 OpenAI-compatible 返回：
  - 若 `choices[0].message.tool_calls` 存在：视为 **structured**，原样返回 tool_calls
  - 否则若 `choices[0].message.content` 是合法 JSON（支持 ```json code fence```）：视为 **structured**，返回解析后的 JSON
  - 否则：视为 **markdown**，返回 content 字符串（前端可 Markdown 渲染）
- 若 HTTP 返回不是 JSON：`raw` 会是 `{ "_non_json": "..." }`，解析结果为 **markdown**

## 全局队列与 RPM 限速

- 全局变量：`DEFAULT_LLM_RPM_LIMIT = 5`
- 语义：rolling-window（任意连续 60s 内最多 5 次“启动请求”）
- 队列：FIFO；排队中的请求不被 `cancel_inflight()` 影响

## 典型用法（示例）

```py
from backend.app.llm import LlmService, openai_chat_completions_url

service = LlmService(store=store)

url = openai_chat_completions_url(settings.openai_base_url)
res = await service.chat(
    url=url,
    apikey=settings.openai_api_key,
    model=settings.openai_model,
    messages=[{"role": "user", "content": "hi"}],
    tools=[
        {
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "Get current time",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ],
)

if res["parsed"]["kind"] == "markdown":
    markdown = res["parsed"]["markdown"]
else:
    structured = res["parsed"]["structured"]
```

取消当前并发请求：

```py
cancelled = await service.cancel_inflight()
```
