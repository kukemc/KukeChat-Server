"""语音口令识别：把一段录音和口令交给识别服务（settings.voice_asr_url，私有部署，见 docs/red-packets.md），判断说得对不对。

识别服务返回识别出的文字，以及两边转成拼音音节之后的编辑距离 distance 和口令的音节数 length；
能不能算「说对了」在这里按 allowed_errors 判断：口令越长允许错的音节越多，同音字、平翘舌、前后鼻音不算错（服务端已经归一）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.core.config import settings

logger = logging.getLogger(__name__)


class VoiceAsrUnavailable(RuntimeError):
    """识别服务没配、连不上或者出错。"""


@dataclass(frozen=True)
class VoiceMatch:
    text: str
    distance: int
    length: int
    duration: float

    @property
    def matched(self) -> bool:
        return self.length > 0 and self.distance <= allowed_errors(self.length)


def enabled() -> bool:
    return bool(settings.voice_asr_url)


def allowed_errors(length: int) -> int:
    """口令有 length 个音节时最多允许错几个：3 个以内一个都不能错，4–7 个错 1 个，8–12 个错 2 个，再长按 20%。"""
    if length <= 3:
        return 0
    if length <= 7:
        return 1
    if length <= 12:
        return 2
    return length // 5


def _multipart(fields: dict[str, str], filename: str, data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = f"----kukechat{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode())
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def _post(audio: bytes, filename: str, content_type: str, expected: str) -> dict:
    if not settings.voice_asr_url:
        raise VoiceAsrUnavailable("voice_asr_url is not configured")
    body, boundary = _multipart({"expected": expected}, filename, audio, content_type)
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if settings.content_moderation_token:
        headers["X-Moderation-Token"] = settings.content_moderation_token
    request = Request(f"{settings.voice_asr_url.rstrip('/')}/asr/match", data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=settings.voice_asr_timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        if exc.code in (413, 422):
            # 录音本身有问题（太长、读不出来）：按「没听清」处理，不当成服务故障
            return {"text": "", "distance": 10**6, "length": 1, "duration": 0, "error": detail}
        raise VoiceAsrUnavailable(f"HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise VoiceAsrUnavailable(str(exc)) from exc


async def match(audio: bytes, filename: str, content_type: str, expected: str) -> VoiceMatch:
    result = await asyncio.to_thread(_post, audio, filename, content_type, expected)
    try:
        return VoiceMatch(
            text=str(result.get("text") or ""),
            distance=int(result.get("distance")),
            length=int(result.get("length")),
            duration=float(result.get("duration") or 0),
        )
    except (TypeError, ValueError) as exc:
        raise VoiceAsrUnavailable(f"bad response: {result!r}"[:200]) from exc
