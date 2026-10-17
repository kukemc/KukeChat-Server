"""测试的公共设置。

1. 关掉 ``unittest.IsolatedAsyncioTestCase`` 强制打开的 asyncio 调试模式：调试模式下每次调度都要抓调用栈，
   这台机器上整套测试会慢一个数量级。

2. ``TEST_DATABASE_URL=postgresql+asyncpg://...`` 时，把测试里所有 ``create_async_engine("sqlite+aiosqlite:...")``
   换成连这个 PostgreSQL 库的引擎，每个「库」是一个独立的 schema（search_path 指过去，后面跟着 public，
   pg_trgm 之类的扩展装在 public 里）。

   建表在 PG 上很慢（每张表、每个索引都要建文件），所以 schema 是复用的：每个测试进程维护一个 schema 池，
   测试里新建的引擎从池里借一个；测试结束后把借出的 schema 清空（逐表 DELETE、序列归 1、断开残留连接）还回池里。
   表结构留着，下一个测试的 create_all 发现表都在，只跑一遍 after_create 的补充结构，很快。

   - ``:memory:`` 每建一个引擎借一个 schema（和内存库一样互不相干）；
   - 文件库按路径借，同一个测试里同一个路径的多个引擎共用一个 schema（模拟多 worker 的测试要靠这个）；
   - 不在测试执行阶段建的引擎（比如 app.core.database 的全局引擎）占用一个 schema 直到进程结束。

   schema 名带进程号，pytest-xdist 并行也不会撞；本进程建的 schema 在测试结束时删掉。
   只针对 SQLite 的测试（PRAGMA、查询计划、老库迁移之类）用 tests/pg_redirect.py 的 ``requires_sqlite`` 跳过。

   用法：TEST_DATABASE_URL=postgresql+asyncpg://kukechat@localhost:5433/kukechat_test python -m pytest tests/ -n 8
"""

from __future__ import annotations

import asyncio
import itertools
import os
import unittest

import pytest

# ---------------------------------------------------------------- 1. asyncio 调试模式


def _setup_asyncio_runner_without_debug(self) -> None:  # noqa: ANN001
    assert self._asyncioRunner is None, "asyncio runner is already initialized"
    self._asyncioRunner = asyncio.Runner(debug=False)


if hasattr(unittest.IsolatedAsyncioTestCase, "_setupAsyncioRunner"):
    unittest.IsolatedAsyncioTestCase._setupAsyncioRunner = _setup_asyncio_runner_without_debug


# ---------------------------------------------------------------- 2. PostgreSQL

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
USING_POSTGRES = TEST_DATABASE_URL.startswith("postgresql")

# 只对 SQLite 有意义的测试用 tests/pg_redirect.py 里的 requires_sqlite 跳过

_all_schemas: list[str] = []
_free_schemas: list[str] = []
# 当前测试执行阶段借出的 schema（测试结束时清空归还）；None = 不在测试执行阶段
_borrowed: list[str] | None = None
_borrowed_files: dict[str, str] = {}

