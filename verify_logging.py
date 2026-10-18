"""日志写入验证：并发安全、目录结构、JSONL 完整性。

用法：
    python verify_logging.py
"""

import json
import multiprocessing as mp
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.logger import logger

results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def read_lines(path: Path):
    """按行读取 JSONL，返回 (记录列表, 坏行数)。"""
    good, bad = [], 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                good.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return good, bad


def mp_worker(args):
    root, count, tag = args
    from app.logger import logger as mp_logger
    mp_logger._root = Path(root)
    for i in range(count):
        mp_logger.log_exchange(
            client_id="10.0.0.9",
            downstream_model="mp-model",
            upstream_model="mp-upstream",
            request={"n": i, "tag": tag},
            response={"status": 200},
        )
    return count


def main():
    tmp = Path(tempfile.mkdtemp(prefix="xlog_verify_"))
    logger._root = tmp
    print(f"临时日志目录：{tmp}\n")

    # ── A. 单线程顺序写，验证目录与文件名格式 ──────────────────
    print("=" * 74)
    print("A · 目录结构与文件名")
    print("=" * 74)
    logger.log_exchange(
        client_id="192.168.31.20",
        downstream_model="cc-coder",
        upstream_model="deepseek-v4-flash",
        request={"model": "cc-coder", "messages": []},
        response={"status": 200, "headers": {"content-type": "application/json"},
                  "body": {"content": [{"type": "text", "text": "ok"}]}},
    )
    files = list(tmp.rglob("*.jsonl"))
    print(f"  生成文件: {[str(f.relative_to(tmp)) for f in files]}")
    check("按日期建子目录", len(files) == 1 and files[0].parent.name.count("-") == 2,
          str(files[0].parent.name if files else "无"))
    check("文件名含客户端/下游/上游模型",
          len(files) == 1 and "192_168_31_20" in files[0].name
          and "cc-coder" in files[0].name and "deepseek-v4-flash" in files[0].name,
          files[0].name if files else "")

    if files:
        recs, bad = read_lines(files[0])
        check("单行是完整记录（含 request 与 response）",
              len(recs) == 1 and "request" in recs[0] and "response" in recs[0])
        check("JSONL 每行可独立解析", bad == 0, f"坏行 {bad}")

    # ── B. 多线程并发 500 条 ────────────────────────────────────
    print()
    print("=" * 74)
    print("B · 多线程并发 500 条")
    print("=" * 74)

    def thread_worker(i):
        logger.log_exchange(
            client_id="10.0.0.7",
            downstream_model="mt-model",
            upstream_model="mt-upstream",
            request={"n": i},
            response={"status": 200},
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(thread_worker, range(500)))

    target = logger.path_for("10.0.0.7", "mt-model", "mt-upstream")
    recs, bad = read_lines(target)
    check("500 条全部落盘且无丢失", len(recs) == 500, f"实际 {len(recs)}")
    check("无损坏行", bad == 0, f"坏行 {bad}")
    check("序号完整无重复", sorted(r["request"]["n"] for r in recs) == list(range(500)))

    # ── C. 多进程并发 4 × 150 条（模拟 uvicorn --workers）──────
    print()
    print("=" * 74)
    print("C · 多进程并发 4 × 150 条")
    print("=" * 74)

    mp_root = tmp / "mp"
    ctx = mp.get_context("spawn")
    with ctx.Pool(4) as pool:
        pool.map(mp_worker, [(str(mp_root), 150, f"p{i}") for i in range(4)])

    mp_files = list(mp_root.rglob("*.jsonl"))
    total, bad_total = 0, 0
    for f in mp_files:
        r, b = read_lines(f)
        total += len(r)
        bad_total += b
    check("4 进程共 600 条全部落盘", total == 600, f"实际 {total}")
    check("跨进程写入无损坏行", bad_total == 0, f"坏行 {bad_total}")

    # ── D. 端到端：不同模型/客户端分文件 ────────────────────────
    print()
    print("=" * 74)
    print("D · 端到端按客户端 + 模型分文件")
    print("=" * 74)

    import app.proxy as proxy
    from app.main import app as proxy_app

    e2e_root = tmp / "e2e"
    logger._root = e2e_root

    class FakeResp:
        status_code = 200
        headers = httpx.Headers({"content-type": "application/json"})

        async def aread(self):
            return b'{"id":"x","content":[{"type":"text","text":"ok"}]}'

        async def aclose(self):
            pass

        async def aiter_bytes(self):
            yield b""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def build_request(self, method, url, headers=None, content=None):
            return object()

        async def send(self, request, stream=True):
            return FakeResp()

        async def aclose(self):
            pass

    proxy.httpx.AsyncClient = FakeClient
    client = TestClient(proxy_app)

    for _ in range(10):
        client.post("/v1/chat/completions",
                    json={"model": "deepseek-v4-flash-openai", "messages": [{"role": "user", "content": "hi"}]})
    for _ in range(5):
        client.post("/v1/messages",
                    json={"model": "deepseek-v4-flash-anthropic", "messages": [{"role": "user", "content": "hi"}]})
    # 跨协议拒绝也应留痕
    client.post("/v1/messages",
                json={"model": "deepseek-v4-flash-openai", "messages": [{"role": "user", "content": "hi"}]})

    e2e_files = sorted(e2e_root.rglob("*.jsonl"))
    print(f"  生成 {len(e2e_files)} 个文件：")
    for f in e2e_files:
        r, b = read_lines(f)
        print(f"    {f.name}  -> {len(r)} 条")

    check("不同下游模型写到不同文件", len(e2e_files) == 3, f"实际 {len(e2e_files)}")
    all_recs = []
    for f in e2e_files:
        r, _ = read_lines(f)
        all_recs.extend(r)
    check("总记录数 = 16（10 + 5 + 1 条拒绝）", len(all_recs) == 16, f"实际 {len(all_recs)}")
    check("拒绝记录带 status=400 与 error",
          any(r["response"].get("status") == 400 and r["response"].get("error")
              for r in all_recs))

    # ── 汇总 ───────────────────────────────────────────────────
    print()
    print("=" * 74)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [n for n, ok, _ in results if not ok]
    print(f"结果：{passed}/{len(results)} 通过")
    for n in failed:
        print(f"  未通过：{n}")
    print("=" * 74)

    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
