将openai格式接口 接入 Antropic Claude Code CLI

## 启用虚拟环境
```bash
uv venv
source .venv/bin/activate
```

安装与启动
```bash
uv pip install python-dotenv fastapi uvicorn httpx

export OPENAI_ACCESS_TOKEN="你的token"
export OPENAI_MODEL="Qwen/Qwen2.5-Coder-32B-Instruct"
uvicorn claude_to_openai_proxy:app --host 127.0.0.1 --port 8045
```

测试一下
```bash
curl http://127.0.0.1:8045/health
```