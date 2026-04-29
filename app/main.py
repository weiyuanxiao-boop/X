from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

from .config import get_model_config, get_settings, get_logger
from .logger import logger as conversation_logger
from .models import ClaudeRequest, OpenAIRequest
from .proxy import (
    call_claude_passthrough, stream_claude_passthrough,
    call_openai_passthrough, stream_openai_passthrough
)

logger = get_logger("main")

settings = get_settings()
model_config = get_model_config()

app = FastAPI(title="LLM Proxy Gateway", version="1.0.0")


# ── Claude API ──────────────────────────────────────────────────

@app.post("/v1/messages")
async def create_message(req: ClaudeRequest, request: Request):
    """Claude-compatible messages endpoint.
    
    Only supports upstream models with Anthropic/Claude API format.
    Returns error if upstream doesn't support Claude API format.
    """
    model_name = req.model or model_config._default
    
    try:
        # Request anthropic format - will raise error if not available
        upstream = model_config.get_upstream_info(model_name, downstream_format="anthropic")
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"Model {model_name} does not support Claude API format: {str(e)}"}
        )
    
    provider = upstream["provider"]
    client_id = request.client.host if request.client else "unknown"

    # Log complete request as-is
    request_data = req.model_dump(exclude_none=True)
    conv_id = conversation_logger.log_request(model_name, upstream["upstream_model"], request_data, client_id)

    if req.stream:
        async def event_stream():
            # Collect stream data for logging
            collected_content = []
            collected_thinking = []
            collected_tool_uses = []
            stop_reason = "end_turn"
            input_tokens = 0
            output_tokens = 0
            
            try:
                # Direct passthrough: Claude → Claude
                logger.info("Claude → Claude: Direct passthrough")
                async for chunk in stream_claude_passthrough(req, upstream):
                    yield chunk
                    # Collect data for logging
                    try:
                        data = chunk.strip()
                        if data.startswith("data: ") and data != "data: [DONE]":
                            import json as _json
                            evt = _json.loads(data[6:])
                            evt_type = evt.get("type", "")
                            
                            if evt_type == "content_block_start":
                                content_block = evt.get("content_block", {})
                                if content_block.get("type") == "thinking":
                                    collected_thinking.append("")
                                elif content_block.get("type") == "text":
                                    collected_content.append("")
                                elif content_block.get("type") == "tool_use":
                                    collected_tool_uses.append({
                                        "type": "tool_use",
                                        "id": content_block.get("id", ""),
                                        "name": content_block.get("name", ""),
                                        "input": {}
                                    })
                            elif evt_type == "content_block_delta":
                                delta = evt.get("delta", {})
                                delta_type = delta.get("type", "")
                                if delta_type == "thinking_delta":
                                    if collected_thinking:
                                        collected_thinking[-1] += delta.get("thinking", "")
                                elif delta_type == "text_delta":
                                    if collected_content:
                                        collected_content[-1] += delta.get("text", "")
                                elif delta_type == "input_json_delta":
                                    # For tool use input
                                    if collected_tool_uses:
                                        # Parse partial JSON if possible
                                        try:
                                            partial = delta.get("partial_json", "")
                                            if partial:
                                                # Accumulate partial JSON
                                                if not hasattr(collected_tool_uses[-1], '_partial_input'):
                                                    collected_tool_uses[-1]['_partial_input'] = ""
                                                collected_tool_uses[-1]['_partial_input'] += partial
                                        except:
                                            pass
                            elif evt_type == "message_delta":
                                delta = evt.get("delta", {})
                                if "stop_reason" in delta:
                                    stop_reason = delta["stop_reason"]
                            elif evt_type == "message_start":
                                msg = evt.get("message", {})
                                usage = msg.get("usage", {})
                                input_tokens = usage.get("input_tokens", 0)
                    except Exception:
                        pass
            except Exception as e:
                logger.exception(f"Stream error for {model_name}: {e}")
                raise
            finally:
                # Build Claude-format response for logging
                response_content = []
                for thinking in collected_thinking:
                    if thinking:
                        response_content.append({"type": "thinking", "thinking": thinking})
                for text in collected_content:
                    if text:
                        response_content.append({"type": "text", "text": text})
                for tool_use in collected_tool_uses:
                    # Clean up partial input
                    tool_use_clean = {k: v for k, v in tool_use.items() if not k.startswith('_')}
                    if tool_use_clean.get("id"):
                        response_content.append(tool_use_clean)
                
                response_data = {
                    "content": response_content,
                    "stop_reason": stop_reason,
                    "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}
                }
                conversation_logger.log_response(model_name, upstream["upstream_model"], conv_id, response_data)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # Non-streaming - Direct passthrough
    logger.info("Claude → Claude: Direct passthrough")
    result = await call_claude_passthrough(req, upstream)

    # Log complete response as-is
    conversation_logger.log_response(model_name, upstream["upstream_model"], conv_id, result)
    logger.info(f"Completed request {conv_id} for {model_name}")

    return result


