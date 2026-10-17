"""测试跑在 PostgreSQL 上时，把 SQLite 引擎换成 PG schema 的那部分（conftest.py 和多进程测试的子进程共用）。

子进程（test_gifts / test_red_packets 用来模拟多个 worker 进程同时花币）不经过 pytest，也就没有 conftest；
父进程把「SQLite 文件路径 → schema」写进环境变量 KUKE_TEST_PG_SCHEMAS，子进程继承下来，按同一张表连同一个 schema。
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

SCHEMA_MAP_ENV = "KUKE_TEST_PG_SCHEMAS"


def test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", "").strip()


def using_postgres() -> bool:
    return test_database_url().startswith("postgresql")


def postgres_engine(factory: Callable[..., Any], schema: str, *args: Any, **kwargs: Any) -> Any:
    """用 factory（真正的 create_async_engine）建一个连 TEST_DATABASE_URL、search_path 指向 schema 的引擎。"""
    from sqlalchemy import event
    from sqlalchemy.engine import make_url
    from sqlalchemy.util import await_only

    for key in ("connect_args", "poolclass", "pool_size", "max_overflow"):
        kwargs.pop(key, None)
    engine = factory(
        make_url(test_database_url()),
        *args,
        # application_name 用来在测试结束时找出这个 schema 上没关掉的连接
        connect_args={"server_settings": {"search_path": f"{schema},public", "application_name": schema}},
        **kwargs,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _ensure_schema(dbapi_connection, _record) -> None:  # noqa: ANN001
        await_only(dbapi_connection.driver_connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    return engine


def publish_schema(url: str, schema: str) -> None:
    mapping = json.loads(os.environ.get(SCHEMA_MAP_ENV) or "{}")
    mapping[url] = schema
    os.environ[SCHEMA_MAP_ENV] = json.dumps(mapping)


def for_worker(factory: Callable[..., Any]) -> Callable[..., Any]:
    """子进程里用：返回一个和 create_async_engine 同签名的函数，SQLite 文件 URL 换成父进程分给它的 schema。"""
    if not using_postgres():
        return factory
    mapping = json.loads(os.environ.get(SCHEMA_MAP_ENV) or "{}")

    def create(url: Any, *args: Any, **kwargs: Any) -> Any:
        text_url = str(url)
        if not text_url.startswith("sqlite"):
            return factory(url, *args, **kwargs)
        if text_url not in mapping:
            raise RuntimeError(f"no PostgreSQL schema published for {text_url}")
        return postgres_engine(factory, mapping[text_url], *args, **kwargs)

    return create


def _requires_sqlite():
    import unittest

    return unittest.skipIf(using_postgres(), "SQLite-specific (PRAGMA / EXPLAIN QUERY PLAN / legacy SQLite schema migration)")


# 只对 SQLite 有意义的测试（PRAGMA、查询计划、老 SQLite 库的迁移）：测试跑在 PG 上时跳过
requires_sqlite = _requires_sqlite()
