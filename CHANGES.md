# LLM Proxy Gateway 改动总结

本文档总结了项目的所有重要改动和对应的测试验证。

## 最新改动 (2026-10-18)

### 改为字节级透传，请求不再经过 Pydantic 模型

**问题**：原先用 `ClaudeRequest` / `OpenAIRequest` 接收请求体，只给顶层模型开了
`extra="allow"`，嵌套模型（`TextContent` / `ToolUseContent` / `Message` /
`OpenAIMessage` 等）都会丢弃未声明字段，并且会把默认值写回 body。
"完全透传"名不副实，实测确认的参数丢失：

| 丢失项 | 后果 |
|--------|------|
| `cache_control`（system / text / tool_use / tool_result） | prompt caching 失效 |
| `tool_calls` / `tool_call_id` / `reasoning_content` | 多轮 function calling 断裂 |
| `document` / `redacted_thinking` / `server_tool_use` block | Pydantic 校验失败，下游收到 422 |
| 下游全部请求头（含 `anthropic-beta`） | 1M 上下文、interleaved thinking 等特性不可用 |
| 默认值注入 `temperature=1.0` / `top_p=1.0` / `stream=false` | 下游未传的参数被强制补上 |
| 上游 4xx → `raise_for_status()` | 下游收到 500，上游错误体与 `retry-after` 全部丢失 |
| 非流式响应头 | `request-id`、`anthropic-ratelimit-*` 丢失 |

**改动内容**：
- `app/main.py`：端点签名改为 `request: Request`，用 `await request.body()` 取原始字节
- `app/proxy.py`：
  - `rewrite_top_level_model()` 按字节扫描，只替换顶层 `model` 的值，其余字节（字段顺序、
    未知字段、unicode、数字写法、空格）原样保留
  - `build_upstream_headers()` 透传下游业务头，仅替换鉴权头与 `content-type`
  - `filter_response_headers()` 剔除逐跳头与 `content-encoding`，保留限流/请求 ID 等
  - `build_body()` 在 body 缺少顶层 `model` 时退化为重新序列化
- 响应不再 `resp.json()` 往返：原样回传上游 `status_code` + 原始 bytes + 响应头
- 流式改用 `aiter_bytes()` 原样转发，不再逐行重建，content-type 沿用上游
- 上游不可达返回 502（原为 500）
- 本地错误（跨协议 / 非法 JSON）按下游协议返回标准错误结构
- 流式判定改为跟随上游 `content-type`，不再依据下游 `stream` 字段猜测
- 删除 `app/models.py`（请求模型不再参与转发链路）

**明确不支持**：Claude ↔ OpenAI 跨协议。`/v1/messages` 只能路由到配置了
`anthropic` base_url 的模型，`/v1/chat/completions` 只能路由到 `openai`，否则 400。

**验证**：
- `verify_passthrough.py`：29 项保真度断言（请求体/请求头/响应体/响应头/流式/错误码/跨协议）
- `test_proxy.py`：16/16 通过

---

### 对话日志改为 JSONL 追加写（解决并发写坏）

**问题**：原实现每写一条日志都要 `_read_log`（读整个 JSON 数组）→ append → `_write_log`（整体重写）。
并发请求下这是典型的读-改-写竞态，后写者会覆盖先写者；进程中途被强杀则整个文件损坏。

**改动**：
- 路径改为 `logs/{YYYY-MM-DD}/conversations_{客户端}_{下游模型}_{上游模型}.jsonl`
  （日期取本地时区；客户端取连接来源 IP）
- **一条对话写一行**，`request` 与 `response` 在同一条记录内，不再需要按 id 跨记录关联
- 写入改为 `O_APPEND` 打开 + **单次 `os.write`** + **跨进程文件锁**
  （Windows `msvcrt.locking` / POSIX `flock`），进程内再加 `threading.Lock`
- 不再要求整个文件是合法 JSON；单行损坏只影响该行
- 本地拒绝（非法 JSON / 跨协议 / 未知模型）与上游不可达（502）也记录，`response` 为 `{"status": ..., "error": ...}`
- 日志时间戳改为带本地时区的 ISO 8601

**验证**：`verify_logging.py` 12/12 通过——多线程 500 条、多进程 4×150 条（模拟 `--workers 4`）、
端到端按客户端与模型分文件、跨协议拒绝留痕。

**实测**：仅靠 `O_APPEND` 时 4 进程并发写 600 条**丢了 126 条**（Windows 的 `_O_APPEND`
是「seek 到末尾再写」，跨进程有竞态）；加锁后 600 条全部落盘、零损坏行。

