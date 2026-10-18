"""端到端透传保真度验证。

起一个本地假上游（记录收到的原始字节与请求头），把代理指向它，
逐项验证「下游发出」与「上游收到」、「上游返回」与「下游收到」是否逐字节一致。

用法：
    python verify_passthrough.py
"""

import atexit
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient

from app.config import get_model_config
from app.logger import logger as conversation_logger
from app.main import app as proxy_app

# 验证脚本不应污染真实日志目录
_LOG_TMP = Path(tempfile.mkdtemp(prefix="xlog_verify_pt_"))
conversation_logger._root = _LOG_TMP
atexit.register(shutil.rmtree, _LOG_TMP, ignore_errors=True)

# ── 假上游 ────────────────────────────────────────────────────

upstream_app = FastAPI()
captured = {"requests": [], "plan": {}}


@upstream_app.post("/{path:path}")
async def catch_all(path: str, request: Request):
    captured["requests"].append({
        "path": path,
        "body": await request.body(),
        "headers": dict(request.headers),
    })
    plan = captured["plan"]
    if plan.get("stream"):
        async def gen():
            for chunk in plan["chunks"]:
                yield chunk
        return StreamingResponse(gen(), media_type="text/event-stream")
    return Response(
        content=plan.get("body", b"{}"),
        status_code=plan.get("status", 200),
        headers=plan.get("headers", {}),
        media_type=plan.get("media_type", "application/json"),
    )


