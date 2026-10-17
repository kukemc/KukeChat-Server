"""SQLite / PostgreSQL 写法不一样的那几处，集中在这里。

- ``upsert_insert``：INSERT ... ON CONFLICT。两个方言的 insert 构造都有 ``on_conflict_do_nothing`` /
  ``on_conflict_do_update``，接口一样，只是得按会话实际连的库挑对那一个。
- ``greatest`` / ``least``：两个值取大 / 取小。SQLite 是多参数的 ``max()`` / ``min()``，PG 是 ``GREATEST()`` / ``LEAST()``
  （PG 的 ``max()`` 只有聚合版）。编译时按方言出 SQL，所以可以写在模块级的语句里。
- ``hours_since``：某个时间列距现在多少小时（浮点）。SQLite 用 julianday，PG 用 extract(epoch)。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, Table
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement, GenericFunction


def dialect_name(db: AsyncSession | AsyncConnection) -> str:
    if isinstance(db, AsyncConnection):
        return db.dialect.name
    return db.get_bind().dialect.name


def is_postgres(db: AsyncSession | AsyncConnection) -> bool:
    return dialect_name(db) == "postgresql"


async def lock_for_update(db: AsyncSession, statement: Any) -> None:
    """PG：执行 ``statement ... FOR UPDATE``，把这几行锁到事务结束。SQLite：什么都不做。

    为什么需要：SQLite 上一个事务只要写过一条，就拿着整库写锁直到提交，所以「先 UPDATE 一下自己的账户行，再查余额、
    再写」天然是全站串行的。PG 只锁碰到的行，而且在 READ COMMITTED 下，一条等过锁的 UPDATE 只会对被锁的那一行
    重新求值，WHERE 里对别的表的子查询（余额公式）还是语句开始时的快照 —— 两个进程会同时通过余额检查。
    先单独用一条 SELECT ... FOR UPDATE 把行锁拿到，之后的语句各自取新快照，就能看到对方已经提交的写。

    SQLite 上跳过：它的 SELECT 不开事务（sqlite3 驱动只在写语句前 BEGIN），加了也锁不住，反而白跑一趟。
    """
    if is_postgres(db):
        await db.execute(statement.with_for_update())


async def lock_row(db: AsyncSession, instance: Any) -> None:
    """PG：SELECT ... FOR UPDATE 锁住这个 ORM 对象对应的行，并用库里的最新值刷新它（别的进程刚提交的改动也看得到）。
    SQLite：什么都不做（理由同 lock_for_update）。"""
    if is_postgres(db):
        await db.refresh(instance, with_for_update=True)


def upsert_insert(db: AsyncSession | AsyncConnection, table: Table | Any):
    """按方言返回 insert(table)，可以接着 .on_conflict_do_nothing(...) / .on_conflict_do_update(...)。"""
    if is_postgres(db):
        return postgresql.insert(table)
    return sqlite.insert(table)


class greatest(GenericFunction):
    """两个（或多个）值里取最大。PG：GREATEST(...)；SQLite：max(...)（多参数的标量版）。"""

    name = "greatest"
    inherit_cache = True


class least(GenericFunction):
    name = "least"
    inherit_cache = True


@compiles(greatest, "sqlite")
def _greatest_sqlite(element, compiler, **kw):  # noqa: ANN001
    return f"max({compiler.process(element.clauses, **kw)})"


@compiles(least, "sqlite")
def _least_sqlite(element, compiler, **kw):  # noqa: ANN001
    return f"min({compiler.process(element.clauses, **kw)})"


class hours_since(FunctionElement):
    """列（带时区的时间）距现在多少小时。"""

    type = Float()
    name = "hours_since"
    inherit_cache = True


@compiles(hours_since)
def _hours_since_default(element, compiler, **kw):  # noqa: ANN001
    return f"(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - {compiler.process(element.clauses, **kw)})) / 3600.0)"


@compiles(hours_since, "sqlite")
def _hours_since_sqlite(element, compiler, **kw):  # noqa: ANN001
    return f"((julianday(CURRENT_TIMESTAMP) - julianday({compiler.process(element.clauses, **kw)})) * 24)"
