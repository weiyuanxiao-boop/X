"""访问鉴权验证：本地放行、远程校验、key 不泄漏到上游、日志留痕。

用法：
    python verify_auth.py
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
from fastapi.responses import Response
from fastapi.testclient import TestClient

from app.config import get_model_config
from app.logger import logger as conversation_logger
from app.main import app as proxy_app

# 日志写到临时目录，避免污染真实 logs/
_LOG_TMP = Path(tempfile.mkdtemp(prefix="xlog_auth_"))
conversation_logger._root = _LOG_TMP
atexit.register(shutil.rmtree, _LOG_TMP, ignore_errors=True)

results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


# ── 假上游：记录收到的头，用于验证 key 不泄漏 ──────────────────

upstream_app = FastAPI()
captured = []


@upstream_app.post("/{path:path}")
async def catch_all(path: str, request: Request):
    captured.append({"path": path, "headers": dict(request.headers)})
    return Response(content=b'{"ok":true}', media_type="application/json")


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
os.environ["TEST_API_KEY"] = "sk-upstream-secret"

model_config = get_model_config()
model_config._models["__auth_claude__"] = {
    "upstream_model": "up-auth",
    "api_key_env": "TEST_API_KEY",
    "base_url": {"anthropic": f"http://127.0.0.1:{PORT}"},
    "api_version": "2023-06-01",
}
model_config._models["__auth_openai__"] = {
    "upstream_model": "up-auth",
    "api_key_env": "TEST_API_KEY",
    "base_url": {"openai": f"http://127.0.0.1:{PORT}"},
}

GOOD = "sk-good-1"
BODY = {"model": "__auth_claude__", "messages": [{"role": "user", "content": "hi"}]}
OPENAI_BODY = {"model": "__auth_openai__", "messages": [{"role": "user", "content": "hi"}]}


def new_client(host: str) -> TestClient:
    return TestClient(proxy_app, client=(host, 50000), raise_server_exceptions=False)


local = new_client("127.0.0.1")
local_other = new_client("127.5.5.5")
local_v6 = new_client("::1")
local_mapped = new_client("::ffff:127.0.0.1")
remote = new_client("203.0.113.9")

# ══════════════════════════════════════════════════════════════
print("=" * 74)
print("A · 开启鉴权（远程必须带 key，本地放行）")
print("=" * 74)
model_config._auth = {"enabled": True, "keys": {"team-a": GOOD, "ci": "sk-good-2"}}

r = local.post("/v1/messages", json=BODY)
check("127.0.0.1 无 key 放行", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

r = local_other.post("/v1/messages", json=BODY)
check("127.5.5.5 无 key 放行（整个 127/8）", r.status_code == 200, str(r.status_code))

r = local_v6.post("/v1/messages", json=BODY)
check("::1 无 key 放行", r.status_code == 200, str(r.status_code))

r = local_mapped.post("/v1/messages", json=BODY)
check("::ffff:127.0.0.1 无 key 放行", r.status_code == 200, str(r.status_code))

r = remote.post("/v1/messages", json=BODY)
check("远程无 key → 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
check("401 用的是 Claude 错误结构", (r.json().get("error") or {}).get("type") == "authentication_error",
      r.text[:160])

r = remote.post("/v1/messages", json=BODY, headers={"Authorization": "Bearer wrong-key"})
check("远程错 key → 401", r.status_code == 401, str(r.status_code))

r = remote.post("/v1/messages", json=BODY, headers={"Authorization": f"Bearer {GOOD}"})
check("远程 Bearer 正确 → 200", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

r = remote.post("/v1/messages", json=BODY, headers={"x-api-key": "sk-good-2"})
check("远程 x-api-key 正确 → 200", r.status_code == 200, str(r.status_code))

r = remote.post("/v1/messages", json=BODY, headers={"Authorization": GOOD})
check("远程裸 Authorization 值 → 200（兼容）", r.status_code == 200, str(r.status_code))

r = remote.post("/v1/chat/completions", json=OPENAI_BODY, headers={"Authorization": f"Bearer {GOOD}"})
check("OpenAI 端点同样生效 → 200", r.status_code == 200, str(r.status_code))

r = remote.post("/v1/chat/completions", json=OPENAI_BODY)
check("OpenAI 端点远程无 key → 401", r.status_code == 401, str(r.status_code))
check("401 用的是 OpenAI 错误结构", "error" in r.json() and isinstance(r.json()["error"], dict), r.text[:160])

# ══════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("B · 非对话端点")
print("=" * 74)
r = remote.get("/health")
check("/health 远程无 key → 200（探活放行）", r.status_code == 200, str(r.status_code))

r = remote.get("/v1/models")
check("/v1/models 远程无 key → 401", r.status_code == 401, str(r.status_code))

r = remote.get("/v1/models", headers={"Authorization": f"Bearer {GOOD}"})
check("/v1/models 远程带 key → 200", r.status_code == 200, str(r.status_code))

r = local.get("/v1/models")
check("/v1/models 本地无 key → 200", r.status_code == 200, str(r.status_code))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("C · 下游 key 不会泄漏到上游")
print("=" * 74)
captured.clear()
remote.post("/v1/messages", json=BODY, headers={"Authorization": f"Bearer {GOOD}"})
up_headers = captured[-1]["headers"] if captured else {}
check("上游 x-api-key 为上游凭证", up_headers.get("x-api-key") == "sk-upstream-secret",
      str(up_headers.get("x-api-key")))
check("上游 Authorization 被清除/覆盖（不含下游 key）",
      GOOD not in json.dumps(up_headers), str(up_headers))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("D · 关闭鉴权 → 全部放行")
print("=" * 74)
model_config._auth = {"enabled": False, "keys": {}}
r = remote.post("/v1/messages", json=BODY)
check("disabled 时远程无 key → 200", r.status_code == 200, str(r.status_code))

model_config._auth = {"enabled": False, "keys": {"team-a": GOOD}}
r = remote.post("/v1/messages", json=BODY)
check("显式 enabled=false 覆盖已有 keys → 200", r.status_code == 200, str(r.status_code))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("E · enabled=true 但 keys 为空 → fail closed")
print("=" * 74)
model_config._auth = {"enabled": True, "keys": {}}
r = remote.post("/v1/messages", json=BODY, headers={"Authorization": f"Bearer {GOOD}"})
check("无 key 可匹配时远程 401", r.status_code == 401, str(r.status_code))
r = local.post("/v1/messages", json=BODY)
check("本地仍放行", r.status_code == 200, str(r.status_code))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("F · 日志留痕")
print("=" * 74)
records = []
for path in _LOG_TMP.rglob("*.jsonl"):
    for line in open(path, encoding="utf-8"):
        if line.strip():
            records.append(json.loads(line))

modes = {}
for rec in records:
    mode = (rec.get("auth") or {}).get("mode", "<无>")
    modes[mode] = modes.get(mode, 0) + 1
print(f"  日志 {len(records)} 条，auth.mode 分布: {modes}")

check("401 有留痕", any(r["response"].get("status") == 401 for r in records))
check("被拒记录不落原始请求体",
      all("__withheld__" in (r["request"] or {})
          for r in records if r["response"].get("status") == 401))
principals = {r["auth"]["principal"] for r in records
              if (r.get("auth") or {}).get("mode") == "key"}
check("命中 key 时记录了 principal", "team-a" in principals or "ci" in principals, str(principals))
check("本地请求记录 mode=local",
      any((r.get("auth") or {}).get("mode") == "local" for r in records))

# ══════════════════════════════════════════════════════════════
print()
print("=" * 74)
passed = sum(1 for _, ok, _ in results if ok)
failed = [n for n, ok, _ in results if not ok]
print(f"结果：{passed}/{len(results)} 通过")
for n in failed:
    print(f"  未通过：{n}")
print("=" * 74)