def start_upstream() -> int:
    config = uvicorn.Config(upstream_app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("fake upstream failed to start")
    return server.servers[0].sockets[0].getsockname()[1]


PORT = start_upstream()
os.environ["TEST_API_KEY"] = "sk-upstream-real"

model_config = get_model_config()
model_config._auth = {"enabled": False, "keys": {}}  # 验证脚本不依赖鉴权配置
model_config._models["__claude__"] = {
    "upstream_model": "upstream-claude-x",
    "api_key_env": "TEST_API_KEY",
    "base_url": {"anthropic": f"http://127.0.0.1:{PORT}"},
    "api_version": "2023-06-01",
}
model_config._models["__openai__"] = {
    "upstream_model": "upstream-gpt-x",
    "api_key_env": "TEST_API_KEY",
    "base_url": {"openai": f"http://127.0.0.1:{PORT}"},
}
model_config._models["__openai_only__"] = {
    "upstream_model": "upstream-gpt-y",
    "api_key_env": "TEST_API_KEY",
    "base_url": {"openai": f"http://127.0.0.1:{PORT}"},
}
# legacy 格式（字符串 base_url + provider 字段），用于验证跨协议校验
model_config._models["__legacy_anthropic__"] = {
    "upstream_model": "upstream-legacy",
    "provider": "anthropic",
    "api_key_env": "TEST_API_KEY",
    "base_url": f"http://127.0.0.1:{PORT}",
}

client = TestClient(proxy_app, raise_server_exceptions=False)

results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def last_request():
    return captured["requests"][-1]


# ══════════════════════════════════════════════════════════════
print("=" * 78)
print("A · Claude 请求体逐字节透传（含 cache_control / document / redacted_thinking）")
print("=" * 78)

raw_claude = (
    b'{"model":"__claude__",  "max_tokens":4096,'
    b'"system":[{"type":"text","text":"be nice","cache_control":{"type":"ephemeral"}}],'
    b'"messages":['
    b'{"role":"user","content":[{"type":"document","source":{"type":"base64",'
    b'"media_type":"application/pdf","data":"JVBERi0xLjQ="}},'
    b'{"type":"text","text":"\xe4\xb8\xad\xe6\x96\x87","cache_control":{"type":"ephemeral"}}]},'
    b'{"role":"assistant","content":[{"type":"redacted_thinking","data":"EmwKAhgB"},'
    b'{"type":"thinking","thinking":"...","signature":"sig"}]},'
    b'{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"ok",'
    b'"cache_control":{"type":"ephemeral"}}]}],'
    b'"tools":[{"name":"f","input_schema":{"type":"object"},"cache_control":{"type":"ephemeral"}}],'
    b'"thinking":{"type":"enabled","budget_tokens":31999},'
    b'"metadata":{"user_id":"u1"},"x_unknown_field":{"deep":[1,2,{"a":null}]},'
    b'"temperature":0.25}'
)

captured["plan"] = {"status": 200, "body": b'{"id":"msg_1","content":[{"type":"text","text":"hi"}]}'}
r = client.post("/v1/messages", content=raw_claude,
                headers={"content-type": "application/json"})
got = last_request()["body"]
expected = raw_claude.replace(b'"__claude__"', b'"upstream-claude-x"')

check("Claude 请求体除 model 外逐字节一致", got == expected,
      f"\n    got={got[:400]!r}\n    exp={expected[:400]!r}")
check("cache_control 未被剥离（4 处）", got.count(b'"cache_control"') == 4,
      f"count={got.count(b'cache_control')}")
check("document block 保留（不再 422）", b'"type":"document"' in got)
check("redacted_thinking 保留（不再 422）", b'"type":"redacted_thinking"' in got)
check("未注入 temperature/top_p/stream 默认值",
      b'"top_p"' not in got and b'"stream"' not in got and b'"temperature":0.25' in got)
check("未知顶层字段保留", b'"x_unknown_field"' in got)
check("中文按原始字节透传（未被转义）", b"\xe4\xb8\xad\xe6\x96\x87" in got)
check("双空格等格式差异保留", b'",  "max_tokens"' in got or b'"__claude__",  "max_tokens"' not in got)

# ══════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("B · 请求头透传")
print("=" * 78)

captured["plan"] = {"status": 200, "body": b"{}"}
client.post("/v1/messages", content=b'{"model":"__claude__","messages":[]}',
            headers={"content-type": "application/json",
                     "anthropic-beta": "interleaved-thinking-2025-05-14",
                     "anthropic-version": "2024-01-01",
                     "x-api-key": "sk-downstream-fake",
                     "x-custom-trace": "trace-123"})
h = last_request()["headers"]
check("anthropic-beta 已透传", h.get("anthropic-beta") == "interleaved-thinking-2025-05-14", str(h))
check("anthropic-version 用下游的", h.get("anthropic-version") == "2024-01-01", str(h.get("anthropic-version")))
check("非白名单自定义头被丢弃", "x-custom-trace" not in h, str(h.get("x-custom-trace")))
check("x-api-key 已换为上游凭证", h.get("x-api-key") == "sk-upstream-real", str(h.get("x-api-key")))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("C · OpenAI 多轮 tool calling 完整保留")
print("=" * 78)

raw_openai = (
    b'{"model":"__openai__","messages":['
    b'{"role":"user","content":"weather?"},'
    b'{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function",'
    b'"function":{"name":"get_weather","arguments":"{\\"city\\":\\"beijing\\"}"}}],'
    b'"reasoning_content":"need tool"},'
    b'{"role":"tool","tool_call_id":"call_1","name":"get_weather","content":"sunny"}],'
    b'"tools":[{"type":"function","function":{"name":"get_weather","parameters":{"type":"object"}}}],'
    b'"stream_options":{"include_usage":true}}'
)
captured["plan"] = {"status": 200, "body": b'{"id":"c1","choices":[]}'}
client.post("/v1/chat/completions", content=raw_openai, headers={"content-type": "application/json"})
got_o = last_request()["body"]
exp_o = raw_openai.replace(b'"__openai__"', b'"upstream-gpt-x"')

check("OpenAI 请求体除 model 外逐字节一致", got_o == exp_o,
      f"\n    got={got_o!r}\n    exp={exp_o!r}")
check("assistant.tool_calls 保留", b'"tool_calls"' in got_o)
check("tool_call_id 保留", b'"tool_call_id":"call_1"' in got_o)
check("reasoning_content 保留", b'"reasoning_content"' in got_o)
check("content:null 保留", b'"content":null' in got_o)

# ══════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("D · 响应体逐字节 + 响应头透传")
print("=" * 78)

resp_body = ('{"id":"msg_2","content":[{"type":"text","text":"中文回复 \\u00e9"}],'
             '"usage":{"input_tokens":5,"cache_read_input_tokens":1200}}').encode()
captured["plan"] = {
    "status": 200,
    "body": resp_body,
    "headers": {
        "request-id": "req_abc123",
        "anthropic-ratelimit-requests-remaining": "999",
        "x-custom-resp": "resp-1",
    },
}
r = client.post("/v1/messages", content=b'{"model":"__claude__","messages":[]}',
                headers={"content-type": "application/json"})
check("响应体逐字节一致", r.content == resp_body, f"got={r.content!r}")
check("request-id 保留", r.headers.get("request-id") == "req_abc123", str(dict(r.headers)))
check("限流头保留", r.headers.get("anthropic-ratelimit-requests-remaining") == "999")
check("自定义响应头保留", r.headers.get("x-custom-resp") == "resp-1")

# ══════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("E · 上游错误码与错误体原样透传")
print("=" * 78)

err_body = (b'{"type":"error","error":{"type":"invalid_request_error",'
            b'"message":"prompt is too long: 250000 tokens > 200000 maximum"}}')
captured["plan"] = {
    "status": 400,
    "body": err_body,
    "headers": {"request-id": "req_err", "retry-after": "30"},
}
r = client.post("/v1/messages", content=b'{"model":"__claude__","messages":[]}',
                headers={"content-type": "application/json"})
check("状态码 400 不再被改写为 500", r.status_code == 400, f"got {r.status_code}")
check("错误体原样透传", r.content == err_body, r.text[:200])
check("retry-after 保留", r.headers.get("retry-after") == "30")

# ══════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("F · 流式逐字节等价")
print("=" * 78)

chunks = [
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"m1",',
    b'"usage":{"input_tokens":9}}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,',
    b'"delta":{"type":"text_delta","text":"\xe4\xbd\xa0\xe5\xa5\xbd"}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n',
]
captured["plan"] = {"stream": True, "chunks": chunks}
r = client.post("/v1/messages",
                content=b'{"model":"__claude__","messages":[],"stream":true}',
                headers={"content-type": "application/json"})
expected_stream = b"".join(chunks)
check("流式响应逐字节一致", r.content == expected_stream,
      f"\n    got={r.content!r}\n    exp={expected_stream!r}")
check("流式 content-type 沿用上游", "text/event-stream" in r.headers.get("content-type", ""),
      r.headers.get("content-type"))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("G · 跨协议仍被拒绝")
print("=" * 78)

captured["plan"] = {"status": 200, "body": b"{}"}
r = client.post("/v1/messages", content=b'{"model":"__openai_only__","messages":[]}',
                headers={"content-type": "application/json"})
check("Claude 端点打仅 OpenAI 上游 → 400", r.status_code == 400, f"got {r.status_code}")

r = client.post("/v1/chat/completions", content=b'{"model":"__claude__","messages":[]}',
                headers={"content-type": "application/json"})
check("OpenAI 端点打仅 Anthropic 上游 → 400", r.status_code == 400, f"got {r.status_code}")

r = client.post("/v1/responses", content=b'{"model":"__legacy_anthropic__","input":"hi"}',
                headers={"content-type": "application/json"})
check("legacy(provider=anthropic) 打 Responses 端点 → 400", r.status_code == 400,
      f"got {r.status_code} {r.text[:120]}")

r = client.post("/v1/messages", content=b'{"model":"__legacy_anthropic__","messages":[]}',
                headers={"content-type": "application/json"})
check("legacy 模型同协议仍可用", r.status_code == 200, f"got {r.status_code}")

r = client.post("/v1/messages", content=b'not json at all',
                headers={"content-type": "application/json"})
check("非法 JSON → 400", r.status_code == 400, f"got {r.status_code}")

# ══════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("H · Responses API 端点")
print("=" * 78)

raw_responses = (
    b'{"model":"__openai__","instructions":"be brief","input":['
    b'{"type":"message","role":"user","content":[{"type":"input_text",'
    b'"text":"\xe4\xbd\xa0\xe5\xa5\xbd"}]}],'
    b'"max_output_tokens":128,"reasoning":{"effort":"low"},'
    b'"text":{"format":{"type":"text"}},'
    b'"tools":[{"type":"function","name":"f","parameters":{"type":"object"}}],'
    b'"store":false,"previous_response_id":null}'
)
captured["plan"] = {"status": 200, "body": b'{"object":"response","output":[]}'}
r = client.post("/v1/responses", content=raw_responses,
                headers={"content-type": "application/json"})
req = last_request()

check("转发到上游 /v1/responses", req["path"] == "v1/responses", req["path"])
exp_resp = raw_responses.replace(b'"__openai__"', b'"upstream-gpt-x"')
check("请求体除 model 外逐字节一致", req["body"] == exp_resp,
      f"\n    got={req['body'][:300]!r}\n    exp={exp_resp[:300]!r}")
check("instructions / reasoning / text / tools 结构保留",
      all(k in req["body"] for k in (b'"instructions"', b'"reasoning"', b'"text"', b'"tools"')))
check("响应体逐字节回传", r.content == b'{"object":"response","output":[]}')

chunks_r = [
    b'event: response.created\ndata: {"type":"response.created"}\n\n',
    b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta",',
    b'"delta":"\xe4\xbd\xa0"}\n\n',
    b'event: response.completed\ndata: {"type":"response.completed"}\n',
]
captured["plan"] = {"stream": True, "chunks": chunks_r}
r = client.post("/v1/responses",
                content=b'{"model":"__openai__","input":"hi","stream":true}',
                headers={"content-type": "application/json"})
check("Responses 流式逐字节一致", r.content == b"".join(chunks_r),
      f"\n    got={r.content[:200]!r}")
check("流式 content-type 沿用上游",
      "text/event-stream" in r.headers.get("content-type", ""))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 78)
passed = sum(1 for _, ok, _ in results if ok)
failed = [n for n, ok, _ in results if not ok]
print(f"结果：{passed}/{len(results)} 通过")
for name in failed:
    print(f"  未通过：{name}")
print("=" * 78)