---

### 新增访问鉴权（本地放行，远程校验 key）

**需求**：非本地客户端必须携带正确 key；本地连接（localhost / 127.x.x.x）不校验；key 在 yaml 配置。

**改动**：
- `app/auth.py`（新增）：
  - `is_loopback()` 覆盖 `localhost`、`127.0.0.0/8`、`::1`、IPv4-mapped IPv6（`::ffff:127.x`）
  - `extract_token()` 支持 `Authorization: Bearer <key>`、`x-api-key: <key>`、裸 `Authorization: <key>`
  - `secrets.compare_digest` 做常量时间比较
- `app/config.py`：解析 `auth` 段。`keys` 支持列表（名字记为 key1/key2）与映射（名字进日志）；
  `enabled` 省略时按 keys 是否为空推断，填了 key 即启用
- `app/main.py`：加 HTTP 中间件统一鉴权。`/health` 放行，其余路径（含 `/v1/models`、`/docs`、`/v1/aliases`）都要过
- 401 按下游协议返回标准错误结构；对话端点留痕，但**请求体不落盘**（避免未认证内容写满磁盘）
- 日志记录新增 `auth` 字段：`{"mode": "local|key|disabled|denied", "principal": "<key 名>"}`，不记录 key 明文
- `model_config.yaml` 增加 `auth` 段，默认 `keys: []`（即关闭），填 key 即生效

**安全细节**：
- `enabled: true` 且 `keys` 为空时 **fail closed**（非本地全部 401），启动时打 warning
- 下游用于鉴权的 `Authorization` / `x-api-key` 不会转发给上游，转发层会用上游凭证覆盖
- 被拒请求不记录原始请求体

**验证**：`verify_auth.py` 27/27 通过——四种回环形式放行、远程无 key/错 key/正确 key、
三种携带方式、`/health` 与 `/v1/models` 的差异、下游 key 不泄漏到上游、fail closed、日志留痕。
回归：`verify_passthrough.py` 29/29、`verify_logging.py` 12/12、`test_proxy.py` 16/16。

---

### 安全整改（按安全审计意见）

