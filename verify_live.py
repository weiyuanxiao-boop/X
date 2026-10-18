"""线上部署验证。

用法：
    python verify_live.py https://x.example.cn <api-key>
    # 或
    LIVE_BASE=https://x.example.cn LIVE_KEY=xxx python verify_live.py

只做只读调用，不会修改服务端配置。密钥不写入任何文件。
"""

import os
import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LIVE_BASE", "")).rstrip("/")
KEY = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("LIVE_KEY", "")

if not BASE or not KEY:
    print("用法: python verify_live.py <base_url> <api-key>")
    raise SystemExit(2)

results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail else ""))


# 走系统代理出网（本机 DNS 无法直连该域名）
client = httpx.Client(timeout=180.0, follow_redirects=False, trust_env=True)
AUTH = {"Authorization": f"Bearer {KEY}"}

print("=" * 74)
print(f"目标: {BASE}")
print("=" * 74)

# ── 1. 传输层 ─────────────────────────────────────────────────
print("\n1 · HTTPS 与重定向")
try:
    r = client.get(f"{BASE}/health")
    check("HTTPS /health 返回 200", r.status_code == 200, r.text[:80])
    hsts = r.headers.get("strict-transport-security")
    check("HSTS 头存在", bool(hsts), hsts or "缺失")
    r = client.head(f"{BASE}/health")
    check("HEAD /health 可用（探活兼容）", r.status_code == 200, str(r.status_code))
except Exception as exc:
    check("HTTPS /health", False, f"{type(exc).__name__}: {exc}")

try:
    http_base = BASE.replace("https://", "http://")
    r = client.get(f"{http_base}/health")
    check("http 跳转 https", r.status_code in (301, 302, 307, 308), f"{r.status_code}")
except Exception as exc:
    check("http 跳转", False, f"{type(exc).__name__}: {exc}")

# ── 2. 鉴权 ───────────────────────────────────────────────────
print("\n2 · 鉴权")
r = client.get(f"{BASE}/v1/models")
check("无 key → 401", r.status_code == 401, f"{r.status_code}")
check("401 为 JSON 错误结构", "error" in r.text[:200], r.text[:100])

r = client.get(f"{BASE}/v1/models", headers={"Authorization": "Bearer wrong-key"})
check("错误 key → 401", r.status_code == 401, f"{r.status_code}")

r = client.get(f"{BASE}/v1/models", headers={"x-api-key": KEY})
check("x-api-key 方式可用", r.status_code == 200, f"{r.status_code}")

r = client.get(f"{BASE}/v1/models", headers=AUTH)
check("Bearer 方式可用", r.status_code == 200, f"{r.status_code}")

models = []
if r.status_code == 200:
    models = r.json().get("models", [])
    print(f"      可用模型 {len(models)} 个")

def pick(fmt: str, prefer: str) -> str:
    for name in (prefer,):
        if any(m["id"] == name and fmt in m.get("supported_formats", []) for m in models):
            return name
    for m in models:
        if fmt in m.get("supported_formats", []) and not m.get("is_alias"):
            return m["id"]
    for m in models:
        if fmt in m.get("supported_formats", []):
            return m["id"]
    return ""

CHAT_MODEL = pick("openai", "deepseek-v4-flash-openai")
CLAUDE_MODEL = pick("anthropic", "deepseek-v4-flash-anthropic")
print(f"      openai 模型: {CHAT_MODEL or '无'}")
print(f"      anthropic 模型: {CLAUDE_MODEL or '无'}")

# ── 3. Chat Completions ───────────────────────────────────────
print("\n3 · /v1/chat/completions")
if CHAT_MODEL:
    r = client.post(f"{BASE}/v1/chat/completions", headers=AUTH, json={
        "model": CHAT_MODEL,
        "messages": [{"role": "user", "content": "用一句话说明什么是代理"}],
        # reasoning 模型的思考链也计入 max_tokens，预算太小会导致 content 为空
        "max_tokens": 400,
    })
    ok = r.status_code == 200
    body = r.json() if ok else {}
    choice = (body.get("choices") or [{}])[0]
    text = choice.get("message", {}).get("content", "") or ""
    usage = body.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    check("非流式 200 且有内容", ok and bool(text),
          f"{r.status_code} text={text[:30]!r} finish={choice.get('finish_reason')} reasoning={reasoning}")

    r = client.post(f"{BASE}/v1/chat/completions", headers=AUTH, json={
        "model": CHAT_MODEL,
        "messages": [{"role": "user", "content": "从 1 数到 20，每行一个数字，不要任何解释"}],
        "max_tokens": 200,
        "stream": True,
    })
    check("流式 200", r.status_code == 200, f"{r.status_code}")
    if r.status_code == 200:
        check("流式含 data: 行", r.text.count("data:") >= 2, f"{r.text.count('data:')} 行")
else:
    check("跳过 Chat（无可用模型）", False)

