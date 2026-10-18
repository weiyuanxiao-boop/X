# LLM Proxy Gateway

基于 FastAPI 的 LLM 代理网关，支持 OpenAI 和 Claude 两种 API 协议，提供同协议字节级透传、消息日志记录和流式/非流式响应。

## 功能特性

- **三端点支持** — OpenAI Chat Completions (`/v1/chat/completions`)、OpenAI Responses (`/v1/responses`)、Claude Messages (`/v1/messages`)
- **访问鉴权** — 默认开启；本地连接免 key，非本地连接必须携带 `model_config.yaml` 中配置的 key
- **资源限制** — 请求体上限（413）、按来源限流（429）、日志单条截断
- **头边界收敛** — 请求头白名单，响应头过滤 `set-cookie` / CORS / 逐跳头
- **字节级透传** — 下游请求体原样发往上游，唯一改动的字节是顶层 `model` 字段的值。字段顺序、未知字段、嵌套结构、unicode 转义、数字写法全部保持原样；响应体同样按原始字节回传，不重新序列化
- **请求头/响应头透传** — 下游业务头（`anthropic-beta`、`openai-organization` 等）原样转发，上游限流头、`request-id` 原样回传
- **跨协议不支持** — Claude ↔ OpenAI 之间不做转换，路由阶段直接返回 400
- **多模型路由** — 通过 `model_config.yaml` 配置下游模型名到上游提供商的映射
- **模型别名** — 支持任意名称映射到已有模型
- **多格式支持** — 一个模型可同时配置 OpenAI 和 Claude 两种接入格式，按下游协议自动选择
- **消息日志** — 记录完整请求与响应（含流式事件序列）到本地 JSON 文件
- **流式 + 非流式** — 两种模式均原样转发并记录日志

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

### OpenAI Responses API

```python
import httpx

r = httpx.post("http://localhost:4936/v1/responses", json={
    "model": "deepseek-v4-flash-openai",
    "input": "用一句话解释什么是反向代理",
    "max_output_tokens": 200,
})
data = r.json()
print(data["object"])          # response
for item in data["output"]:
    for block in item.get("content", []):
        if block.get("type") == "output_text":
            print(block["text"])
```

流式返回的是 `event: response.*` 事件序列，代理逐字节透传，不做事件重组。

### 错误处理

当模型不支持请求的协议格式时，返回 400 错误：

```python
# 使用只支持 OpenAI 格式的模型请求 Claude 端点
r = httpx.post("http://localhost:4936/v1/messages", json={
    "model": "deepseek-v4-flash-openai",  # 只支持 OpenAI
    "messages": [{"role": "user", "content": "你好"}],
})
# 返回: {"type":"error","error":{"type":"invalid_request_error",
#        "message":"Model 'deepseek-v4-flash-openai' does not support Anthropic/Claude API format..."}}
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

> `base_url` 只写到域名或兼容路径前缀，**不要带 `/v1`**：
> 端点路径由代理按 `/v1/messages` / `/v1/chat/completions` / `/v1/responses` 追加，
> 带 `/v1` 会拼成 `/v1/v1/...`。

## 协议路由规则

| 下游端点 | 上游配置 | 行为 |
|---------|---------|------|
| `/v1/messages` (Claude) | 有 `anthropic` URL | ✅ 字节级透传 → `/v1/messages` |
| `/v1/messages` (Claude) | 只有 `openai` URL | ❌ 返回 400 错误 |
| `/v1/chat/completions` (OpenAI) | 有 `openai` URL | ✅ 字节级透传 → `/v1/chat/completions` |
| `/v1/responses` (OpenAI Responses) | 有 `openai` URL | ✅ 字节级透传 → `/v1/responses` |
| `/v1/chat/completions` 或 `/v1/responses` | 只有 `anthropic` URL | ❌ 返回 400 错误 |

**格式优先选择：** 当模型同时配置了两种格式时，自动选择与下游相同的格式，避免转换。

**不支持跨协议转换：** Claude ↔ OpenAI 之间不做任何字段映射。若下游协议与上游可用格式不一致，直接返回 400，不会尝试转换。

## 访问鉴权

**默认开启**（`auth.enabled: true`）。`keys` 为空时所有非本地请求都会被拒绝（fail closed），
所以对外提供服务前必须先在 `model_config.yaml` 里填入 key。

```yaml
auth:
  enabled: true
  keys:
    - "sk-aaa"          # 列表写法，日志里记为 key1
    - "sk-bbb"
  # 或带名字的映射写法，名字会记入日志的 auth.principal
  # keys:
  #   team-a: "sk-aaa"
  #   ci: "sk-bbb"
