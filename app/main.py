import json
import re
import threading
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .auth import authenticate
from .config import get_logger, get_model_config
from .logger import logger as conversation_logger
from .proxy import (
    build_body,
    build_upstream_headers,
    build_upstream_url,
    collect_sse_line,
    create_client,
    filter_response_headers,
    is_event_stream,
    parse_json_body,
)

logger = get_logger("main")

model_config = get_model_config()

app = FastAPI(title="LLM Proxy Gateway", version="2.0.0")

# 流式日志事件上限，防止超长响应把日志撑爆
_SSE_MAX_EVENTS = 5000

# 需要记录鉴权失败的对话端点（其他路径不写对话日志，避免噪音）
_CONVERSATION_PATHS = {"/v1/messages", "/v1/chat/completions", "/v1/responses"}

_auth_config = model_config.get_auth_config()
if _auth_config["enabled"]:
    if _auth_config["keys"]:
        logger.info(f"Access auth ENABLED ({len(_auth_config['keys'])} key(s)); loopback bypassed")
    else:
        logger.warning("Access auth ENABLED but no keys configured — all non-local requests will be rejected")
else:
    logger.info("Access auth disabled — all requests allowed")


def _error(status_code: int, error_type: str, message: str, downstream_format: str) -> JSONResponse:
    """按下游协议返回标准错误结构，便于官方 SDK 正确解析。"""
    if downstream_format == "anthropic":
        content = {"type": "error", "error": {"type": error_type, "message": message}}
    else:
        content = {"error": {"message": message, "type": error_type, "param": None, "code": None}}
    return JSONResponse(status_code=status_code, content=content)


# ── 资源限制 ──────────────────────────────────────────────────

_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_rate_buckets: dict[str, tuple[int, int]] = {}
_rate_lock = threading.Lock()


class _BodyTooLarge(Exception):
    """请求体超过 limits.max_body_mb。"""


def _rate_limited(client_id: str) -> bool:
    """按来源做固定窗口限流（每分钟）。limits.rate_limit_per_minute=0 表示不限。"""
    limit = model_config.get_limits()["rate_limit_per_minute"]
    if limit <= 0:
        return False

    window = int(time.monotonic() // 60)
    with _rate_lock:
        start, count = _rate_buckets.get(client_id, (window, 0))
        if start != window:
            start, count = window, 0
        count += 1
        _rate_buckets[client_id] = (start, count)
        if len(_rate_buckets) > 10000:      # 防止来源 IP 过多导致内存增长
            for key in [k for k, v in _rate_buckets.items() if v[0] != window]:
                _rate_buckets.pop(key, None)

    return count > limit


async def _read_body_limited(request: Request) -> bytes:
    """边读边计数，超限立刻中断，避免把超大 body 整个读进内存。

    ``limits.max_body_mb = 0`` 表示不限制。
    """
    mb = model_config.get_limits()["max_body_mb"]
    limit = mb * 1024 * 1024 if mb > 0 else None

    if limit is not None:
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) > limit:
                    raise _BodyTooLarge()
            except ValueError:
                pass

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if limit is not None and total > limit:
            raise _BodyTooLarge()
        chunks.append(chunk)

    return b"".join(chunks)


# ── 访问鉴权 ──────────────────────────────────────────────────

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """本地连接放行；非本地必须携带 model_config.yaml 中配置的 key。

    /health 不做鉴权（留给探活），其余路径（含 /v1/models、/docs）都要过。
    """
    path = request.url.path
    if path == "/health":
        return await call_next(request)

    client_id = request.client.host if request.client else "unknown"

    # 限流先于鉴权：401 同样消耗配额，避免 key 爆破和日志放大
    if _rate_limited(client_id):
        logger.warning(f"429 {client_id} {path}: rate limit exceeded")
        downstream_format = "anthropic" if path == "/v1/messages" else "openai"
        response = _error(429, "rate_limit_error", "Too many requests", downstream_format)
        response.headers["Retry-After"] = "60"
        return response

    result = authenticate(request)
    request.state.auth = result

    if result.allowed:
        return await call_next(request)

    if path in _CONVERSATION_PATHS:
        # 不落盘未鉴权的请求体，避免任意内容写入磁盘
        conversation_logger.log_exchange(
            client_id=client_id,
            downstream_model="unknown",
            upstream_model="",
            request={"__withheld__": "unauthenticated request body not logged"},
            response={"status": 401, "error": result.reason},
            auth={"mode": result.mode, "principal": result.principal},
        )
    logger.warning(f"401 {client_id} {path}: {result.reason}")

    downstream_format = "anthropic" if path == "/v1/messages" else "openai"
    return _error(401, "authentication_error", result.reason, downstream_format)