# ── OpenAI-compatible API ──────────────────────────────────────

@app.post("/v1/chat/completions")
async def create_chat_completion(req: OpenAIRequest, request: Request):
    """OpenAI-compatible chat completions endpoint.
    
    Only supports upstream models with OpenAI API format.
    Returns error if upstream doesn't support OpenAI API format.
    """
    model_name = req.model or model_config._default
    
    try:
        # Request openai format - will raise error if not available
        upstream = model_config.get_upstream_info(model_name, downstream_format="openai")
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"Model {model_name} does not support OpenAI API format: {str(e)}"}
        )
    
    provider = upstream["provider"]
    client_id = request.client.host if request.client else "unknown"

    # Log complete request as-is
    request_data = req.model_dump(exclude_none=True)
    conv_id = conversation_logger.log_request(model_name, upstream["upstream_model"], request_data, client_id)

    if req.stream:
        logger.info(f"Starting OpenAI stream request, upstream provider: {provider}")

        async def event_stream():
            # Collect stream data for logging
            collected_content = []
            collected_reasoning = []
            collected_tool_calls = []
            finish_reason = "stop"
            input_tokens = 0
            completion_tokens = 0
            
            try:
                # Direct passthrough: OpenAI → OpenAI
                logger.info("OpenAI → OpenAI: Direct passthrough")
                async for chunk in stream_openai_passthrough(req, upstream):
                    yield chunk
                    # Collect data for logging
                    try:
                        data = chunk.strip()
                        if data.startswith("data: ") and data != "data: [DONE]":
                            import json as _json
                            evt = _json.loads(data[6:])
                            choice = evt.get("choices", [{}])[0]
                            delta = choice.get("delta", {})
                            
                            # Collect content
                            if "content" in delta and delta["content"]:
                                if not collected_content:
                                    collected_content.append("")
                                collected_content[-1] += delta["content"]
                            
                            # Collect reasoning_content
                            if "reasoning_content" in delta and delta["reasoning_content"]:
                                if not collected_reasoning:
                                    collected_reasoning.append("")
                                collected_reasoning[-1] += delta["reasoning_content"]
                            
                            # Collect tool_calls
                            if "tool_calls" in delta:
                                for tc in delta["tool_calls"]:
                                    idx = tc.get("index", 0)
                                    while len(collected_tool_calls) <= idx:
                                        collected_tool_calls.append({"index": len(collected_tool_calls), "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                    
                                    if tc.get("id"):
                                        collected_tool_calls[idx]["id"] = tc["id"]
                                    if tc.get("type"):
                                        collected_tool_calls[idx]["type"] = tc["type"]
                                    if "function" in tc:
                                        if tc["function"].get("name"):
                                            collected_tool_calls[idx]["function"]["name"] = tc["function"]["name"]
                                        if tc["function"].get("arguments"):
                                            collected_tool_calls[idx]["function"]["arguments"] += tc["function"]["arguments"]
                            
                            # Collect finish_reason
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                            
                            # Collect usage
                            if evt.get("usage"):
                                usage = evt["usage"]
                                input_tokens = usage.get("prompt_tokens", 0)
                                completion_tokens = usage.get("completion_tokens", 0)
                    except Exception:
                        pass
            except Exception as e:
                logger.exception(f"Stream error for {model_name}: {e}")
                raise
            finally:
                # Build OpenAI-format response for logging
                message = {"role": "assistant"}
                if collected_content and any(collected_content):
                    message["content"] = "".join(collected_content)
                if collected_reasoning and any(collected_reasoning):
                    message["reasoning_content"] = "".join(collected_reasoning)
                if collected_tool_calls:
                    # Clean up tool calls
                    tool_calls = []
                    for tc in collected_tool_calls:
                        if tc.get("id"):
                            tool_calls.append({
                                "id": tc["id"],
                                "type": tc.get("type", "function"),
                                "function": {
                                    "name": tc["function"].get("name", ""),
                                    "arguments": tc["function"].get("arguments", "")
                                }
                            })
                    if tool_calls:
                        message["tool_calls"] = tool_calls
                
                response_data = {
                    "choices": [{
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason
                    }],
                    "usage": {
                        "prompt_tokens": input_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": input_tokens + completion_tokens
                    }
                }
                conversation_logger.log_response(model_name, upstream["upstream_model"], conv_id, response_data)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # Non-streaming - Direct passthrough
    logger.info("OpenAI → OpenAI: Direct passthrough")
    result = await call_openai_passthrough(req, upstream)

    # Log complete response as-is
    conversation_logger.log_response(model_name, upstream["upstream_model"], conv_id, result)
    logger.info(f"Completed request {conv_id} for {model_name}")

    return result


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    """List available models."""
    return {"models": [{"id": m, "object": "model"} for m in model_config.list_models()]}