| 审计项 | 整改内容 |
|---|---|
| 鉴权未启用 | `auth.enabled` **默认改为 true**；`keys` 为空时非本地一律拒绝（fail closed），启动打 warning |
| httpx `trust_env=True` | 默认 `trust_env=False`，不再读取 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`；需要代理时显式配 `upstream.proxy` |
| 请求头黑名单 | 改为**白名单**：只放行 `accept` / `accept-language` / `user-agent` / `anthropic-beta` / `anthropic-version` / `openai-organization` / `openai-project` / `openai-beta`；`Cookie`、`X-Forwarded-For`、`Referer`、自定义头不再到达上游 |
| 上游 `Set-Cookie` 回传 | 响应侧过滤 `set-cookie` / `set-cookie2` / `access-control-*`；`request-id`、限流头仍保留 |
| `/v1/aliases` YAML 注入 | `alias` 加正则校验（`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`）；`save_aliases` 改用 `yaml.safe_dump` 而非字符串拼接 |
| 请求体无上限 | 边读边计数，超 `limits.max_body_mb`（默认 32MB）立即中断并返回 413；不整块读入内存 |
| 无速率限制 | 按来源 IP 固定窗口限流（`limits.rate_limit_per_minute`，默认 0=不限），超出返回 429 + `Retry-After: 60` |
| 错误信息泄露 | 502 只回 `Failed to reach upstream`，400 只回 `Request body is not a valid JSON object`；具体异常只写服务日志 |
| 日志体积 | 单条请求超过 `limits.max_logged_body_mb`（默认 8MB）时，日志只记 `{"__truncated__": true, "bytes": N}` |

**新增配置**：`limits`（max_body_mb / rate_limit_per_minute / max_logged_body_mb）、
`upstream`（trust_env / proxy）。

**验证**：`verify_hardening.py` 28/28 通过。
回归：`verify_passthrough.py` 29/29（断言"自定义头已透传"改为"非白名单自定义头被丢弃"）、
`verify_logging.py` 12/12、`verify_auth.py` 27/27、`test_proxy.py` 16/16。

---

### QA 复核发现的缺口及二次修复

首轮验收脚本（`verify_hardening.py`）未覆盖以下场景，由独立对抗测试发现：

| 缺口 | 复现证据 | 修复 |
|---|---|---|
| **鉴权失败不受限流约束** | 限流设 3/min，连续 30 次错误 key 全部返回 401、零 429，且每次失败写一行日志（可爆破 key + 日志放大） | 限流判断移到 `authenticate()` **之前**，401 同样消耗配额；复验 `{401: 3, 429: 27}` |
| **响应体未做日志截断** | `max_logged_body_mb=1` 时，5MB 上游响应仍被完整写入日志（+5.00 MB） | 非流式按 `len(body)` 判断、流式按累计字节判断，超限只记 `{"__truncated__": true, "bytes": N}`；复验 +0.00 MB |
| **空 `anthropic-version` 覆盖配置值** | 下游传空字符串，上游收到 `''` 而非配置的 `2023-06-01` | 白名单透传时跳过空值/纯空白头值，回落到配置默认 |
| **`x-internal-*` / `server` 仍回传** | 上游 `X-Internal-Host: 10.0.0.5` 到达下游 | 响应头过滤前缀增加 `x-internal-`，并剔除 `server` |
| **`max_body_mb=0` 语义不清** | 0 实际表示"上限 0 字节"，与 `rate_limit_per_minute` 的 0=不限不一致 | 统一为 **0 = 不限制**，配置与文档同步 |

**复验**：上述 4 项全部转为通过；回归 `verify_passthrough.py` 29/29、`verify_hardening.py` 28/28、
`verify_auth.py` 27/27、`verify_logging.py` 12/12、`test_proxy.py` 16/16。

---

### 新增 OpenAI Responses API 支持

**改动**：
- 新增 `POST /v1/responses` 路由，字节级透传到上游 `/v1/responses`
- `proxy.py` 的 `_ENDPOINT_PATH` 从「协议格式 → 路径」改为「**端点类型** → 路径」：
  `messages` / `chat` / `responses`
- `_handle()` 增加 `endpoint` 参数；`downstream_format` 仍只决定上游 base_url 与鉴权头
- 鉴权、限流、`_CONVERSATION_PATHS` 自动覆盖新端点，无需额外改动
- 流式复用现有 SSE 逐字节转发；日志采集按 `data:` 行解析，天然兼容 `response.*` 事件

**实测（DeepSeek）**：
- `POST /v1/responses` 返回标准 `response` 对象（`object=response`、`output`、`usage`）
- `/v1/responses` 与 `/responses` 两种路径都可用，取前者与现有 `/v1/*` 风格一致
- `deepseek-v4-flash` 模型名可直接使用（文档写 `deepseek-flash`，实际两者都接受）
- 流式为标准 SSE：`response.created` → `response.output_item.added` →
  `response.reasoning_text.delta` / `response.output_text.delta` → `response.completed`

**验证**：
- `test_proxy.py` 新增 #17「OpenAI Responses (Non-Stream)」真实上游用例，**17/17 通过**
- `verify_passthrough.py` 新增 6 项（路径转发 / 请求体逐字节 / instructions·reasoning·text·tools 保留 /
  响应体逐字节 / 流式逐字节 / content-type），**35/35 通过**
- 后续补充 legacy 跨协议用例后为 **37/37**

**注意**：代理层已支持，但上游必须真的提供 `/v1/responses`。目前确认 DeepSeek 支持；
其他厂商若只有 chat completions 兼容层，打该端点会原样收到上游的 404/400。

---

### 修复：legacy 格式绕过跨协议校验

**问题**：`base_url` 用字符串 + `provider` 字段的旧写法，在 `get_upstream_info()` 里**没有校验
`downstream_format`**，直接返回配置的 provider。后果是用错鉴权头去打上游：

- `qwen3.6-plus-anthropic`（provider=anthropic）打 `/v1/responses` → 用 `Authorization: Bearer`
  打 Anthropic 端点，下游只收到上游 401
- `Qwen3.6-Coder`（provider=openai）打 `/v1/messages` → 用 `x-api-key` 打 OpenAI 端点，同样 401

这违反了"跨协议一律 400"的既定决策，而且错误原因被藏在远端。

**修复**：legacy 分支补上 `provider != downstream_format` 的拒绝逻辑，返回
`Model 'xxx' only supports anthropic API format (downstream requested openai)`。

**验证**：`verify_passthrough.py` 新增 2 项（legacy 模型打 Responses → 400、同协议仍可用），
**37/37 通过**；`test_proxy.py` 17/17。

---

### 新增浏览器跨域（CORS）支持

**问题**：安全整改时把 `access-control-*` 响应头全部过滤掉了，浏览器 JS 无法直连。
另外 `OPTIONS` 预检不带 `Authorization`，会被鉴权中间件拦成 401——而且 401 也没有 CORS 头，
浏览器只能报笼统的 CORS 错误，看不到真实原因。

**改动**：
- `config.py` 新增 `cors` 配置解析（`enabled` / `allow_origins` / `allow_credentials` /
  `allow_methods` / `allow_headers`）；`allow_origins` 含 `"*"` 时强制 `allow_credentials=false`
- `main.py` 在**鉴权中间件之后**注册 `CORSMiddleware`（Starlette 里后注册的位于外层），
  使 OPTIONS 预检在进入鉴权前就被处理
- `model_config.yaml` 增加 `cors` 段

**验证**：`verify_hardening.py` 新增 3 项——预检 200、预检返回 `allow-origin`、
401 响应带 CORS 头，**36/36 通过**。真实 uvicorn 下用 curl 模拟浏览器预检，
返回 `allow-origin: *` + `allow-methods` + `max-age=600` + 含 `authorization` 的 `allow-headers`。
回归 `verify_passthrough.py` 37/37、`verify_auth.py` 27/27、`verify_logging.py` 12/12。

---

## 历史改动 (2026-04-29)

### 移除交叉协议转换，仅支持同协议透传
**改动内容**:
- 移除所有 OpenAI ↔ Claude 交叉协议转换代码
- 下游协议必须与上游协议一致
- 如果上游模型不支持请求的协议格式，返回 400 错误

**架构**:
- **OpenAI → OpenAI**: 完全透传
- **Claude → Claude**: 完全透传
- ~~OpenAI → Claude~~: **已移除**
- ~~Claude → OpenAI~~: **已移除**

**文件**:
- `app/models.py` (移除转换相关模型)
- `app/proxy.py` (仅保留 `*_passthrough` 函数)
- `app/main.py` (移除转换逻辑，添加协议不匹配错误处理)
- `test_proxy.py` (移除交叉协议测试)

**测试覆盖**:
- ✅ 测试 #1-2: OpenAI → OpenAI (流式/非流式)
- ✅ 测试 #3-4: Claude → Claude (流式/非流式)
- ✅ 测试 #14-15: Tool Use (同协议)

---

### 完全透传 passthrough 调用
**改动内容**:
- `call_*_passthrough` 和 `stream_*_passthrough` 完全透传所有参数
- 使用 `model_dump(exclude_none=True)` 导出所有字段（包括 extra 字段）
- 流式响应原样转发每一行（包括 `data:`, `[DONE]` 等）

**文件**:
- `app/proxy.py` (passthrough 函数简化)

---

### 去掉参数验证（允许额外字段）
**改动内容**:
- `ClaudeRequest` 和 `OpenAIRequest` 使用 `ConfigDict(extra="allow")`
- 可以接收任何未来可能添加的参数而不会报错

**文件**:
- `app/models.py`

---

## 测试用例列表

| # | 测试名称 | 验证内容 |
|---|---------|---------|
| 1 | OpenAI → OpenAI (Stream) | OpenAI 格式流式响应，中文正常显示 |
| 2 | OpenAI → OpenAI (Non-Stream) | OpenAI 格式非流式响应 |
| 3 | Claude → Claude (Stream) | Claude 格式流式响应 |
| 4 | Claude → Claude (Non-Stream) | Claude 格式非流式响应 |
| 5 | reasoning_effort 参数测试 | 参数正常传递 |
| 6 | 中文输出不被转义 | ensure_ascii=False 验证 |
| 7 | 单次 message_delta | 不重复返回 stop_reason |
| 8 | 格式优先选择 | 下游 OpenAI 优先上游 OpenAI，下游 Claude 优先上游 Anthropic |
| 9 | Claude Content: string | string 格式 content |
| 10 | Claude Content: array[text] | array[text] 格式 content |
| 11 | Claude Content: thinking | thinking 类型 content |
| 12 | Claude Content: tool_use | tool_use 类型 content |
| 13 | Claude Content: mixed | 混合格式支持 |
| 14 | Tool Use: OpenAI → OpenAI | OpenAI 格式工具调用 |
| 15 | Tool Use: Claude → Claude | Claude 格式工具调用 |
| 16 | Unsupported Protocol Error | 不支持的协议返回 400 错误 |

## 运行测试

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

## 测试状态

✅ **17/17 通过 (100.0%)** + `verify_passthrough.py` 37/37

所有核心功能都有对应的测试验证。
