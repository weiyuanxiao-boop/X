"""对话日志：JSON Lines，按日期目录 + 客户端/模型分文件，追加写入。

设计要点
--------
1. **不做「读全文件 → 修改 → 写回」**。每次日志是一行独立 JSON，直接以
   ``O_APPEND`` 打开文件并用**单次** ``os.write`` 追加，因此：
   - 并发请求不会互相覆盖（内核在 O_APPEND 下把整个 write 当作一次原子操作）；
   - 进程中途被杀最多丢失最后一行，不会把已有记录写坏。
2. **文件不再是合法 JSON 文档**，而是 JSONL：一行一个完整对话对象，
   可以用 ``grep``、``jq -c``、``wc -l`` 直接处理，单行损坏不影响其余行。
3. 一条对话只在响应结束时写一行，``request`` 与 ``response`` 在同一条记录内，
   不需要跨记录按 id 关联。
4. 路径：``{LOG_DIR}/{YYYY-MM-DD}/conversations_{client}_{下游模型}_{上游模型}.jsonl``
"""

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .config import get_logger, get_settings

_UNSAFE = re.compile(r"[^\w\-]")
_MAX_PART = 48          # 单个名字片段上限，避免 Windows 路径过长


# ── 跨进程写锁 ────────────────────────────────────────────────
# 仅靠 O_APPEND 在 Windows 上不足以保证跨进程原子追加（CRT 的 _O_APPEND 是
# 「seek 到末尾再写」，两个进程之间存在竞态，实测 4 进程并发会丢记录）。
# 因此写入前对文件首字节加独占锁：POSIX 用 flock，Windows 用 msvcrt。

if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.002)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _safe_name(value) -> str:
    text = _UNSAFE.sub("_", str(value or "")).strip("_")
    return (text or "unknown")[:_MAX_PART]


class ConversationLogger:
    def __init__(self):
        self._root = get_settings().log_dir_path
        self._root.mkdir(parents=True, exist_ok=True)
        # 进程内串行化；跨进程依赖 O_APPEND + 单次 write 的原子性
        self._lock = threading.Lock()
        self._logger = get_logger("conversation")

    def path_for(
        self,
        client_id: str,
        downstream_model: str,
        upstream_model: str,
        when: datetime | None = None,
    ) -> Path:
        """计算一条日志应写入的文件路径。"""
        stamp = when or datetime.now().astimezone()
        filename = (
            f"conversations_{_safe_name(client_id)}_"
            f"{_safe_name(downstream_model)}_{_safe_name(upstream_model)}.jsonl"
        )
        return self._root / stamp.strftime("%Y-%m-%d") / filename

    def log_exchange(
        self,
        *,
        client_id: str,
        downstream_model: str,
        upstream_model: str,
        request,
        response,
        auth: dict | None = None,
        when: datetime | None = None,
    ) -> str:
        """追加一条完整对话记录，返回会话 ID。

        Args:
            request:  下游原始请求（解析后的完整对象，未裁剪）
            response: 上游返回摘要，含 status / headers / body 或 events / error
            auth:     鉴权结果摘要 {"mode": ..., "principal": ...}，不记录 key 明文
        """
        stamp = when or datetime.now().astimezone()
        conv_id = str(uuid.uuid4())
        entry = {
            "id": conv_id,
            "timestamp": stamp.isoformat(timespec="milliseconds"),
            "client_id": client_id,
            "downstream_model": downstream_model,
            "upstream_model": upstream_model,
            "request": request,
            "response": response,
        }
        if auth:
            entry["auth"] = auth

        # 单行 JSON，确保写入落为一次 write 系统调用
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        data = line.encode("utf-8")

        path = self.path_for(client_id, downstream_model, upstream_model, stamp)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                _lock(fd)
                try:
                    os.write(fd, data)
                finally:
                    _unlock(fd)
            finally:
                os.close(fd)

        self._logger.debug(f"Logged exchange {conv_id} -> {path.name}")
        return conv_id


logger = ConversationLogger()
