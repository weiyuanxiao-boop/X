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

from .config import get_logger

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

# 请求侧额外剔除：这些必须由本服务重新决定
_DROP_REQUEST = _HOP_BY_HOP | {
    "host",
    "content-length",
    "content-type",     # 统一按 application/json 发送
    "accept-encoding",  # 交给 httpx 协商，避免解压后仍声称压缩
    "authorization",    # 换成上游凭证
    "x-api-key",        # 换成上游凭证
}

# 响应侧额外剔除：body 已被 httpx 解压且长度可变
_DROP_RESPONSE = _HOP_BY_HOP | {
    "content-length",
    "content-encoding",
}


def build_upstream_headers(
    downstream_headers: Mapping[str, str],
    downstream_format: str,
    upstream: dict,
) -> dict:
    """构造上游请求头。

    下游的业务头（anthropic-beta / openai-organization / accept / user-agent …）
    一律透传；鉴权头换成上游凭证；anthropic-version 下游带了就用下游的。
    """
    headers = {}
    lowered = set()

    for key, value in downstream_headers.items():
        lower = key.lower()
        if lower in _DROP_REQUEST:
            continue
        headers[key] = value
        lowered.add(lower)

    if downstream_format == "anthropic":
        headers["x-api-key"] = upstream["api_key"]
        if "anthropic-version" not in lowered:
            # 模型未配置 api_version 时给出官方稳定版本，避免发送空值
            headers["anthropic-version"] = upstream.get("api_version") or "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {upstream['api_key']}"

    headers["content-type"] = "application/json"
    return headers


def filter_response_headers(headers: httpx.Headers) -> dict:
    """过滤上游响应头，保留限流、请求 ID 等下游排查所需信息。"""
    result: dict[str, str] = {}
    canonical: dict[str, str] = {}   # lower -> 实际使用的 key

    for key, value in headers.multi_items():
        lower = key.lower()
        if lower in _DROP_RESPONSE:
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

_ENDPOINT_PATH = {
    "anthropic": "/v1/messages",
    "openai": "/v1/chat/completions",
}


def build_upstream_url(upstream: dict, downstream_format: str) -> str:
    base = upstream["base_url"].rstrip("/")
    return f"{base}{_ENDPOINT_PATH[downstream_format]}"


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
