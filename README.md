# LLM Proxy Gateway

基于 FastAPI 的 LLM 代理网关，支持 OpenAI 和 Claude 两种 API 协议，提供同协议完全透传、消息日志记录和流式/非流式响应。

## 功能特性

- **双协议支持** — 同时支持 OpenAI (`/v1/chat/completions`) 和 Claude (`/v1/messages`) 两种 API 格式
- **同协议透传** — 下游与上游协议一致时完全透传，不修改任何参数，保留所有原始字段
- **多模型路由** — 通过 `model_config.yaml` 配置下游模型名到上游提供商的映射
- **模型别名** — 支持任意名称映射到已有模型
- **多格式支持** — 一个模型可同时配置 OpenAI 和 Claude 两种接入格式，自动选择最优路径
- **消息日志** — 原样记录所有请求和响应参数到本地 JSON 文件
- **流式 + 非流式** — 两种模式均完整记录日志

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置 .env 文件，填入各提供商 API Key
# 启动服务
python -m uvicorn app.main:app --host 0.0.0.0 --port 4936 --reload
```

## 客户端调用示例

### Claude 协议

```python
import httpx

# 非流式
r = httpx.post("http://localhost:4936/v1/messages", json={
    "model": "deepseek-v4-flash-anthropic",
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 1000,
    "temperature": 0.7,
})
print(r.json())

# 流式
with httpx.stream("POST", "http://localhost:4936/v1/messages", json={
    "model": "deepseek-v4-flash-anthropic",
    "messages": [{"role": "user", "content": "讲个故事"}],
    "stream": True,
}) as r:
    for line in r.iter_lines():
        if line.startswith("data: "):
            print(line[6:])
```

### OpenAI 协议

```python
import httpx

# 非流式
r = httpx.post("http://localhost:4936/v1/chat/completions", json={
    "model": "deepseek-v4-flash-openai",
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 1000,
    "temperature": 0.7,
})
print(r.json())

# 流式
with httpx.stream("POST", "http://localhost:4936/v1/chat/completions", json={
    "model": "deepseek-v4-flash-openai",
    "messages": [{"role": "user", "content": "讲个故事"}],
    "stream": True,
}) as r:
    for line in r.iter_lines():
        if line.startswith("data: "):
            print(line[6:])
```

### 错误处理

当模型不支持请求的协议格式时，返回 400 错误：

```python
# 使用只支持 OpenAI 格式的模型请求 Claude 端点
r = httpx.post("http://localhost:4936/v1/messages", json={
    "model": "deepseek-v4-flash-openai",  # 只支持 OpenAI
    "messages": [{"role": "user", "content": "你好"}],
})
# 返回: {"error": "Model 'deepseek-v4-flash-openai' does not support Anthropic/Claude API format..."}
```

## 配置说明

### .env — 环境变量与服务配置

```env
# 服务配置
HOST=0.0.0.0
PORT=4936

# 日志配置
LOG_DIR=logs
LOG_LEVEL=INFO

# API Keys
CLAUDE_API_KEY=your-claude-api-key
OPENAI_API_KEY=your-openai-api-key
DEEPSEEK_API_KEY=your-deepseek-api-key
```

### model_config.yaml — 模型路由与格式

```yaml
models:
  # 同时支持 OpenAI 和 Claude 两种格式
  deepseek-v4-flash:
    upstream_model: deepseek-v4-flash
    api_key_env: DEEPSEEK_API_KEY
    base_url:
      openai: "https://api.deepseek.com"
      anthropic: "https://api.deepseek.com/anthropic"
  
  # 只支持 OpenAI 格式
  gpt-4o:
    upstream_model: gpt-4o
    api_key_env: OPENAI_API_KEY
    base_url:
      openai: "https://api.openai.com"
  
  # 只支持 Claude 格式
  claude-sonnet-4:
    upstream_model: claude-sonnet-4-20250514
    api_key_env: CLAUDE_API_KEY
    base_url:
      anthropic: "https://api.anthropic.com"

# 默认模型
default_model: deepseek-v4-flash

# 模型别名
aliases:
  my-assistant: deepseek-v4-flash
