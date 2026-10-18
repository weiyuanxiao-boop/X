"""访问鉴权：本地连接放行，非本地连接必须携带配置中的 key。

放行规则
--------
- 未启用（``auth.enabled`` 为 false，或未配置任何 key）→ 全部放行
- 来源是回环地址（``localhost`` / ``127.0.0.0/8`` / ``::1`` / IPv4-mapped IPv6）→ 放行
- 其他来源 → 必须携带正确 key

key 的三种携带方式（按顺序取第一个命中的，兼容 OpenAI 与 Claude SDK）::

    Authorization: Bearer <key>
    Authorization: <key>
    x-api-key: <key>

下游用于鉴权的这两个头不会被转发给上游 —— 转发层会用上游凭证覆盖它们。
"""

import ipaddress
import secrets
from dataclasses import dataclass

from fastapi import Request

from .config import get_model_config

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


@dataclass(frozen=True)
class AuthResult:
    allowed: bool
    mode: str            # disabled | local | key | denied
    principal: str = ""  # 命中的 key 名字（仅 mode="key" 时有值）
    reason: str = ""


def is_loopback(host: str | None) -> bool:
    """判断来源是否为回环地址。"""
    if not host:
        return False
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:          # ::ffff:127.0.0.1
        ip = mapped
    return ip.is_loopback


def extract_token(headers) -> str | None:
    """从请求头取下游提交的 key。"""
    raw = headers.get("authorization")
    if raw:
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip() or None
        return raw.strip() or None

    raw = headers.get("x-api-key")
    if raw:
        return raw.strip() or None
    return None


def authenticate(request: Request) -> AuthResult:
    """对一次请求做鉴权判定，不抛异常。"""
    client_host = request.client.host if request.client else None
    config = get_model_config().get_auth_config()

    if not config.get("enabled"):
        return AuthResult(True, "disabled", reason="auth disabled")

    if is_loopback(client_host):
        return AuthResult(True, "local", reason="loopback bypass")

    keys = config.get("keys") or {}
    if not keys:
        # 开了开关却没配 key：拒绝，避免静默失效
        return AuthResult(False, "denied", reason="No API keys configured on server")

    token = extract_token(request.headers)
    if not token:
        return AuthResult(
            False, "denied",
            reason="Missing API key. Send 'Authorization: Bearer <key>' or 'x-api-key: <key>'",
        )

    for name, expected in keys.items():
        if secrets.compare_digest(token, expected):
            return AuthResult(True, "key", principal=name)

    return AuthResult(False, "denied", reason="Invalid API key")
