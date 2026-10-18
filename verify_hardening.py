"""QA 整改验收：逐条验证安全加固项。

用法：
    python verify_hardening.py
"""

import atexit
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

from app.config import CONFIG_DIR, ModelConfig, get_model_config
from app.logger import logger as conversation_logger
from app.main import app as proxy_app

_LOG_TMP = Path(tempfile.mkdtemp(prefix="xlog_hard_"))
conversation_logger._root = _LOG_TMP
atexit.register(shutil.rmtree, _LOG_TMP, ignore_errors=True)

results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


# ── 假上游 ────────────────────────────────────────────────────
upstream_app = FastAPI()
captured = []
plan = {"big_bytes": 0}


@upstream_app.post("/{path:path}")
async def catch_all(path: str, request: Request):
    captured.append({"headers": dict(request.headers), "body": await request.body()})
    if plan["big_bytes"]:
        body = b'{"data":"' + b"x" * plan["big_bytes"] + b'"}'
    else:
        body = b'{"ok":true}'
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Set-Cookie": "leak=1; Path=/",
            "Access-Control-Allow-Origin": "*",
            "X-Internal-Host": "10.0.0.5",
            "X-Request-Id": "req-abc",
            "Anthropic-Ratelimit-Requests-Remaining": "999",
        },
    )


