"""Safe deployment bootstrap for command-line hosting panels.

Run with: /path/to/python deploy_start.py
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import uuid
from urllib.parse import urlparse
from pathlib import Path


CORE_MODULES = (
    "fastapi",
    "uvicorn",
    "gunicorn",
    "sqlalchemy",
    "pydantic_settings",
    "PIL",
    "boto3",
    "redis",
)


def read_dotenv_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() != key:
            continue
        return value.strip().strip('"').strip("'") or None
    return None


def redis_is_reachable(redis_url: str | None) -> bool:
    if not redis_url:
        return False
    parsed = urlparse(redis_url)
    host = parsed.hostname
    port = parsed.port or 6379
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def missing_modules() -> list[str]:
    return [module for module in CORE_MODULES if importlib.util.find_spec(module) is None]


def install_dependencies(requirements_path: Path) -> None:
    if not requirements_path.is_file():
        raise FileNotFoundError(f"requirements.txt not found: {requirements_path}")

    install_command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
        str(requirements_path),
        "gunicorn",
        "uvicorn[standard]",
    ]
    subprocess.check_call(install_command)


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    requirements_path = script_dir / "requirements.txt"

    if missing_modules():
        install_dependencies(requirements_path)

    host = os.environ.get("HOST", "127.0.0.1")
    port = os.environ.get("PORT", "8000")
    dotenv_redis_url = read_dotenv_value(script_dir / ".env", "REDIS_URL")
    redis_url = os.environ.get("REDIS_URL") or dotenv_redis_url
    redis_ready = redis_is_reachable(redis_url)
    os.environ.setdefault("REALTIME_INSTANCE_ID", uuid.uuid4().hex)
    # Multiple workers require REDIS_URL so realtime events can fan out across
    # process-local WebSocket connection registries.
    workers = os.environ.get("WEB_CONCURRENCY", "2" if redis_ready else "1")
    timeout = os.environ.get("GUNICORN_TIMEOUT", "120")
    graceful_timeout = os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30")
    bind = f"{host}:{port}"
    command = [
        sys.executable,
        "-m",
        "gunicorn",
        "app.main:app",
        "-k",
        "uvicorn.workers.UvicornWorker",
        "--bind",
        bind,
        "--workers",
        workers,
        "--timeout",
        timeout,
        "--graceful-timeout",
        graceful_timeout,
    ]

    if os.name == "posix":
        os.execv(sys.executable, command)

    completed = subprocess.call(command)
    raise SystemExit(completed)


if __name__ == "__main__":
    main()
