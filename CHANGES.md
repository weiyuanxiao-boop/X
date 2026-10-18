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

✅ **16/16 通过 (100.0%)** + `verify_passthrough.py` 29/29

所有核心功能都有对应的测试验证。
