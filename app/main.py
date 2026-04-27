from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from .config import get_model_config, get_settings
from .logger import logger
from .models import ClaudeRequest
from .proxy import call_claude, call_openai, stream_claude, stream_openai


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
    for key in ("temperature", "top_p", "max_tokens", "stop_sequences", "top_k", "metadata"):
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
    conv_id = logger.log_request(model_name, upstream["upstream_model"], messages, client_id, extra)

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
                logger.log_response(model_name, upstream["upstream_model"], conv_id, "", {}, f"error: {e}")
                raise
            finally:
                full_text = "".join(text_parts)
                logger.log_response(model_name, upstream["upstream_model"], conv_id, full_text, {"input_tokens": 0, "output_tokens": 0}, finish_reason)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # Non-streaming
    provider = upstream["provider"]
    if provider == "claude":
        result = await call_claude(req, upstream)
    else:
        result = await call_openai(req, upstream)

    text = ""
    for block in result.get("content", []):
        text += block.get("text", "")

    usage = result.get("usage", {})
    logger.log_response(model_name, upstream["upstream_model"], conv_id, text, usage, result.get("stop_reason", "end_turn"))
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)