def start_upstream() -> int:
    cfg = uvicorn.Config(upstream_app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    return server.servers[0].sockets[0].getsockname()[1]


PORT = start_upstream()
os.environ["TEST_API_KEY"] = "sk-upstream-secret"

mc = get_model_config()
mc._models["__hard__"] = {
    "upstream_model": "up-hard",
    "api_key_env": "TEST_API_KEY",
    "base_url": {"anthropic": f"http://127.0.0.1:{PORT}"},
    "api_version": "2023-06-01",
}
BODY = {"model": "__hard__", "messages": [{"role": "user", "content": "hi"}]}

local = TestClient(proxy_app, client=("127.0.0.1", 50000), raise_server_exceptions=False)
remote = TestClient(proxy_app, client=("203.0.113.9", 50000), raise_server_exceptions=False)

# ══════════════════════════════════════════════════════════════
print("=" * 76)
print("1 · auth 默认启用")
print("=" * 76)
default_auth = ModelConfig._parse_auth(None)
check("缺省无 auth 段时 enabled=True", default_auth["enabled"] is True, str(default_auth))
check("显式 enabled=false 可关闭",
      ModelConfig._parse_auth({"enabled": False})["enabled"] is False)

mc._auth = {"enabled": True, "keys": {}}
r = remote.post("/v1/messages", json=BODY)
check("keys 为空时远程 401（fail closed）", r.status_code == 401, str(r.status_code))
r = local.post("/v1/messages", json=BODY)
check("本地仍放行", r.status_code == 200, str(r.status_code))

mc._auth = {"enabled": True, "keys": {"k": "sk-good"}}
r = remote.post("/v1/messages", json=BODY)
check("远程无 key 401", r.status_code == 401)
r = remote.post("/v1/messages", json=BODY, headers={"Authorization": "Bearer sk-good"})
check("远程带正确 key 200", r.status_code == 200, str(r.status_code))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("2 · 请求头白名单")
print("=" * 76)
captured.clear()
local.post("/v1/messages", json=BODY, headers={
    "Cookie": "session=secret",
    "X-Forwarded-For": "1.2.3.4",
    "X-Real-IP": "1.2.3.4",
    "Referer": "https://internal.example/",
    "Origin": "https://internal.example",
    "X-Internal-Auth": "token",
    "Anthropic-Beta": "interleaved-thinking-2025-05-14",
    "Accept": "application/json",
})
up = captured[-1]["headers"]
lower = {k.lower() for k in up}
blocked = {"cookie", "x-forwarded-for", "x-real-ip", "referer", "origin", "x-internal-auth"}
leaked = blocked & lower
check("Cookie / XFF / Referer / 自定义头均未到达上游", not leaked, str(leaked))
check("anthropic-beta 仍然透传", up.get("anthropic-beta") == "interleaved-thinking-2025-05-14",
      str(up.get("anthropic-beta")))
check("accept 仍然透传", "accept" in lower)
check("上游凭证已替换", up.get("x-api-key") == "sk-upstream-secret", str(up.get("x-api-key")))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("3 · 响应头过滤")
print("=" * 76)
r = local.post("/v1/messages", json=BODY)
down = {k.lower() for k in r.headers}
check("Set-Cookie 未回传", "set-cookie" not in down)
check("access-control-* 未回传", not any(k.startswith("access-control-") for k in down))
check("x-request-id 仍保留", r.headers.get("x-request-id") == "req-abc",
      str(r.headers.get("x-request-id")))
check("限流头仍保留",
      r.headers.get("anthropic-ratelimit-requests-remaining") == "999")

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("4 · alias 注入防护")
print("=" * 76)
aliases_path = CONFIG_DIR / "aliases.yaml"
backup = aliases_path.read_bytes() if aliases_path.exists() else None
try:
    payload = urllib.parse.quote("evil\ninjected_alias")
    r = local.post(f"/v1/aliases?alias={payload}&target=deepseek-v4-flash")
    check("含换行的 alias 被拒绝", r.status_code == 400, f"{r.status_code} {r.text[:90]}")

    r = local.post("/v1/aliases", params={"alias": "ok-alias_1", "target": "deepseek-v4-flash"})
    check("合法 alias 正常写入", r.status_code == 200, str(r.status_code))
    parsed = yaml.safe_load(aliases_path.read_text(encoding="utf-8")) or {}
    check("写入后文件仍是合法 YAML 且无注入键",
          "injected_alias" not in parsed and parsed.get("ok-alias_1") == "deepseek-v4-flash",
          str(list(parsed)[-3:]))
finally:
    if backup is not None:
        aliases_path.write_bytes(backup)
        print("  已恢复 aliases.yaml")

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("5 · 请求体大小上限")
print("=" * 76)
mc._limits = {"max_body_mb": 1, "rate_limit_per_minute": 0, "max_logged_body_mb": 8}
big = {"model": "__hard__", "messages": [{"role": "user", "content": "A" * (2 * 1024 * 1024)}]}
r = local.post("/v1/messages", json=big)
check("2MB 请求体在 1MB 上限下返回 413", r.status_code == 413, str(r.status_code))

r = local.post("/v1/messages", json=BODY)
check("正常请求不受影响", r.status_code == 200, str(r.status_code))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("6 · 速率限制")
print("=" * 76)
mc._limits = {"max_body_mb": 32, "rate_limit_per_minute": 3, "max_logged_body_mb": 8}
rl_client = TestClient(proxy_app, client=("198.51.100.77", 50000), raise_server_exceptions=False)
mc._auth = {"enabled": False, "keys": {}}
codes = [rl_client.post("/v1/messages", json=BODY).status_code for _ in range(5)]
check("限流 3/min：前 3 次通过、后 2 次 429",
      codes == [200, 200, 200, 429, 429], str(codes))
r = rl_client.post("/v1/messages", json=BODY)
check("429 带 Retry-After", r.headers.get("retry-after") == "60", str(r.headers.get("retry-after")))

mc._limits = {"max_body_mb": 32, "rate_limit_per_minute": 0, "max_logged_body_mb": 8}

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("7 · 错误信息脱敏")
print("=" * 76)
mc._models["__dead2__"] = {
    "upstream_model": "up-dead2",
    "api_key_env": "TEST_API_KEY",
    "base_url": {"anthropic": "http://127.0.0.1:9"},
    "api_version": "2023-06-01",
}
r = local.post("/v1/messages", json={"model": "__dead2__", "messages": [{"role": "user", "content": "hi"}]})
check("502 不回显底层异常", r.status_code == 502 and "os error" not in r.text.lower()
      and "127.0.0.1:9" not in r.text, r.text[:120])

r = local.post("/v1/messages", content=b"{not json")
check("非法 JSON 不回显解析细节",
      r.status_code == 400 and "Expecting" not in r.text and "line 1" not in r.text, r.text[:120])

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("8 · 日志超大请求截断")
print("=" * 76)
mc._limits = {"max_body_mb": 32, "rate_limit_per_minute": 0, "max_logged_body_mb": 1}
mid = {"model": "__hard__", "messages": [{"role": "user", "content": "B" * (1500 * 1024)}]}
local.post("/v1/messages", json=mid)

records = []
for p in _LOG_TMP.rglob("*.jsonl"):
    for line in open(p, encoding="utf-8"):
        if line.strip():
            records.append(json.loads(line))
truncated = [r for r in records if isinstance(r.get("request"), dict) and r["request"].get("__truncated__")]
check("超过 1MB 的请求只记元信息", bool(truncated),
      f"记录数 {len(records)}")
if truncated:
    check("截断记录保留了字节数", truncated[-1]["request"].get("bytes", 0) > 1024 * 1024,
          str(truncated[-1]["request"]))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("9 · 上游代理隔离")
print("=" * 76)
cfg = ModelConfig._parse_upstream(None)
check("缺省 trust_env=False", cfg["trust_env"] is False, str(cfg))
check("缺省不指定 proxy", cfg["proxy"] is None)
check("显式 proxy 可配置",
      ModelConfig._parse_upstream({"proxy": "http://127.0.0.1:7890"})["proxy"] == "http://127.0.0.1:7890")

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("10 · 鉴权失败同样受限流约束（QA 复核补充）")
print("=" * 76)
mc._auth = {"enabled": True, "keys": {"k": "sk-good"}}
mc._limits = {"max_body_mb": 32, "rate_limit_per_minute": 3, "max_logged_body_mb": 8}
brute = TestClient(proxy_app, client=("203.0.113.60", 50000), raise_server_exceptions=False)
codes = [brute.post("/v1/messages", json=BODY,
                    headers={"Authorization": f"Bearer bad{i}"}).status_code
         for i in range(10)]
check("连续错误 key 会触发 429", 429 in codes, str(codes))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("11 · 响应体日志截断（QA 复核补充）")
print("=" * 76)
mc._auth = {"enabled": False, "keys": {}}
mc._limits = {"max_body_mb": 32, "rate_limit_per_minute": 0, "max_logged_body_mb": 1}
plan["big_bytes"] = 3 * 1024 * 1024

size_before = sum(p.stat().st_size for p in _LOG_TMP.rglob("*.jsonl"))
local.post("/v1/messages", json=BODY)
size_after = sum(p.stat().st_size for p in _LOG_TMP.rglob("*.jsonl"))
delta = size_after - size_before
check("3MB 响应不会被完整写入日志", delta < 1.5 * 1024 * 1024, f"日志增加 {delta / 1024:.0f} KB")
plan["big_bytes"] = 0

records2 = []
for p in _LOG_TMP.rglob("*.jsonl"):
    for line in open(p, encoding="utf-8"):
        if line.strip():
            records2.append(json.loads(line))
trunc_resp = [r for r in records2
              if isinstance(r.get("response"), dict)
              and isinstance(r["response"].get("body"), dict)
              and r["response"]["body"].get("__truncated__")]
check("响应截断留有标记", bool(trunc_resp))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("12 · 空 anthropic-version 不覆盖配置（QA 复核补充）")
print("=" * 76)
captured.clear()
local.post("/v1/messages", json=BODY, headers={"anthropic-version": ""})
got = captured[-1]["headers"].get("anthropic-version")
check("空值回落到配置的 2023-06-01", got == "2023-06-01", repr(got))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("13 · x-internal-* 不回传（QA 复核补充）")
print("=" * 76)
r = local.post("/v1/messages", json=BODY)
check("X-Internal-Host 已过滤", r.headers.get("x-internal-host") is None,
      str(r.headers.get("x-internal-host")))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 76)
passed = sum(1 for _, ok, _ in results if ok)
failed = [n for n, ok, _ in results if not ok]
print(f"结果：{passed}/{len(results)} 通过")
for n in failed:
    print(f"  未通过：{n}")
print("=" * 76)