```

流式写法（`keys: ["sk-aaa", "sk-bbb"]`）与块状写法完全等价，任选其一。
值建议始终加双引号：不加引号时纯数字会被 YAML 解析成整数，含特殊字符可能整段解析失败。

### 放行规则

| 来源 | 是否校验 key |
|---|---|
| `localhost` / `127.0.0.0/8` / `::1` / `::ffff:127.x.x.x` | 不校验，直接放行 |
| 其他地址 | 必须携带正确 key，否则 401 |
| `keys` 为空（默认状态） | 非本地全部拒绝（fail closed），启动时打 warning |

`/health` 不鉴权，留给探活；`/v1/models`、`/docs`、`/v1/aliases` 等其余路径都要过鉴权。

### 客户端怎么传 key

三种方式任选，按顺序取第一个命中的（同时兼容 OpenAI 与 Claude 官方 SDK）：

```bash
# OpenAI / 通用
curl -H "Authorization: Bearer sk-aaa" ...
# Claude SDK
curl -H "x-api-key: sk-aaa" ...
# 容错：Authorization 直接放 key
curl -H "Authorization: sk-aaa" ...
```

下游用于鉴权的这两个头**不会转发给上游** —— 转发层会用上游凭证覆盖 `Authorization` / `x-api-key`。

### 401 响应

按下游协议返回标准错误结构：

```json
// /v1/messages
{"type": "error", "error": {"type": "authentication_error", "message": "Invalid API key"}}

// /v1/chat/completions
{"error": {"message": "Invalid API key", "type": "authentication_error", "param": null, "code": null}}
```

被拒请求会写入对话日志（`response.status = 401`），但**请求体不落盘**，避免未认证内容写满磁盘。

## 资源限制与上游连接

```yaml
limits:
  max_body_mb: 32             # 单请求体上限，超出返回 413；0 = 不限
  rate_limit_per_minute: 0    # 每个来源 IP 每分钟请求数上限，0 = 不限
  max_logged_body_mb: 8       # 日志中 request 的最大字节数，超出只记元信息

upstream:
  trust_env: false            # 是否读取 HTTP_PROXY / HTTPS_PROXY 等环境变量
  proxy: null                 # 显式代理地址，null = 直连
```

- **max_body_mb** — 边读边计数，超限立即中断并返回 413，不会把整个 body 读进内存；0 表示不限制
- **rate_limit_per_minute** — 按来源 IP 固定窗口计数，超出返回 429 并带 `Retry-After: 60`
- **max_logged_body_mb** — 请求或响应超过该大小时，日志里只记 `{"__truncated__": true, "bytes": N}`
- **trust_env** — 默认 `false`，不读取系统代理变量，避免 API key 与对话内容流经环境中的代理
- **proxy** — 确实需要走代理时显式指定，例如 `"http://127.0.0.1:7890"`

## 请求头与响应头边界

**请求头是白名单**，只有下列头会到达上游，其余（`Cookie`、`X-Forwarded-For`、`Referer`、自定义头等）一律丢弃：

| 协议 | 放行的头 |
|---|---|
| 通用 | `accept`、`accept-language`、`user-agent` |
| Anthropic | `anthropic-beta`、`anthropic-version` |
| OpenAI | `openai-organization`、`openai-project`、`openai-beta` |

`Authorization` / `x-api-key` 永远替换为上游凭证，不会转发下游的值。

**响应头会过滤**：`set-cookie`、`access-control-*`、逐跳头与 `content-*` 不回落给下游；
`request-id`、`anthropic-ratelimit-*`、`retry-after` 等排查用头保留。

错误响应只返回通用描述（如 `Failed to reach upstream`），具体异常（可能含内网地址、代理信息）只写服务日志。

## 浏览器跨域（CORS）

只有**浏览器 JS 直接调用**本服务时才需要；后端、curl、SDK 调用不受影响。

```yaml
cors:
  enabled: true
  allow_origins:
    - "*"          # 或具体来源
  allow_credentials: false
  allow_methods: [GET, POST, OPTIONS]
  allow_headers:
    - authorization
    - content-type
    - x-api-key
    - anthropic-version
    - anthropic-beta
