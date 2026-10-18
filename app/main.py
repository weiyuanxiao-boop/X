import json

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import get_logger, get_model_config
from .logger import logger as conversation_logger
from .proxy import (
    UPSTREAM_TIMEOUT,
    build_body,
    build_upstream_headers,
    build_upstream_url,
    collect_sse_line,
    filter_response_headers,
    is_event_stream,
    parse_json_body,
)

logger = get_logger("main")

model_config = get_model_config()

app = FastAPI(title="LLM Proxy Gateway", version="2.0.0")

# 流式日志事件上限，防止超长响应把日志撑爆
_SSE_MAX_EVENTS = 5000


def _error(status_code: int, error_type: str, message: str, downstream_format: str) -> JSONResponse:
    """按下游协议返回标准错误结构，便于官方 SDK 正确解析。"""
    if downstream_format == "anthropic":
        content = {"type": "error", "error": {"type": error_type, "message": message}}
    else:
        content = {"error": {"message": message, "type": error_type, "param": None, "code": None}}
    return JSONResponse(status_code=status_code, content=content)


# ── 统一转发逻辑 ───────────────────────────────────────────────

async def _handle(request: Request, downstream_format: str) -> Response:
    """同协议字节级透传。

    请求体、请求头、响应体的原始字节全部保持下游/上游发来的样子，
    只有顶层 model 字段的值被替换为路由到的 upstream_model。
    跨协议请求在路由阶段直接拒绝（400）。
    """
    raw_body = await request.body()
    client_id = request.client.host if request.client else "unknown"

    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
    except Exception as exc:
        message = f"Request body is not a valid JSON object: {exc}"
        conversation_logger.log_exchange(
            client_id=client_id,
            downstream_model="unknown",
            upstream_model="",
            request={"__raw__": raw_body.decode("utf-8", "replace")[:4000]},
            response={"status": 400, "error": message},
        )
        return _error(400, "invalid_request_error", message, downstream_format)

    model_name = payload.get("model") or model_config._default

    try:
        # 只接受与下游同协议的上游；跨协议在这里抛 ValueError
        upstream = model_config.get_upstream_info(model_name, downstream_format=downstream_format)
    except ValueError as exc:
        conversation_logger.log_exchange(
            client_id=client_id,
            downstream_model=model_name,
            upstream_model="",
            request=payload,
            response={"status": 400, "error": str(exc)},
        )
        return _error(400, "invalid_request_error", str(exc), downstream_format)

    body = build_body(raw_body, payload, upstream["upstream_model"])
    headers = build_upstream_headers(request.headers, downstream_format, upstream)
    url = build_upstream_url(upstream, downstream_format)

    client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)
    try:
        upstream_request = client.build_request("POST", url, headers=headers, content=body)
        resp = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        message = f"Failed to reach upstream: {exc}"
        logger.exception(f"Upstream connection failed for {model_name}: {exc}")
        conversation_logger.log_exchange(
            client_id=client_id,
            downstream_model=model_name,
            upstream_model=upstream["upstream_model"],
            request=payload,
            response={"status": 502, "error": message},
        )
        return _error(502, "upstream_error", message, downstream_format)

    response_headers = filter_response_headers(resp.headers)

    # 上游返回什么形态就回什么形态，不依据下游的 stream 字段猜测
    if is_event_stream(resp.headers):
        async def relay():
            buffer = b""
            events: list = []
            truncated = False
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                    if truncated:
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
                    request=payload,
                    response={
                        "status": resp.status_code,
                        "headers": response_headers,
                        "stream": True,
                        "events": events,
                    },
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

    conv_id = conversation_logger.log_exchange(
        client_id=client_id,
        downstream_model=model_name,
        upstream_model=upstream["upstream_model"],
        request=payload,
        response={
            "status": resp.status_code,
            "headers": response_headers,
            "body": parse_json_body(upstream_body),
        },
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
    return await _handle(request, "anthropic")


# ── OpenAI-compatible API ─────────────────────────────────────

@app.post("/v1/chat/completions")
async def create_chat_completion(request: Request):
    """OpenAI 协议端点：仅透传给配置了 openai base_url 的上游。"""
    return await _handle(request, "openai")


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
