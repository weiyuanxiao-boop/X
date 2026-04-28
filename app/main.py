from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from .config import get_model_config, get_settings, get_logger
from .logger import logger as conversation_logger
from .models import ClaudeRequest, OpenAIRequest
from .proxy import (
    call_claude, call_openai, stream_claude, stream_openai,
    call_openai_passthrough, stream_openai_passthrough,
    _openai_to_claude_request, _claude_to_openai
)

logger = get_logger("main")


def _convert_to_dict(obj):
    """Convert Pydantic model or list of models to dict/list."""
    if obj is None:
        return None
    if isinstance(obj, list):
        return [item.model_dump() if hasattr(item, "model_dump") else item for item in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


settings = get_settings()
model_config = get_model_config()

app = FastAPI(title="LLM Proxy Gateway", version="1.0.0")


def _collect_extra_params(req: ClaudeRequest) -> dict:
    params = {}
    for key in ("temperature", "top_p", "max_tokens", "stop_sequences", "top_k", "metadata", "reasoning_effort", "output_config"):
        val = getattr(req, key, None)
        if val is not None:
            params[key] = val
    
    # Handle system field which may be string or ContentBlock array
    if req.system is not None:
        if isinstance(req.system, list):
            params["system"] = [_convert_to_dict(s) for s in req.system]
        else:
            params["system"] = req.system
    
    # Handle tools and tool_choice which may be Pydantic models
    if req.tools:
        params["tools"] = _convert_to_dict(req.tools)
    if req.tool_choice:
        params["tool_choice"] = _convert_to_dict(req.tool_choice)
    
    return params


@app.post("/v1/messages")
async def create_message(req: ClaudeRequest, request: Request):
    model_name = req.model or model_config._default
    upstream = model_config.get_upstream_info(model_name)
    client_id = request.client.host if request.client else "unknown"

    # Apply default reasoning_effort from config if client doesn't provide one
    if not req.reasoning_effort and not req.output_config:
        config_reasoning_effort = upstream.get("reasoning_effort")
        if config_reasoning_effort:
            req.reasoning_effort = config_reasoning_effort

    # Convert messages to dict, handling both string and ContentBlock array content
    messages = []
    for m in req.messages:
        msg_dict = {"role": m.role}
        if isinstance(m.content, list):
            msg_dict["content"] = [_convert_to_dict(c) for c in m.content]
        else:
            msg_dict["content"] = m.content
        messages.append(msg_dict)

    extra = _collect_extra_params(req)
    conv_id = conversation_logger.log_request(model_name, upstream["upstream_model"], messages, client_id, extra)

    if req.stream:
        async def event_stream():
            provider = upstream["provider"]
            text_parts = []
            finish_reason = "end_turn"
            try:
                if provider == "claude":
                    async for chunk in stream_claude(req, upstream):
                        yield chunk
                        try:
                            import json as _json
                            data = chunk[6:].strip()
                            if data and data != "[DONE]":
                                evt = _json.loads(data)
                                if evt.get("type") == "content_block_delta":
                                    delta = evt.get("delta", {})
                                    text_parts.append(delta.get("text", "") or delta.get("thinking", ""))
                        except Exception:
                            pass
                else:
                    async for chunk in stream_openai(req, upstream):
                        yield chunk
                        try:
                            import json as _json
                            data = chunk[6:].strip()
                            if data and data != "[DONE]":
                                evt = _json.loads(data)
                                if evt.get("type") == "content_block_delta":
                                    text_parts.append(evt.get("delta", {}).get("text", ""))
                        except Exception:
                            pass
            except Exception as e:
                conversation_logger.log_response(model_name, upstream["upstream_model"], conv_id, "", {}, f"error: {e}")
                logger.exception(f"Stream error for {model_name}: {e}")
                raise
            finally:
                full_text = "".join(text_parts)
                conversation_logger.log_response(model_name, upstream["upstream_model"], conv_id, full_text, {"input_tokens": 0, "output_tokens": 0}, finish_reason)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # Non-streaming
    provider = upstream["provider"]
    if provider == "claude":
        result = await call_claude(req, upstream)
    else:
        result = await call_openai(req, upstream)

    text = ""
    for block in result.get("content", []):
        block_text = block.get("text") or block.get("thinking") or ""
        text += block_text

    usage = result.get("usage", {})
    conversation_logger.log_response(model_name, upstream["upstream_model"], conv_id, text, usage, result.get("stop_reason", "end_turn"))
    logger.info(f"Completed request {conv_id} for {model_name}, tokens: {usage}")
    return result


@app.get("/v1/models")
async def list_models():
    models = []
    for name in model_config.list_models():
        models.append({"id": name, "object": "model", "owned_by": "proxy"})
    return {"data": models}


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── OpenAI-compatible API ──────────────────────────────────────

@app.post("/v1/chat/completions")
async def create_chat_completion(req: OpenAIRequest, request: Request):
    """OpenAI-compatible chat completions endpoint."""
    model_name = req.model or model_config._default
    upstream = model_config.get_upstream_info(model_name)
    provider = upstream["provider"]
    client_id = request.client.host if request.client else "unknown"
    
    # Convert messages to dict for logging
    messages = [{"role": m.role, "content": m.content or ""} for m in req.messages]
    
    extra = {
        "temperature": req.temperature,
        "top_p": req.top_p,
        "max_tokens": req.max_tokens,
        "reasoning_effort": req.reasoning_effort,
    }
    if req.stop:
        extra["stop"] = req.stop
    if req.tools:
        extra["tools"] = req.tools
    if req.tool_choice:
        extra["tool_choice"] = req.tool_choice
    
    conv_id = conversation_logger.log_request(model_name, upstream["upstream_model"], messages, client_id, extra)
    
    # Apply default reasoning_effort from config if client doesn't provide one
    if not req.reasoning_effort:
        config_reasoning_effort = upstream.get("reasoning_effort")
        if config_reasoning_effort:
            req.reasoning_effort = config_reasoning_effort
    
    if req.stream:
        logger.info(f"Starting OpenAI stream request, upstream provider: {provider}")
        
        async def event_stream():
            text_parts = []
            try:
                if provider == "openai":
                    # Direct passthrough: OpenAI → OpenAI
                    logger.info("OpenAI → OpenAI: Direct passthrough")
                    async for chunk in stream_openai_passthrough(req, upstream):
                        yield chunk
                        # Collect text for logging
                        try:
                            import json as _json
                            data = chunk[6:].strip()
                            if data and data != "[DONE]":
                                evt = _json.loads(data)
                                if evt.get("choices", [{}])[0].get("delta", {}).get("content"):
                                    text_parts.append(evt["choices"][0]["delta"]["content"])
                        except: pass
                else:
                    # Convert: Claude → OpenAI
                    logger.info("Claude → OpenAI: Converting response")
                    # Convert OpenAI request to Claude request
                    claude_req = _openai_to_claude_request(req)
                    async for chunk in stream_claude(claude_req, upstream):
                        try:
                            import json as _json
                            data = chunk[6:].strip()
                            if data and data != "[DONE]":
                                evt = _json.loads(data)
                                if evt.get("type") == "content_block_delta":
                                    delta = evt.get("delta", {})
                                    text = delta.get("text", "")
                                    thinking = delta.get("thinking", "")
                                    if text or thinking:
                                        openai_chunk = {
                                            "id": f"chatcmpl-{conv_id[:8]}",
                                            "object": "chat.completion.chunk",
                                            "created": int(__import__("time").time()),
                                            "model": model_name,
                                            "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                                        }
                                        if text:
                                            openai_chunk["choices"][0]["delta"]["content"] = text
                                            text_parts.append(text)
                                        if thinking:
                                            openai_chunk["choices"][0]["delta"]["reasoning_content"] = thinking
                                        yield f"data: {_json.dumps(openai_chunk, ensure_ascii=False)}\n\n"
                                elif evt.get("type") == "message_delta":
                                    stop_reason = evt.get("delta", {}).get("stop_reason", "end_turn")
                                    finish_map = {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop", "tool_use": "tool_calls"}
                                    openai_chunk = {
                                        "id": f"chatcmpl-{conv_id[:8]}",
                                        "object": "chat.completion.chunk",
                                        "created": int(__import__("time").time()),
                                        "model": model_name,
                                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_map.get(stop_reason, "stop")}],
                                    }
                                    yield f"data: {_json.dumps(openai_chunk, ensure_ascii=False)}\n\n"
                        except Exception as e:
                            logger.warning(f"Stream parse error: {e}")
                            pass
            except Exception as e:
                conversation_logger.log_response(model_name, upstream["upstream_model"], conv_id, "", {}, f"error: {e}")
                logger.exception(f"Stream error for {model_name}: {e}")
                raise
            finally:
                full_text = "".join(text_parts)
                conversation_logger.log_response(model_name, upstream["upstream_model"], conv_id, full_text, {"input_tokens": 0, "output_tokens": 0}, "stop")
        
        return StreamingResponse(event_stream(), media_type="text/event-stream")
    
    # Non-streaming
    if provider == "openai":
        # Direct passthrough: OpenAI → OpenAI
        logger.info("OpenAI → OpenAI: Direct passthrough")
        result = await call_openai_passthrough(req, upstream)

        logger.info(f"result: {result}")
    else:
        # Convert: Claude → OpenAI
        logger.info("Claude → OpenAI: Converting response")
        claude_req = _openai_to_claude_request(req)
        result = await call_claude(claude_req, upstream)
        result = _claude_to_openai(result)
    
    # Log response
    text = ""
    for choice in result.get("choices", []):
        message = choice.get("message", {})
        content = message.get("content")
        if content:
            text += content
    usage = result.get("usage", {})
    conversation_logger.log_response(model_name, upstream["upstream_model"], conv_id, text or "", usage, "stop")
    logger.info(f"Completed request {conv_id} for {model_name}, tokens: {usage}")
    
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)
