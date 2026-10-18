"""上游转发层 —— 字节级透传。

本模块刻意**不**对请求/响应做任何结构建模或校验：
    - 下游 raw body 原样发往上游，唯一改动的字节是顶层 "model" 字段的值
      （替换为路由到的 upstream_model）。字段顺序、未知字段、嵌套结构、
      unicode 转义、数字写法全部保持下游发来的原样。
    - 上游响应体按原始字节回传，不重新序列化。
    - 请求头/响应头按白名单过滤后透传，只剔除逐跳头和需要由本服务覆盖的头。

跨协议（Claude -> OpenAI 或反向）不在这里处理，路由阶段即拒绝。
"""

import json
from typing import Mapping

import httpx

from .config import get_logger, get_model_config

logger = get_logger("proxy")

# 上游超时：连接 15s，读写 10min（长推理/长输出不应被中转层掐断）
UPSTREAM_TIMEOUT = httpx.Timeout(600.0, connect=15.0)

# ── HTTP 头过滤 ────────────────────────────────────────────────

# 逐跳头，不能跨代理转发
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# ── 请求头采用白名单 ──────────────────────────────────────────
# 只放行协议相关头。cookie / x-forwarded-for / x-real-ip / referer / origin /
# 任意自定义头都不会到达上游，避免把下游会话凭证或内网信息带出去。
_PASS_HEADERS_COMMON = {"accept", "accept-language", "user-agent"}
_PASS_HEADERS_ANTHROPIC = {"anthropic-beta", "anthropic-version"}
_PASS_HEADERS_OPENAI = {"openai-organization", "openai-project", "openai-beta"}

# 响应侧额外剔除：body 已被 httpx 解压且长度可变；cookie 与 CORS 头不应由中转层转发；
# server 头会暴露上游技术栈
_DROP_RESPONSE = _HOP_BY_HOP | {
    "content-length",
    "content-encoding",
    "set-cookie",
    "set-cookie2",
    "server",
}
_DROP_RESPONSE_PREFIXES = ("access-control-", "x-internal-")


def create_client() -> httpx.AsyncClient:
    """构造上游客户端。

    ``trust_env`` 默认为 False：不读取 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 等
    环境变量，避免 API key 与对话内容被环境中的代理截获。需要走代理时，在
    ``model_config.yaml`` 的 ``upstream.proxy`` 显式指定地址。
    """
    config = get_model_config().get_upstream_config()
    return httpx.AsyncClient(
        timeout=UPSTREAM_TIMEOUT,
        trust_env=config["trust_env"],
        proxy=config["proxy"],
    )


def build_upstream_headers(
    downstream_headers: Mapping[str, str],
    downstream_format: str,
    upstream: dict,
) -> dict:
    """构造上游请求头（白名单）。

    仅放行 ``accept`` / ``user-agent`` / ``anthropic-beta`` / ``openai-organization``
    等协议头；鉴权头换成上游凭证。
    """
    allowed = set(_PASS_HEADERS_COMMON)
    allowed |= _PASS_HEADERS_ANTHROPIC if downstream_format == "anthropic" else _PASS_HEADERS_OPENAI

    headers = {}
    for key, value in downstream_headers.items():
        # 空值不参与覆盖：例如下游传了空的 anthropic-version，应回落到配置值
        if key.lower() in allowed and value and value.strip():
            headers[key] = value

    if downstream_format == "anthropic":
        headers["x-api-key"] = upstream["api_key"]
        if "anthropic-version" not in {k.lower() for k in headers}:
            # 模型未配置 api_version 时给出官方稳定版本，避免发送空值
            headers["anthropic-version"] = upstream.get("api_version") or "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {upstream['api_key']}"

    headers["content-type"] = "application/json"
    return headers


def filter_response_headers(headers: httpx.Headers) -> dict:
    """过滤上游响应头，保留限流、请求 ID 等下游排查所需信息。

    剔除逐跳头、编码相关头、``set-cookie`` 与 ``access-control-*``。
    """
    result: dict[str, str] = {}
    canonical: dict[str, str] = {}   # lower -> 实际使用的 key

    for key, value in headers.multi_items():
        lower = key.lower()
        if lower in _DROP_RESPONSE or lower.startswith(_DROP_RESPONSE_PREFIXES):
            continue
        if lower in canonical:
            used = canonical[lower]
            result[used] = f"{result[used]}, {value}"
        else:
            canonical[lower] = key
            result[key] = value

    return result


