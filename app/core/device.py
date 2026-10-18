"""设备信号的请求侧：签发 / 校验设备令牌、把客户端带来的标识算成库里存的哈希（docs/device-bans.md）。

客户端每个请求带这几个头（都可以缺）：

- ``X-Kuke-Device``：服务端签发的设备令牌 ``kd_<随机 22 位>.<签名 22 位>``。缺了或签名不对，响应里带
  ``X-Kuke-Device-Issue`` 发一个新的，客户端存下来以后每次都带。
- ``X-Kuke-Machine``：桌面端 / 安卓的机器码，客户端已经加盐 SHA-256 过（64 位十六进制）。
- ``X-Kuke-Fp``：浏览器指纹的 SHA-256（64 位十六进制）。
- ``X-Kuke-Platform``：web / desktop / android / ios。

库里只存 HMAC(服务端密钥, 值)：库泄露了也拿不回客户端那边的原值，也没法拿别处的值来撞。
这里全是纯计算（每个请求两三次 HMAC，微秒级），不碰数据库。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
from dataclasses import dataclass, field

from starlette.datastructures import Headers, MutableHeaders

from app.core.config import settings

ISSUE_HEADER = "X-Kuke-Device-Issue"
PLATFORMS = ("web", "desktop", "android", "ios")
KINDS = ("device", "machine", "fp", "ip")

_TOKEN_RE = re.compile(r"kd_([A-Za-z0-9_-]{22})\.([A-Za-z0-9_-]{22})")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
# 签发 / 哈希用的密钥从 JWT 密钥派生，换 JWT 密钥会让所有设备令牌失效（客户端会被重新发一个，不影响使用）
_KEY = hmac.new(settings.jwt_secret_key.encode("utf-8"), b"kukechat-device-v1", hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sign(device_id: str) -> str:
    return _b64(hmac.new(_KEY, f"kd|{device_id}".encode("utf-8"), hashlib.sha256).digest())[:22]


def issue_token() -> str:
    device_id = secrets.token_urlsafe(16)[:22]
    return f"kd_{device_id}.{_sign(device_id)}"


def verify_token(token: str | None) -> str | None:
    """签名对得上返回令牌里的随机 id，否则 None。"""
    match = _TOKEN_RE.fullmatch((token or "").strip())
    if match is None:
        return None
    device_id, signature = match.groups()
    return device_id if hmac.compare_digest(signature, _sign(device_id)) else None


def signal_hash(kind: str, value: str) -> str:
    """库里存的值：device / machine / fp 是 HMAC，ip 是原文。"""
    if kind == "ip":
        return value
    return hmac.new(_KEY, f"{kind}|{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def ip_prefix(ip: str | None) -> str | None:
    """IPv4 取 /24、IPv6 取 /64；不是合法 IP 返回 None。"""
    try:
        address = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    bits = 24 if address.version == 4 else 64
    return str(ipaddress.ip_network(f"{address}/{bits}", strict=False))


def normalize_ip(ip: str | None) -> str | None:
    try:
        address = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


def mask(kind: str, value: str) -> str:
    """后台展示用：哈希只给前 8 位，IP 给原文。"""
    return value if kind == "ip" else f"{value[:8]}…"


def client_ip(headers: Headers, client: tuple[str, int] | None) -> str:
    """和 api/v1/auth.py 的 get_client_ip 同一套取法（nginx 会写 X-Forwarded-For / X-Real-IP）。"""
    forwarded_for = headers.get("x-forwarded-for")
    if forwarded_for:
        first_ip = forwarded_for.split(",", 1)[0].strip()
        if first_ip:
            return first_ip
    real_ip = headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if client:
        return client[0]
    return "unknown"


def _platform(headers: Headers) -> str | None:
    value = (headers.get("x-kuke-platform") or "").strip().lower()
    if value in PLATFORMS:
        return value
    client = (headers.get("x-kuke-client") or "").lower()
    if "android" in client:
        return "android"
    return None


def _hex64(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    return value if _HEX64_RE.fullmatch(value) else None


@dataclass
class DeviceContext:
    """一个请求带来的设备信号。哈希都已经算好，可以直接拿去比对封禁、写库。"""

    ip: str | None = None
    prefix: str | None = None
    platform: str | None = None
    device_id: str | None = None
    # kind -> 库里存的值（只含这次请求真的带了的）
    hashes: dict[str, str] = field(default_factory=dict)
    issued_token: str | None = None

    @property
    def has_device(self) -> bool:
        return self.device_id is not None

    def ensure_issued(self) -> str:
        """这次请求没带有效令牌时要发的新令牌（同一个请求里多次调用拿到同一个）。"""
        if self.issued_token is None:
            self.issued_token = issue_token()
        return self.issued_token

    def signals(self) -> list[tuple[str, str]]:
        """[(kind, 库里存的值)]，ip 排在最后。"""
        items = [(kind, value) for kind, value in self.hashes.items()]
        if self.ip is not None:
            items.append(("ip", self.ip))
        return items


def context_from_headers(headers: Headers, client: tuple[str, int] | None) -> DeviceContext:
    ip = normalize_ip(client_ip(headers, client))
    context = DeviceContext(ip=ip, prefix=ip_prefix(ip), platform=_platform(headers))
    device_id = verify_token(headers.get("x-kuke-device"))
    if device_id is not None:
        context.device_id = device_id
        context.hashes["device"] = signal_hash("device", device_id)
    machine = _hex64(headers.get("x-kuke-machine"))
    if machine is not None:
        context.hashes["machine"] = signal_hash("machine", machine)
    fingerprint = _hex64(headers.get("x-kuke-fp"))
    if fingerprint is not None:
        context.hashes["fp"] = signal_hash("fp", fingerprint)
    return context


_STATE_KEY = "kuke_device"


def context_for_request(request) -> DeviceContext:  # noqa: ANN001
    """请求的设备信号。中间件已经算过就直接用（存在 scope["state"] 里），否则现算一次并记下。"""
    state = request.scope.setdefault("state", {})
    context = state.get(_STATE_KEY)
    if context is None:
        context = context_from_headers(request.headers, request.scope.get("client"))
        state[_STATE_KEY] = context
    return context


def _wants_device(path: str) -> bool:
    # 后台和机器人开放接口不参与（后台不发设备令牌；机器人用密钥认证）
    return path.startswith("/api/v1/") and not path.startswith(("/api/v1/admin", "/api/v1/bot-api"))


class DeviceTokenMiddleware:
    """纯 ASGI 中间件：算好设备信号放进 scope，没带有效令牌的响应加一个 X-Kuke-Device-Issue。不碰数据库。"""

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http" or scope.get("method") == "OPTIONS" or not _wants_device(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        context = context_from_headers(Headers(scope=scope), scope.get("client"))
        scope.setdefault("state", {})[_STATE_KEY] = context
        if context.has_device:
            await self.app(scope, receive, send)
            return

        async def send_with_token(message) -> None:  # noqa: ANN001
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append(ISSUE_HEADER, context.ensure_issued())
            await send(message)

        await self.app(scope, receive, send_with_token)