```

**base_url 支持两种格式：**

1. **对象格式（推荐）** — 指定支持的协议格式：
   ```yaml
   base_url:
     openai: "https://api.example.com"
     anthropic: "https://api.example.com/anthropic"
   ```

2. **字符串格式（遗留支持）** — 需要配合 `provider` 字段：
   ```yaml
   base_url: "https://api.example.com"
   provider: openai  # 或 anthropic
   ```

## 协议路由规则

| 下游端点 | 上游配置 | 行为 |
|---------|---------|------|
| `/v1/messages` (Claude) | 有 `anthropic` URL | ✅ 直接透传 |
| `/v1/messages` (Claude) | 只有 `openai` URL | ❌ 返回 400 错误 |
| `/v1/chat/completions` (OpenAI) | 有 `openai` URL | ✅ 直接透传 |
| `/v1/chat/completions` (OpenAI) | 只有 `anthropic` URL | ❌ 返回 400 错误 |

**格式优先选择：** 当模型同时配置了两种格式时，自动选择与下游相同的格式，避免转换。

## 日志

所有日志统一写入 `logs/app.log` 文件，包含：
- 应用日志（启动、错误等）
- 代理日志（上游调用、错误等）
- 对话日志（请求/响应记录）

### 日志结构（对话记录）

对话记录以 JSON 格式保存在 `logs/conversations_{下游模型}_{上游模型}_{日期}.json` 文件中。

### 日志结构

```json
{
  "id": "uuid",
  "timestamp": "2026-04-29T10:00:00+00:00",
  "client_id": "127.0.0.1",
  "downstream_model": "deepseek-v4-flash-openai",
  "upstream_model": "deepseek-v4-flash",
  "request": {
    "model": "deepseek-v4-flash-openai",
    "messages": [{"role": "user", "content": "你好"}],
    "temperature": 0.7,
    "max_tokens": 1000,
    "tools": [...],
    ...  // 所有请求参数原样记录
  },
  "response": {
    "id": "...",
    "choices": [...],
    "usage": {...},
    ...  // 所有响应参数原样记录
  }
}
```

### 流式日志

流式模式下，日志会收集所有流式数据块，重组为与非流式一致的格式：

- **OpenAI 格式**: 收集 `content`, `reasoning_content`, `tool_calls`
- **Claude 格式**: 收集 `text`, `thinking`, `tool_use`

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/messages` | Claude 对话接口（流式/非流式） |
| POST | `/v1/chat/completions` | OpenAI 对话接口（流式/非流式） |
| GET | `/v1/models` | 获取可用模型列表 |
| GET | `/health` | 健康检查 |

## 测试

运行测试套件验证所有功能：

```bash
# 运行所有测试
python test_proxy.py

# 运行特定测试
python test_proxy.py 1        # 只运行测试 #1
python test_proxy.py 1,3,5    # 运行多个测试

# 调试模式
python test_proxy.py 5 --debug

# 列出所有测试
python test_proxy.py --list
```

### 测试用例

| # | 测试名称 | 验证内容 |
|---|---------|---------|
| 1 | OpenAI → OpenAI (Stream) | OpenAI 格式流式响应 |
| 2 | OpenAI → OpenAI (Non-Stream) | OpenAI 格式非流式响应 |
| 3 | Claude → Claude (Stream) | Claude 格式流式响应 |
| 4 | Claude → Claude (Non-Stream) | Claude 格式非流式响应 |
| 5 | reasoning_effort 参数测试 | 参数正常传递 |
| 6 | 中文输出不被转义 | ensure_ascii=False 验证 |
| 7 | 单次 message_delta | 不重复返回 stop_reason |
| 8 | 格式优先选择 | 自动选择同协议格式 |
| 9-13 | Claude Content 测试 | string/array/thinking/tool_use/mixed |
| 14-15 | Tool Use 测试 | OpenAI/Claude 工具调用 |
| 16 | Unsupported Protocol Error | 协议不支持返回 400 |

## 项目结构

```
D:\ProjectsMy\X\
├── app/
│   ├── main.py          # FastAPI 应用，API 端点
│   ├── config.py        # 配置加载，日志设置
│   ├── models.py        # Pydantic 数据模型
│   ├── proxy.py         # 上游调用（完全透传）
│   └── logger.py        # 对话日志记录
├── model_config.yaml    # 模型路由配置
├── .env                 # 环境变量与服务配置
├── test_proxy.py        # 测试套件
└── logs/                # 日志输出目录
```

## 注意事项

- 修改 `model_config.yaml` 后需重启服务或使用 `--reload` 模式
- API Key 变更需更新 `.env` 并重启
- 日志文件按日期分割，避免单文件过大
- 日志级别通过 `.env` 中的 `LOG_LEVEL` 控制