```

三点必须注意：

1. **`*` 与 `allow_credentials: true` 不能共存**（浏览器规范禁止）。配了 `*` 时服务端会强制 `allow_credentials=false`，此时前端的 `fetch(..., {credentials: "include"})` 会被浏览器拒绝。要么去掉 `credentials`，要么把 `*` 换成具体来源。
2. **OPTIONS 预检不携带 `Authorization`**，所以 CORS 中间件必须位于鉴权中间件**外层**（代码里注册在鉴权之后），否则预检会先被 401 拦掉。
3. **鉴权失败（401）的响应也会带 CORS 头**，浏览器才能看到真实的 401 而不是笼统的 CORS 错误。

**安全提示**：浏览器直连意味着 API key 写在前端代码里，任何能打开该页面的人都能拿到。建议只在内网管理页或本地调试时开启；公网页面应走后端转发。


## 日志

运行日志写入 `logs/app.log`；对话记录单独存为 JSONL，见下。

### 对话记录：目录与文件

> 2026-10-18 之前的日志仍在 `logs/` 根目录，是单个 JSON 数组文件；
> 之后的日志按下述规则写入日期子目录，格式为 JSONL。

```
logs/
└── 2026-10-18/
    ├── conversations_127.0.0.1_cc-coder_deepseek-v4-flash.jsonl
    └── conversations_127.0.0.1_deepseek-v4-flash-openai_deepseek-v4-flash.jsonl