# ── 顶层 model 字段改写（只在字节层面动这一处）───────────────

_QUOTE = 0x22
_BACKSLASH = 0x5C
_OPEN = (0x7B, 0x5B)   # { [
_CLOSE = (0x7D, 0x5D)  # } ]
_WS = b" \t\r\n"


def _skip_string(raw: bytes, start: int) -> int:
    """start 指向开引号，返回闭引号之后的下标（处理转义）。"""
    i = start + 1
    n = len(raw)
    while i < n:
        byte = raw[i]
        if byte == _BACKSLASH:
            i += 2
            continue
        if byte == _QUOTE:
            return i + 1
        i += 1
    return n


def rewrite_top_level_model(raw: bytes, new_model: str) -> bytes | None:
    """把 JSON 顶层 "model" 的值替换为 new_model，其余字节不动。

    UTF-8 编码下所有多字节字符的字节值都 >= 0x80，不会撞上 JSON 的结构字符，
    因此按字节扫描是安全的，中文内容不会被破坏。

    Returns:
        改写后的 body；若 body 中不存在顶层 model 字段则返回 None。
    """
    n = len(raw)
    i = 0
    depth = 0

    while i < n:
        byte = raw[i]

        if byte == _QUOTE:
            end = _skip_string(raw, i)
            if depth == 1 and raw[i:end] == b'"model"':
                pos = end
                while pos < n and raw[pos] in _WS:
                    pos += 1
                if pos < n and raw[pos] == 0x3A:      # ':'
                    pos += 1
                    while pos < n and raw[pos] in _WS:
                        pos += 1
                    if pos < n and raw[pos] == _QUOTE:
                        value_end = _skip_string(raw, pos)
                        encoded = json.dumps(new_model, ensure_ascii=False).encode("utf-8")
                        return raw[:pos] + encoded + raw[value_end:]
            i = end
            continue

        if byte in _OPEN:
            depth += 1
        elif byte in _CLOSE:
            depth -= 1
        i += 1

    return None


def build_body(raw_body: bytes, payload: dict, upstream_model: str) -> bytes:
    """返回发往上游的 body。

    优先字节级改写；若下游 body 没有顶层 model（罕见），退化为重新序列化。
    """
    rewritten = rewrite_top_level_model(raw_body, upstream_model)
    if rewritten is not None:
        return rewritten
    logger.warning("Request body has no top-level 'model' field, re-serializing")
    return json.dumps({**payload, "model": upstream_model}, ensure_ascii=False).encode("utf-8")


# ── URL ───────────────────────────────────────────────────────

# 下游端点 → 上游路径。三个端点都是同协议直通，不需要格式转换。
_ENDPOINT_PATH = {
    "messages": "/v1/messages",              # Claude
    "chat": "/v1/chat/completions",          # OpenAI Chat Completions
    "responses": "/v1/responses",            # OpenAI Responses
}


def build_upstream_url(upstream: dict, endpoint: str) -> str:
    base = upstream["base_url"].rstrip("/")
    return f"{base}{_ENDPOINT_PATH[endpoint]}"


# ── 响应识别 ──────────────────────────────────────────────────

def is_event_stream(headers: httpx.Headers) -> bool:
    return "text/event-stream" in headers.get("content-type", "").lower()


def parse_json_body(content: bytes):
    """尽最大努力把 bytes 解析为 JSON，失败则返回原文。"""
    try:
        return json.loads(content)
    except Exception:
        return content.decode("utf-8", errors="replace")


def collect_sse_line(line: bytes, events: list, max_events: int = 5000) -> bool:
    """把一行 SSE 解析进 events（仅用于日志），返回是否已达条数上限。"""
    if len(events) >= max_events:
        return True

    text = line.decode("utf-8", errors="replace").strip()
    if not text.startswith("data:"):
        return False

    chunk = text[5:].strip()
    if not chunk or chunk == "[DONE]":
        return False

    try:
        events.append(json.loads(chunk))
    except Exception:
        events.append({"__raw__": chunk})
    return False
