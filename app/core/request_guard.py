from __future__ import annotations

import hashlib
import time

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.responses import Response


PROOF_WINDOW_SECONDS = 5 * 60
PROOF_SALT = "KukeChat"
ALLOWED_CLIENT_PREFIXES = ("kukechat-web/", "kukechat-admin/", "kukechat-bot/")


class ClientProofMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if should_skip(request):
            return await call_next(request)

        try:
            validate_client_proof(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)


def should_skip(request: Request) -> bool:
    if request.method == "OPTIONS":
        return True
    path = request.url.path
    if path.startswith("/api/v1/bot-api"):
        return True
    if not path.startswith("/api/v1"):
        return True
    if path.startswith("/api/v1/admin"):
        return False
    return False


def validate_client_proof(request: Request) -> None:
    client = request.headers.get("x-kuke-client", "")
    timestamp = request.headers.get("x-kuke-timestamp", "")
    nonce = request.headers.get("x-kuke-nonce", "")
    proof = request.headers.get("x-kuke-proof", "")

    if not client.startswith(ALLOWED_CLIENT_PREFIXES):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid client")
    if len(nonce) < 12 or len(nonce) > 64:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request nonce")

    try:
        request_time = int(timestamp) / 1000
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request timestamp") from exc

    if abs(time.time() - request_time) > PROOF_WINDOW_SECONDS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request expired")

    expected = hashlib.sha256(f"{request.method.upper()}|{request.url.path.replace('/api/v1', '', 1)}|{timestamp}|{nonce}|{PROOF_SALT}".encode("utf-8")).hexdigest()
    if not proof or not constant_time_equal(proof, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request proof")


def constant_time_equal(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    result = 0
    for left_char, right_char in zip(left.encode("utf-8"), right.encode("utf-8")):
        result |= left_char ^ right_char
    return result == 0