```

- 一级目录是**日期**（本地时区）
- 文件名是 `conversations_{客户端}_{下游模型}_{上游模型}.jsonl`，客户端取连接来源 IP
- 一条对话写一行，`request` 与 `response` 在同一条记录内，不需要跨记录关联

### 为什么是 JSONL 而不是 JSON 数组

写入采用 **O_APPEND + 单次 write + 跨进程文件锁**（Windows `msvcrt.locking` / POSIX `flock`），
不做「读整个文件 → 改 → 写回」。因此：

- 并发请求互不覆盖，多线程、多 worker 进程同时写都不会丢记录
- 进程被强杀最多丢失正在写的那一行，不影响已落盘记录
- 单行损坏只影响该行，其余行照常读取

代价是整个文件不再是合法 JSON 文档，需要用按行处理的工具：

```bash
wc -l logs/2026-10-18/*.jsonl              # 请求数
grep '"status": 500' logs/2026-10-18/*.jsonl
jq -c 'select(.response.status != 200)' logs/2026-10-18/*.jsonl   # 逐行解析
```

```python
import json, glob

for path in glob.glob("logs/2026-10-18/*.jsonl"):
    for line in open(path, encoding="utf-8"):
        if line.strip():
            record = json.loads(line)
```

### 单行结构

```json
{
  "id": "uuid",
  "timestamp": "2026-10-18T21:49:04.741+08:00",
  "client_id": "127.0.0.1",
  "downstream_model": "deepseek-v4-flash-openai",
  "upstream_model": "deepseek-v4-flash",
  "auth": {"mode": "local", "principal": ""},
  "request": {
    "model": "deepseek-v4-flash-openai",
    "messages": [{"role": "user", "content": "你好"}],
    "temperature": 0.7,
    "max_tokens": 1000,
    "tools": [...]
  },
  "response": {
    "status": 200,
    "headers": {"content-type": "application/json", "request-id": "..."},
    "body": {"id": "...", "choices": [...], "usage": {...}}
  }
}
```

本地拒绝（非法 JSON、跨协议、模型未知）与上游不可达也会记录，`response` 为
`{"status": 400|502, "error": "..."}`，`upstream_model` 为空串。

### 流式日志

流式模式下 `response` 不带 `body`，改为 `stream: true` + `events`：按 SSE 逐条解析出的
`data:` JSON 原样保存，事件数量上限 5000 条，超出时追加 `{"__truncated__": true}`。
这种记录方式不会丢失上游事件里的任何字段（如 `usage`、`cache_read_input_tokens`）。

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/messages` | Claude 对话接口（流式/非流式） |
| POST | `/v1/chat/completions` | OpenAI Chat Completions 接口（流式/非流式） |
| POST | `/v1/responses` | OpenAI Responses 接口（流式/非流式） |
| GET | `/v1/models` | 获取可用模型列表 |
| GET | `/health` | 健康检查 |

## 测试

### 透传保真度验证（离线，推荐先跑）

`verify_passthrough.py` 会自己起一个本地假上游，记录收到的原始字节，逐项比对
「下游发出」与「上游收到」、「上游返回」与「下游收到」是否逐字节一致，不依赖真实 API Key：

```bash
python verify_passthrough.py
```

覆盖 37 项断言：请求体逐字节一致、`cache_control` / `document` / `redacted_thinking` 保留、
不注入默认值、请求头透传、`tool_calls` / `tool_call_id` 保留、响应体与响应头透传、
上游 400 不被改写为 500、`retry-after` 保留、流式逐字节等价、跨协议 400。

### 日志写入验证（离线）

验证并发不丢记录、JSONL 完整性、目录与文件名规则：

```bash
python verify_logging.py
```

覆盖多线程 500 条、多进程 4×150 条（模拟 `--workers 4`）、端到端按客户端与模型分文件。

### 访问鉴权验证（离线）

```bash
python verify_auth.py
```

覆盖 127.0.0.1 / 127.5.5.5 / ::1 / ::ffff:127.x 放行、远程无 key / 错 key / 正确 key、
三种携带方式、`/health` 与 `/v1/models` 差异、下游 key 不泄漏到上游、日志 `auth` 字段留痕。

### 安全加固验收（离线）

```bash
python verify_hardening.py
```

覆盖 auth 默认启用、请求头白名单、响应头过滤、alias 注入防护、body 上限 413、
限流 429 + Retry-After、错误信息脱敏、日志截断、上游代理隔离。

### 端到端测试（需服务在 4936 运行 + 真实上游）

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
| 17 | OpenAI Responses (Non-Stream) | Responses API 透传，返回 response 对象 |

## 项目结构

```
D:\ProjectsMy\X\
├── app/
│   ├── main.py          # FastAPI 应用，API 端点与转发编排
│   ├── config.py        # 配置加载，日志设置
│   ├── proxy.py         # 字节级透传（model 改写、头过滤、SSE 采集）
│   ├── auth.py          # 访问鉴权（本地放行、key 校验）
│   └── logger.py        # 对话日志记录（JSONL 追加写）
├── model_config.yaml    # 模型路由配置
├── .env                 # 环境变量与服务配置
├── test_proxy.py        # 端到端测试套件（需真实上游）
├── verify_passthrough.py # 透传保真度验证（自带假上游，离线可跑）
├── verify_logging.py    # 日志并发写入与结构验证（离线可跑）
├── verify_auth.py       # 访问鉴权验证（离线可跑）
├── verify_hardening.py  # 安全加固验收（离线可跑）
└── logs/                # 日志输出目录（按日期分目录）
```

## 注意事项

- 修改 `model_config.yaml` 后需重启服务或使用 `--reload` 模式
- API Key 变更需更新 `.env` 并重启
- 日志文件按日期分割，避免单文件过大
- 日志级别通过 `.env` 中的 `LOG_LEVEL` 控制