if USING_POSTGRES:
    import sqlalchemy.ext.asyncio as _sa_asyncio
    from sqlalchemy.engine import make_url

    import pg_redirect

    _real_create_async_engine = _sa_asyncio.create_async_engine
    _counter = itertools.count(1)

    def _take_schema() -> str:
        if _free_schemas:
            name = _free_schemas.pop()
        else:
            name = f"kt_{os.getpid()}_{next(_counter)}"
            _all_schemas.append(name)
        if _borrowed is not None:
            _borrowed.append(name)
        return name

    def _schema_for(url: str) -> str:
        if ":memory:" in url or url.rstrip("/") in ("sqlite+aiosqlite:", "sqlite:"):
            return _take_schema()
        if _borrowed is None:
            return _take_schema()
        if url not in _borrowed_files:
            _borrowed_files[url] = _take_schema()
            # 多进程测试的子进程按同一个路径连同一个 schema（tests/pg_redirect.py）
            pg_redirect.publish_schema(url, _borrowed_files[url])
        return _borrowed_files[url]

    def _postgres_engine(url, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        text_url = str(url)
        if not text_url.startswith("sqlite"):
            return _real_create_async_engine(url, *args, **kwargs)
        return pg_redirect.postgres_engine(_real_create_async_engine, _schema_for(text_url), *args, **kwargs)

    _sa_asyncio.create_async_engine = _postgres_engine

    # 只建一部分表（create_all(tables=[...])）在 SQLite 上没问题，在 PG 上外键指向没建的表会直接报错；
    # 反正 schema 是复用的，迟早要全建，PG 上一律建全。
    from sqlalchemy import MetaData

    _real_create_all = MetaData.create_all

    def _create_all_tables(self, bind, tables=None, checkfirst=True):  # noqa: ANN001
        if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
            tables = None
        return _real_create_all(self, bind, tables=tables, checkfirst=checkfirst)

    MetaData.create_all = _create_all_tables

    def _connect_kwargs() -> dict:
        url = make_url(TEST_DATABASE_URL)
        return {"user": url.username, "password": url.password, "host": url.host or "localhost", "port": url.port or 5432, "database": url.database}

    def _cleanup_sql(schema: str) -> str:
        """逐表 DELETE（按外键依赖倒序，不建新文件，比 TRUNCATE 快得多）+ 序列归 1。"""
        import app.models  # noqa: F401
        from app.core.database import Base

        names = [table.name for table in reversed(Base.metadata.sorted_tables)] + ["message_search_index"]
        body = "\n".join(
            f"IF to_regclass('\"{schema}\".\"{name}\"') IS NOT NULL THEN DELETE FROM \"{schema}\".\"{name}\"; END IF;" for name in names
        )
        return f"""
        DO $$
        DECLARE r record;
        BEGIN
        {body}
        FOR r IN SELECT schemaname, sequencename FROM pg_sequences WHERE schemaname = '{schema}' LOOP
            PERFORM setval(format('%I.%I', r.schemaname, r.sequencename)::regclass, 1, false);
        END LOOP;
        END $$;
        """

    async def _recycle(schemas: list[str]) -> list[str]:
        import asyncpg

        connection = await asyncpg.connect(**_connect_kwargs())
        recycled = []
        try:
            await connection.execute("SET statement_timeout = 0")
            for schema in schemas:
                # 测试没关掉的连接（可能还开着事务、拿着锁）先断开
                await connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = $1 AND pid <> pg_backend_pid()", schema
                )
                try:
                    # 触发器里的表名不带 schema，按 search_path 找
                    await connection.execute(f'SET search_path = "{schema}", public')
                    await connection.execute(_cleanup_sql(schema))
                    recycled.append(schema)
                except Exception as exc:  # noqa: BLE001
                    print(f"conftest: could not recycle schema {schema}: {exc}")
        finally:
            await connection.close()
        return recycled

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(item):  # noqa: ANN001
        global _borrowed
        _borrowed = []
        _borrowed_files.clear()
        try:
            yield
        finally:
            borrowed, _borrowed = _borrowed, None
            _borrowed_files.clear()
            if borrowed:
                _free_schemas.extend(asyncio.run(_recycle(borrowed)))


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ANN001
    if not USING_POSTGRES or not _all_schemas:
        return
    import asyncpg

    async def _drop() -> None:
        connection = await asyncpg.connect(**_connect_kwargs())
        try:
            await connection.execute("SET statement_timeout = 0")
            await connection.execute("SET lock_timeout = 0")
            for name in _all_schemas:
                await connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = $1", name)
                await connection.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        finally:
            await connection.close()

    try:
        asyncio.run(_drop())
    except Exception as exc:  # noqa: BLE001
        print(f"conftest: failed to drop test schemas: {exc}")
