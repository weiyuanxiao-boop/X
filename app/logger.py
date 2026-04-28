import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import get_settings, get_logger


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name)


class ConversationLogger:
    def __init__(self):
        settings = get_settings()
        self._log_dir = settings.log_dir_path
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._logger = get_logger("conversation")

    def _today_file(self, downstream_model: str, upstream_model: str) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        name = f"conversations_{_safe_name(downstream_model)}_{_safe_name(upstream_model)}_{today}.json"
        return self._log_dir / name

    def _read_log(self, path: Path) -> list:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                self._logger.warning(f"Log file corrupted, starting fresh: {path}")
                return []
        return []

    def _write_log(self, path: Path, data: list):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def log_request(
        self, downstream_model: str, upstream_model: str,
        messages: list[dict], client_id: str = "",
        extra_params: dict | None = None,
    ) -> str:
        conv_id = str(uuid.uuid4())
        entry = {
            "id": conv_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client_id": client_id,
            "downstream_model": downstream_model,
            "upstream_model": upstream_model,
            "messages": messages,
            "extra_params": extra_params or {},
            "response": None,
        }
        path = self._today_file(downstream_model, upstream_model)
        logs = self._read_log(path)
        logs.append(entry)
        self._write_log(path, logs)
        self._logger.debug(f"Logged request {conv_id} for {downstream_model} -> {upstream_model}")
        return conv_id

    def log_response(self, downstream_model: str, upstream_model: str, conv_id: str, response_text: str, usage: dict, finish_reason: str):
        path = self._today_file(downstream_model, upstream_model)
        logs = self._read_log(path)
        for entry in logs:
            if entry["id"] == conv_id:
                entry["response"] = {
                    "text": response_text,
                    "usage": usage,
                    "finish_reason": finish_reason,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                break
        self._write_log(path, logs)
        self._logger.debug(f"Logged response for {conv_id}, finish_reason: {finish_reason}")


logger = ConversationLogger()