# ── 统一转发逻辑 ───────────────────────────────────────────────

async def _handle(request: Request, downstream_format: str, endpoint: str) -> Response:
    """同协议字节级透传。

    Args:
        downstream_format: 下游协议，决定上游 base_url 与鉴权头（anthropic / openai）
        endpoint: 端点类型，决定上游路径（messages / chat / responses）
    
    请求体、请求头、响应体的原始字节全部保持下游/上游发来的样子，
    只有顶层 model 字段的值被替换为路由到的 upstream_model。
    跨协议请求在路由阶段直接拒绝（400）。
    """
    try:
        raw_body = await _read_body_limited(request)
    except _BodyTooLarge:
        limit_mb = model_config.get_limits()["max_body_mb"]
        logger.warning(f"413 body exceeds {limit_mb}MB from "
                       f"{request.client.host if request.client else 'unknown'}")
        return _error(413, "request_too_large",
                      f"Request body exceeds {limit_mb} MB", downstream_format)

    client_id = request.client.host if request.client else "unknown"
    auth_result = getattr(request.state, "auth", None)
    auth_summary = (
        {"mode": auth_result.mode, "principal": auth_result.principal}
        if auth_result else None
    )

    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
    except Exception as exc:
        # 细节只进服务日志，不回显给下游
        logger.warning(f"Invalid JSON body from {client_id}: {exc}")
        message = "Request body is not a valid JSON object"
        conversation_logger.log_exchange(
            client_id=client_id,
            downstream_model="unknown",
            upstream_model="",
            request={"__raw__": raw_body.decode("utf-8", "replace")[:4000]},
            response={"status": 400, "error": message},
            auth=auth_summary,
        )
        return _error(400, "invalid_request_error", message, downstream_format)

    # 超大的请求体只记元信息，避免单条日志把磁盘撑爆
    max_logged = model_config.get_limits()["max_logged_body_mb"] * 1024 * 1024
    if max_logged and len(raw_body) > max_logged:
        request_for_log: object = {"__truncated__": True, "bytes": len(raw_body)}
    else:
        request_for_log = payload

    model_name = payload.get("model") or model_config._default

    try:
        # 只接受与下游同协议的上游；跨协议在这里抛 ValueError
        upstream = model_config.get_upstream_info(model_name, downstream_format=downstream_format)
    except ValueError as exc:
        conversation_logger.log_exchange(
            client_id=client_id,
            downstream_model=model_name,
            upstream_model="",
            request=request_for_log,
            response={"status": 400, "error": str(exc)},
            auth=auth_summary,
        )
        return _error(400, "invalid_request_error", str(exc), downstream_format)

    body = build_body(raw_body, payload, upstream["upstream_model"])
    headers = build_upstream_headers(request.headers, downstream_format, upstream)
    url = build_upstream_url(upstream, endpoint)

    client = create_client()
    try:
        upstream_request = client.build_request("POST", url, headers=headers, content=body)
        resp = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.exception(f"Upstream connection failed for {model_name}: {exc}")
        # 具体异常（可能含内网地址、代理信息）只进日志，不回显给下游
        detail = f"{type(exc).__name__}: {exc}"[:500]
        conversation_logger.log_exchange(
            client_id=client_id,
            downstream_model=model_name,
            upstream_model=upstream["upstream_model"],
            request=request_for_log,
            response={"status": 502, "error": detail},
            auth=auth_summary,
        )
        return _error(502, "upstream_error", "Failed to reach upstream", downstream_format)

    response_headers = filter_response_headers(resp.headers)

    # 上游返回什么形态就回什么形态，不依据下游的 stream 字段猜测
    if is_event_stream(resp.headers):
        async def relay():
            buffer = b""
            events: list = []
            truncated = False
            total_bytes = 0
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                    if truncated:
                        continue
                    total_bytes += len(chunk)
                    # 响应超过日志上限：停止采集，只保留元信息（转发不受影响）
                    if max_logged and total_bytes > max_logged:
                        events.clear()
                        events.append({"__truncated__": True, "bytes": total_bytes})
                        truncated = True
                        buffer = b""
                        continue
                    buffer += chunk
                    while True:
                        idx = buffer.find(b"\n")
                        if idx < 0:
                            break
                        line, buffer = buffer[:idx], buffer[idx + 1:]
                        if collect_sse_line(line, events, _SSE_MAX_EVENTS):
                            events.append({"__truncated__": True})
                            truncated = True
                            break
                if buffer and not truncated:
                    collect_sse_line(buffer, events, _SSE_MAX_EVENTS)
            finally:
                await resp.aclose()
                await client.aclose()
                conversation_logger.log_exchange(
                    client_id=client_id,
                    downstream_model=model_name,
                    upstream_model=upstream["upstream_model"],
                    request=request_for_log,
                    response={
                        "status": resp.status_code,
                        "headers": response_headers,
                        "stream": True,
                        "events": events,
                    },
                    auth=auth_summary,
                )

        return StreamingResponse(
            relay(),
            status_code=resp.status_code,
            headers=response_headers,
        )

    try:
        upstream_body = await resp.aread()
    finally:
        await resp.aclose()
        await client.aclose()

    # 响应体同样受 max_logged_body_mb 限制，避免大响应把磁盘写满
    if max_logged and len(upstream_body) > max_logged:
        logged_body: object = {"__truncated__": True, "bytes": len(upstream_body)}
    else:
        logged_body = parse_json_body(upstream_body)

    conv_id = conversation_logger.log_exchange(
        client_id=client_id,
        downstream_model=model_name,
        upstream_model=upstream["upstream_model"],
        request=request_for_log,
        response={
            "status": resp.status_code,
            "headers": response_headers,
            "body": logged_body,
        },
        auth=auth_summary,
    )
    logger.info(f"Completed request {conv_id} for {model_name} (status {resp.status_code})")

    return Response(
        content=upstream_body,
        status_code=resp.status_code,
        headers=response_headers,
    )