# ── 4. Claude Messages ────────────────────────────────────────
print("\n4 · /v1/messages")
if CLAUDE_MODEL:
    r = client.post(f"{BASE}/v1/messages", headers=AUTH, json={
        "model": CLAUDE_MODEL,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": "用一句话说明什么是代理"}],
    })
    ok = r.status_code == 200
    body = r.json() if ok else {}
    blocks = body.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    check("非流式 200 且有内容", ok and bool(text), f"{r.status_code} {text[:40]!r}")
    check("返回标准 Claude 结构", body.get("type") == "message" or "content" in body,
          str(list(body)[:5]))

    r = client.post(f"{BASE}/v1/messages", headers=AUTH, json={
        "model": CLAUDE_MODEL,
        "max_tokens": 200,
        "stream": True,
        "messages": [{"role": "user", "content": "从 1 数到 20，每行一个数字，不要任何解释"}],
    })
    check("流式 200", r.status_code == 200, f"{r.status_code}")
    if r.status_code == 200:
        check("流式含 event/data", "event:" in r.text and "data:" in r.text,
              f"{r.text.count('data:')} 个 data 行")
else:
    check("跳过 Claude（无可用模型）", False)

# ── 5. Responses ──────────────────────────────────────────────
print("\n5 · /v1/responses")
if CHAT_MODEL:
    r = client.post(f"{BASE}/v1/responses", headers=AUTH, json={
        "model": CHAT_MODEL,
        "input": "用一句话说明什么是代理",
        "max_output_tokens": 100,
    })
    ok = r.status_code == 200
    body = r.json() if ok else {}
    check("非流式 200", ok, f"{r.status_code} {r.text[:100]}")
    if ok:
        check("object=response", body.get("object") == "response", str(body.get("object")))
        check("output 为列表", isinstance(body.get("output"), list))

    r = client.post(f"{BASE}/v1/responses", headers=AUTH, json={
        "model": CHAT_MODEL,
        "input": "从 1 数到 20，每行一个数字",
        "max_output_tokens": 200,
        "stream": True,
    })
    check("流式 200", r.status_code == 200, f"{r.status_code}")
    if r.status_code == 200:
        check("流式为 response.* 事件", "response.created" in r.text or "response.output" in r.text,
              r.text[:60])
else:
    check("跳过 Responses（无可用模型）", False)

# ── 6. 跨协议拒绝 ─────────────────────────────────────────────
print("\n6 · 跨协议拒绝")
only_openai = next((m["id"] for m in models
                    if m.get("supported_formats") == ["openai"]), "")
only_claude = next((m["id"] for m in models
                    if m.get("supported_formats") == ["anthropic"]), "")
if only_openai:
    r = client.post(f"{BASE}/v1/messages", headers=AUTH,
                    json={"model": only_openai, "max_tokens": 16,
                          "messages": [{"role": "user", "content": "hi"}]})
    check(f"仅 openai 的 {only_openai} 打 /v1/messages → 400", r.status_code == 400,
          f"{r.status_code} {r.text[:90]}")
else:
    check("（无单格式模型，跳过）", True)
if only_claude:
    r = client.post(f"{BASE}/v1/responses", headers=AUTH,
                    json={"model": only_claude, "input": "hi"})
    check(f"仅 anthropic 的 {only_claude} 打 /v1/responses → 400", r.status_code == 400,
          f"{r.status_code} {r.text[:90]}")
else:
    check("（无单格式模型，跳过）", True)

# ── 7. 响应头过滤 ─────────────────────────────────────────────
print("\n7 · 响应头边界")
if CHAT_MODEL:
    r = client.post(f"{BASE}/v1/chat/completions", headers=AUTH, json={
        "model": CHAT_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8,
    })
    lowered = {k.lower() for k in r.headers}
    check("无 set-cookie", "set-cookie" not in lowered)
    check("无 access-control-*", not any(k.startswith("access-control-") for k in lowered))
    check("无 server 头泄露上游", "server" not in lowered or r.headers.get("server") == "nginx",
          str(r.headers.get("server")))

# ── 8. 流式缓冲探测 ───────────────────────────────────────────
print("\n8 · 流式是否被 nginx 缓冲（首字节 vs 总耗时）")
if CHAT_MODEL:
    t0 = time.monotonic()
    first = None
    chunks = 0
    try:
        with client.stream("POST", f"{BASE}/v1/chat/completions", headers=AUTH, json={
            "model": CHAT_MODEL,
            "messages": [{"role": "user",
                          "content": "从 1 数到 50，每行一个数字，不要任何解释"}],
            "max_tokens": 400,
            "stream": True,
        }) as resp:
            for chunk in resp.iter_bytes():
                if first is None:
                    first = time.monotonic() - t0
                chunks += 1
        total = time.monotonic() - t0
        ratio = (first / total) if total > 0 else 1.0
        print(f"      首字节 {first:.2f}s / 总耗时 {total:.2f}s / {chunks} 个 chunk")
        check("首字节明显早于总耗时（未被缓冲）",
              first is not None and ratio < 0.5 and chunks > 2,
              f"首字节占比 {ratio:.0%}")
    except Exception as exc:
        check("流式缓冲探测", False, f"{type(exc).__name__}: {exc}")

# ── 汇总 ─────────────────────────────────────────────────────
print("\n" + "=" * 74)
passed = sum(1 for _, ok, _ in results if ok)
failed = [n for n, ok, _ in results if not ok]
print(f"结果：{passed}/{len(results)} 通过")
for name in failed:
    print(f"  未通过：{name}")
print("=" * 74)

client.close()