# ── Claude API ────────────────────────────────────────────────

@app.post("/v1/messages")
async def create_message(request: Request):
    """Claude 协议端点：仅透传给配置了 anthropic base_url 的上游。"""
    return await _handle(request, "anthropic", "messages")


# ── OpenAI-compatible API ─────────────────────────────────────

@app.post("/v1/chat/completions")
async def create_chat_completion(request: Request):
    """OpenAI Chat Completions 端点。"""
    return await _handle(request, "openai", "chat")


@app.post("/v1/responses")
async def create_response(request: Request):
    """OpenAI Responses 端点：透传给上游 /v1/responses。"""
    return await _handle(request, "openai", "responses")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    """List available models with supported formats and alias info."""
    models = []

    actual_models = list(model_config._models.keys())
    aliases = model_config._aliases

    for model_name in actual_models:
        try:
            model_info = {"id": model_name, "object": "model", "supported_formats": [], "is_alias": False}

            try:
                model_config.get_upstream_info(model_name, downstream_format="openai")
                model_info["supported_formats"].append("openai")
            except ValueError:
                pass

            try:
                model_config.get_upstream_info(model_name, downstream_format="anthropic")
                model_info["supported_formats"].append("anthropic")
            except ValueError:
                pass

            models.append(model_info)
        except Exception:
            models.append({"id": model_name, "object": "model", "supported_formats": [], "is_alias": False})

    for alias_name, target_model in aliases.items():
        target_info = next((m for m in models if m["id"] == target_model), None)
        models.append({
            "id": alias_name,
            "object": "model",
            "supported_formats": target_info["supported_formats"] if target_info else [],
            "is_alias": True,
            "alias_of": target_model,
        })

    return {"models": models}


@app.get("/v1/aliases")
async def list_aliases():
    """List all configured model aliases."""
    return {"aliases": [{"alias": k, "target": v} for k, v in model_config._aliases.items()]}


@app.post("/v1/aliases")
async def create_alias(alias: str, target: str):
    """Add or update a model alias."""
    # 限制字符集：避免换行/冒号等字符写坏 aliases.yaml
    if not _ALIAS_PATTERN.fullmatch(alias):
        raise HTTPException(
            status_code=400,
            detail="alias must be 1-64 chars, start with a letter or digit, "
                   "and contain only letters, digits, dot, underscore or hyphen",
        )
    if target not in model_config._models:
        raise HTTPException(status_code=400, detail=f"Target model '{target}' not found")

    is_update = alias in model_config._aliases
    model_config._aliases[alias] = target
    model_config.save_aliases()

    message = "Alias updated (saved to aliases.yaml)" if is_update else "Alias added (saved to aliases.yaml)"
    return {"alias": alias, "target": target, "message": message}


@app.delete("/v1/aliases/{alias}")
async def delete_alias(alias: str):
    """Remove a model alias."""
    if alias not in model_config._aliases:
        raise HTTPException(status_code=404, detail=f"Alias '{alias}' not found")

    del model_config._aliases[alias]
    model_config.save_aliases()

    return {"alias": alias, "message": "Alias removed (updated aliases.yaml)"}
