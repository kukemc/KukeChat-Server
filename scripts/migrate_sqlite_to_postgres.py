#!/usr/bin/env python
"""把线上 SQLite 库整库迁到 PostgreSQL（docs/postgres-cutover.md）。

    python scripts/migrate_sqlite_to_postgres.py --sqlite /www/wwwroot/api/kukechat.db \\
        --pg postgresql+asyncpg://kukechat:密码@127.0.0.1:5432/kukechat [--batch 2000] [--report report.json]

流程：
1. 只读打开 SQLite（不会改源库），先跑一遍 quick_check。
2. 用后端自己的 init_db 在目标库上建好表结构（表、索引、搜索索引表、触发器）。目标库必须是空的
   （有数据就拒绝，除非带 --truncate 清空重来）。
3. 为了快：先摘掉外键约束和非唯一索引、关掉 messages 上的触发器，按外键依赖顺序逐表用 COPY 批量导入，
   导完再把它们装回去（装外键时数据库会整体校验一遍）。主键 / 唯一约束全程都在。
4. 逐列做类型转换：时间一律转成带时区的 UTC（SQLite 里存的是不带时区的 UTC 字符串）、0/1 → 布尔、日期、整数、浮点。
5. 脏数据不静默吞掉，逐类统计并打印：
   - 外键指向不存在的行（孤儿行）：外键列可空且不是级联删除的 → 置空；否则 → 跳过这一行（它的父行早就被删了）。
   - 字符串超过列长度 → 截断；文本里的 NUL 字符（PG 不允许）→ 删掉。
   - 时间 / 日期 / 数字解析不了 → 列可空就置空，否则跳过这一行。
   - 违反唯一约束（模型后来加的约束，老 SQLite 表上没有）→ 跳过后来的那一行。
   - 存 JSON 的文本列里解析不了的值 → 只统计，原样搬过去（应用读的时候本来就有兜底）。
6. 重建聊天记录搜索索引（message_search_index，不从 SQLite 搬），重置每张表的自增序列，ANALYZE。
7. 核对：逐表行数（SQLite 行数 − 跳过的行数 = PG 行数），每张表抽样若干行逐列比对。

整个导入在一个事务里：中途失败会整体回滚，目标库回到「只有空表」的状态，修好问题后加 --truncate 重跑即可。
只用标准库、SQLAlchemy 和 asyncpg（后端本来就要装的）。不打印任何数据内容，只打印统计数字。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import secrets
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 迁移脚本不签发令牌，但导入 app 时配置校验要求有 JWT_SECRET_KEY；服务器上 .env 里有，本地演练时没有就给个一次性的
os.environ.setdefault("JWT_SECRET_KEY", secrets.token_urlsafe(48))

import asyncpg  # noqa: E402
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Integer, Numeric, String, Table  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

import app.models  # noqa: E402,F401
from app.core.database import Base, init_db  # noqa: E402
from app.services.schema_extras import INDEXED_TYPES  # noqa: E402

UTC = timezone.utc
INT4_MAX = 2**31 - 1
INT8_MAX = 2**63 - 1
# 不从 SQLite 搬、导完在 PG 上重建的表
REBUILT_TABLES = {"message_search_index"}
SAMPLE_ROWS = 25


# ---------------------------------------------------------------- 统计


@dataclass
class TableStats:
    name: str
    source_rows: int = 0
    copied_rows: int = 0
    skipped_rows: int = 0
    target_rows: int | None = None
    seconds: float = 0.0
    sample_checked: int = 0
    sample_mismatches: int = 0


@dataclass
class Report:
    tables: dict[str, TableStats] = field(default_factory=dict)
    # (表, 列, 问题, 处理) -> 次数
    issues: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)

    def issue(self, table: str, column: str, problem: str, action: str, count: int = 1) -> None:
        self.issues[(table, column, problem, action)] += count


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------- 类型转换


class Skip(Exception):
    """这一行搬不了（必填列的值无效），整行跳过。"""


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # 极少数地方可能存成了 unix 时间戳
        return datetime.fromtimestamp(float(value), tz=UTC)
    elif isinstance(value, bytes):
        parsed = datetime.fromisoformat(value.decode("utf-8").strip())
    else:
        text_value = str(value).strip()
        if text_value.endswith(("Z", "z")):
            text_value = text_value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text_value)
    # SQLAlchemy 往 SQLite 里存 DateTime(timezone=True) 时丢掉了时区，存的是 UTC 墙上时间
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text_value = value.decode("utf-8") if isinstance(value, bytes) else str(value)
    text_value = text_value.strip()
    if len(text_value) > 10:
        return datetime.fromisoformat(text_value).date()
    return date.fromisoformat(text_value)


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text_value = (value.decode("utf-8") if isinstance(value, bytes) else str(value)).strip().lower()
    if text_value in ("1", "true", "t", "yes", "y"):
        return True
    if text_value in ("0", "false", "f", "no", "n", ""):
        return False
    raise ValueError(f"not a boolean: {text_value[:20]!r}")


def _parse_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("non-integer float")
        return int(value)
    return int(str(value).strip())


def _parse_float(value: Any) -> float:
    result = float(value)
    if math.isnan(result) or math.isinf(result):
        raise ValueError("nan/inf")
    return result


class ColumnPlan:
    """一列怎么从 SQLite 的值变成 PG 要的值。"""

    def __init__(self, table: Table, column: Any, report: Report) -> None:
        self.table = table.name
        self.name = column.name
        self.column = column
        self.nullable = bool(column.nullable) and not column.primary_key
        self.report = report
        column_type = column.type
        self.kind = "other"
        self.max_length: int | None = None
        # 判断「这是不是一个存 JSON 的文本列」用：非空值里有多少像 JSON、其中多少解析不了
        self.nonempty = 0
        self.json_like = 0
        self.json_invalid = 0
        if isinstance(column_type, DateTime):
            self.kind = "datetime"
        elif isinstance(column_type, Date):
            self.kind = "date"
        elif isinstance(column_type, Boolean):
            self.kind = "bool"
        elif isinstance(column_type, BigInteger):
            self.kind = "bigint"
        elif isinstance(column_type, Integer):
            self.kind = "int"
        elif isinstance(column_type, (Float, Numeric)):
            self.kind = "float"
        elif isinstance(column_type, String):
            self.kind = "str"
            self.max_length = getattr(column_type, "length", None)

    def report_json(self) -> None:
        """九成以上的非空值都像 JSON 的列算 JSON 列：报告其中解析不了 / 根本不是 JSON 的值（原样搬，应用读时有兜底）。"""
        if self.kind != "str" or not self.nonempty or self.json_like < 0.9 * self.nonempty:
            return
        bad = self.json_invalid + (self.nonempty - self.json_like)
        if bad:
            self.report.issue(self.table, self.name, "JSON 列里有无效 JSON", "原样保留", bad)

    def _invalid(self, problem: str) -> None:
        if self.nullable:
            self.report.issue(self.table, self.name, problem, "置空")
            return None
        self.report.issue(self.table, self.name, problem, "跳过整行")
        raise Skip

    def convert(self, value: Any) -> Any:
        if value is None:
            return None
        kind = self.kind
        try:
            if kind == "datetime":
                return _parse_datetime(value)
            if kind == "date":
                return _parse_date(value)
            if kind == "bool":
                return _parse_bool(value)
            if kind in ("int", "bigint"):
                result = _parse_int(value)
                limit = INT4_MAX if kind == "int" else INT8_MAX
                if abs(result) > limit:
                    return self._invalid("整数超出列的范围")
                return result
            if kind == "float":
                return _parse_float(value)
        except Skip:
            raise
        except Exception:  # noqa: BLE001
            return self._invalid(f"{kind} 值无法解析")
        if kind == "str":
            if isinstance(value, bytes):
                try:
                    value = value.decode("utf-8")
                except UnicodeDecodeError:
                    value = value.decode("utf-8", errors="replace")
                    self.report.issue(self.table, self.name, "非 UTF-8 字节", "替换成 U+FFFD")
            elif not isinstance(value, str):
                value = str(value)
            if "\x00" in value:
                value = value.replace("\x00", "")
                self.report.issue(self.table, self.name, "文本里有 NUL 字符（PG 不允许）", "删掉 NUL")
            if self.max_length is not None and len(value) > self.max_length:
                self.report.issue(self.table, self.name, f"超过长度 {self.max_length}", "截断")
                value = value[: self.max_length]
            if value:
                self.nonempty += 1
                if value[:1] in ("{", "["):
                    self.json_like += 1
                    try:
                        json.loads(value)
                    except ValueError:
                        self.json_invalid += 1
            return value
        return value


# ---------------------------------------------------------------- 外键


@dataclass
class ForeignKeyPlan:
    column: str
    parent_table: str
    parent_column: str
    # 孤儿行的处理：null = 置空，skip = 跳过整行
    action: str


def _foreign_key_plans(table: Table) -> list[ForeignKeyPlan]:
    plans = []
    for column in table.columns:
        for fk in column.foreign_keys:
            ondelete = (fk.ondelete or "").upper()
            nullable = bool(column.nullable)
            action = "null" if nullable and ondelete != "CASCADE" else "skip"
            plans.append(ForeignKeyPlan(column.name, fk.column.table.name, fk.column.name, action))
    return plans


# ---------------------------------------------------------------- 主流程


def _pg_dsn(url: str) -> dict[str, Any]:
    parsed = make_url(url)
    return {
        "user": parsed.username,
        "password": parsed.password,
        "host": parsed.host or "localhost",
        "port": parsed.port or 5432,
        "database": parsed.database,
    }


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _target_is_empty(conn: asyncpg.Connection, tables: list[Table]) -> list[str]:
    nonempty = []
    for table in tables:
        exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table.name)
        if exists and await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {_quote(table.name)})"):
            nonempty.append(table.name)
    return nonempty


async def migrate(args: argparse.Namespace) -> int:
    report = Report()
    started = time.perf_counter()
    sqlite_path = Path(args.sqlite).resolve()
    if not sqlite_path.is_file():
        log(f"找不到 SQLite 库：{sqlite_path}")
        return 2
    source = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    if not args.skip_check:
        t = time.perf_counter()
        result = source.execute("PRAGMA quick_check").fetchone()[0]
        log(f"[检查] SQLite quick_check：{result}（{time.perf_counter() - t:.1f}s）")
        if result != "ok":
            log("源库自检没通过，先修好（或换一个备份）再迁移。确实要继续可加 --skip-check。")
            return 2

    source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    model_tables = [table for table in Base.metadata.sorted_tables if table.name not in REBUILT_TABLES]
    ignored = sorted(
        name
        for name in source_tables
        if name not in Base.metadata.tables and name not in REBUILT_TABLES and not name.startswith(("sqlite_", "message_search_fts"))
    )
    if ignored:
        report.notes.append(f"SQLite 里有、模型里没有的表（不迁移）：{', '.join(ignored)}")

    # ---- 1. 目标库：建结构
    dsn = _pg_dsn(args.pg)
    pg = await asyncpg.connect(**dsn, server_settings={"application_name": "kukechat-migrate"})
    await pg.execute("SET statement_timeout = 0")
    await pg.execute("SET lock_timeout = 0")
    await pg.execute("SET idle_in_transaction_session_timeout = 0")
    nonempty = await _target_is_empty(pg, model_tables)
    if nonempty:
        if not args.truncate:
            log(f"目标库里已经有数据（{', '.join(nonempty[:8])}{' 等' if len(nonempty) > 8 else ''}），拒绝覆盖。确认要清空重来就加 --truncate。")
            return 2
        names = ", ".join(_quote(table.name) for table in model_tables if await pg.fetchval("SELECT to_regclass($1) IS NOT NULL", table.name))
        await pg.execute(f"TRUNCATE {names}, message_search_index RESTART IDENTITY CASCADE")
        log(f"[准备] 已清空目标库的 {len(nonempty)} 张非空表")

    t = time.perf_counter()
    engine = create_async_engine(args.pg)
    try:
        await init_db(engine)
    finally:
        await engine.dispose()
    log(f"[结构] init_db 建表完成（{time.perf_counter() - t:.1f}s）")

    # ---- 2. 摘掉外键、非唯一索引，关触发器（都在同一个事务里，失败整体回滚）
    tx = pg.transaction()
    await tx.start()
    try:
        fks = await pg.fetch(
            """
            SELECT c.conrelid::regclass::text AS table_name, c.conname, pg_get_constraintdef(c.oid) AS definition
            FROM pg_constraint c
            WHERE c.contype = 'f' AND c.connamespace = current_schema()::regnamespace
            """
        )
        for row in fks:
            await pg.execute(f"ALTER TABLE {row['table_name']} DROP CONSTRAINT {_quote(row['conname'])}")
        indexes = await pg.fetch(
            """
            SELECT i.indexrelid::regclass::text AS index_name, pg_get_indexdef(i.indexrelid) AS definition
            FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid
            WHERE t.relnamespace = current_schema()::regnamespace AND NOT i.indisunique AND NOT i.indisprimary
            """
        )
        for row in indexes:
            await pg.execute(f"DROP INDEX {row['index_name']}")
        await pg.execute("ALTER TABLE messages DISABLE TRIGGER USER")
        log(f"[准备] 暂时摘掉 {len(fks)} 个外键、{len(indexes)} 个非唯一索引，关掉 messages 的触发器")

        # ---- 3. 逐表导入
        # 被外键引用的列 → 已导入的值（孤儿检查用）
        referenced: dict[tuple[str, str], set[Any]] = defaultdict(set)
        fk_plans = {table.name: _foreign_key_plans(table) for table in model_tables}
        wanted_refs = {(plan.parent_table, plan.parent_column) for plans in fk_plans.values() for plan in plans}
        for table in model_tables:
            stats = TableStats(table.name)
            report.tables[table.name] = stats
            if table.name not in source_tables:
                report.notes.append(f"SQLite 里没有表 {table.name}（建了空表）")
                continue
            t = time.perf_counter()
            await _copy_table(source, pg, table, fk_plans[table.name], referenced, wanted_refs, stats, report, args.batch)
            stats.seconds = time.perf_counter() - t
            skipped = f"，跳过 {stats.skipped_rows}" if stats.skipped_rows else ""
            log(f"[导入] {table.name}: {stats.copied_rows}/{stats.source_rows} 行{skipped}（{stats.seconds:.1f}s）")

        # ---- 4. 装回索引、外键、触发器；重建搜索索引；重置序列
        t = time.perf_counter()
        for row in indexes:
            await pg.execute(row["definition"])
        log(f"[收尾] 重建 {len(indexes)} 个索引（{time.perf_counter() - t:.1f}s）")
        t = time.perf_counter()
        not_valid = 0
        for row in fks:
            try:
                async with pg.transaction():
                    await pg.execute(f"ALTER TABLE {row['table_name']} ADD CONSTRAINT {_quote(row['conname'])} {row['definition']}")
            except asyncpg.PostgresError as exc:
                # 不该发生（孤儿行在导入时已经处理了）。真发生了就以 NOT VALID 装上（只管以后的写），并报出来
                not_valid += 1
                report.notes.append(f"外键 {row['table_name']}.{row['conname']} 校验失败，已按 NOT VALID 装上：{exc.__class__.__name__}")
                await pg.execute(f"ALTER TABLE {row['table_name']} ADD CONSTRAINT {_quote(row['conname'])} {row['definition']} NOT VALID")
        log(f"[收尾] 装回 {len(fks)} 个外键并校验（{time.perf_counter() - t:.1f}s{f'，{not_valid} 个 NOT VALID' if not_valid else ''}）")
        await pg.execute("ALTER TABLE messages ENABLE TRIGGER USER")
        t = time.perf_counter()
        await pg.execute("TRUNCATE message_search_index")
        await pg.execute(
            "INSERT INTO message_search_index (message_id, conversation_id, sender_id, created_at, category, text) "
            "SELECT m.id, m.conversation_id, m.sender_id, m.created_at, "
            "kc_message_search_category(m.type, m.content, m.metadata), kc_message_search_text(m.type, m.content, m.metadata) "
            f"FROM messages m WHERE m.type IN {INDEXED_TYPES} AND m.recalled_at IS NULL"
        )
        search_rows = await pg.fetchval("SELECT count(*) FROM message_search_index")
        log(f"[收尾] 重建聊天记录搜索索引：{search_rows} 行（{time.perf_counter() - t:.1f}s）")
        if "message_search_index" in source_tables:
            old_rows = source.execute("SELECT count(*) FROM message_search_index").fetchone()[0]
            report.notes.append(f"搜索索引：SQLite {old_rows} 行，PG 重建后 {search_rows} 行")
        t = time.perf_counter()
        sequences = 0
        for table in model_tables:
            for column in table.primary_key.columns:
                sequence = await pg.fetchval("SELECT pg_get_serial_sequence($1, $2)", table.name, column.name)
                if sequence:
                    await pg.execute(
                        f"SELECT setval($1, COALESCE((SELECT MAX({_quote(column.name)}) FROM {_quote(table.name)}), 0) + 1, false)", sequence
                    )
                    sequences += 1
        log(f"[收尾] 重置 {sequences} 个自增序列（{time.perf_counter() - t:.1f}s）")
        await tx.commit()
    except BaseException:
        await tx.rollback()
        raise

    t = time.perf_counter()
    await pg.execute("ANALYZE")
    log(f"[收尾] ANALYZE（{time.perf_counter() - t:.1f}s）")

    # ---- 5. 核对
    t = time.perf_counter()
    count_errors = 0
    for table in model_tables:
        stats = report.tables[table.name]
        stats.target_rows = await pg.fetchval(f"SELECT count(*) FROM {_quote(table.name)}")
        if stats.target_rows != stats.source_rows - stats.skipped_rows:
            count_errors += 1
    mismatched_tables = 0
    for table in model_tables:
        if table.name in source_tables and report.tables[table.name].copied_rows:
            await _sample_check(source, pg, table, report.tables[table.name], report)
            if report.tables[table.name].sample_mismatches:
                mismatched_tables += 1
    seq_errors = await _check_sequences(pg, model_tables)
    log(f"[核对] 行数不符 {count_errors} 张表；抽样不一致 {mismatched_tables} 张表；序列落后 {seq_errors} 个（{time.perf_counter() - t:.1f}s）")
    await pg.close()
    source.close()

    _print_summary(report, time.perf_counter() - started)
    if args.report:
        _write_report(report, Path(args.report), time.perf_counter() - started)
    return 0 if count_errors == 0 and mismatched_tables == 0 and seq_errors == 0 else 1


async def _copy_table(
    source: sqlite3.Connection,
    pg: asyncpg.Connection,
    table: Table,
    fk_plans: list[ForeignKeyPlan],
    referenced: dict[tuple[str, str], set[Any]],
    wanted_refs: set[tuple[str, str]],
    stats: TableStats,
    report: Report,
    batch_size: int,
) -> None:
    source_columns = {row[1] for row in source.execute(f"PRAGMA table_info({_quote(table.name)})")}
    columns = [column for column in table.columns if column.name in source_columns]
    extra = sorted(source_columns - {column.name for column in table.columns})
    if extra:
        report.notes.append(f"{table.name}：SQLite 里多出的列不迁移：{', '.join(extra)}")
    for column in table.columns:
        if column.name in source_columns:
            continue
        if not column.nullable and column.server_default is None:
            raise RuntimeError(f"{table.name}.{column.name} 在 SQLite 里不存在，又是必填且没有数据库默认值，没法迁移")
        report.notes.append(f"{table.name}.{column.name}：SQLite 里没有这一列，用数据库默认值")
    names = [column.name for column in columns]
    plans = [ColumnPlan(table, column, report) for column in columns]
    position = {name: index for index, name in enumerate(names)}
    keep_refs = [(index, (table.name, name)) for name, index in position.items() if (table.name, name) in wanted_refs]
    self_refs = {plan.column for plan in fk_plans if plan.parent_table == table.name}
    # 自引用外键（子任务、转发之类）：父行可能排在后面，先把整张表的被引用列读出来做集合（只给本表自查用；
    # 别的表查这张表时用的仍是实际导进去的行）。
    self_parents: dict[str, set[Any]] = {}
    for plan in fk_plans:
        if plan.parent_table == table.name and plan.parent_column not in self_parents:
            self_parents[plan.parent_column] = {
                value for (value,) in source.execute(f"SELECT {_quote(plan.parent_column)} FROM {_quote(table.name)}")
            }
    fk_checks = [
        (
            position[plan.column],
            plan,
            self_parents[plan.parent_column] if plan.parent_table == table.name else referenced[(plan.parent_table, plan.parent_column)],
        )
        for plan in fk_plans
        if plan.column in position
    ]

    stats.source_rows = source.execute(f"SELECT count(*) FROM {_quote(table.name)}").fetchone()[0]
    order = "rowid" if not table.primary_key.columns else ", ".join(_quote(column.name) for column in table.primary_key.columns)
    cursor = source.execute(f"SELECT {', '.join(_quote(name) for name in names)} FROM {_quote(table.name)} ORDER BY {order}")
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        records = []
        for raw in rows:
            try:
                record = [plan.convert(value) for plan, value in zip(plans, raw)]
                for index, plan, parents in fk_checks:
                    value = record[index]
                    if value is None or value in parents:
                        continue
                    if plan.action == "null":
                        record[index] = None
                        report.issue(table.name, plan.column, f"外键指向不存在的 {plan.parent_table}", "置空")
                    else:
                        report.issue(table.name, plan.column, f"外键指向不存在的 {plan.parent_table}", "跳过整行")
                        raise Skip
            except Skip:
                stats.skipped_rows += 1
                continue
            records.append(tuple(record))
        accepted = await _copy_batch(pg, table, names, records, report, stats)
        for record in accepted:
            for index, key in keep_refs:
                if record[index] is not None:
                    referenced[key].add(record[index])
        stats.copied_rows += len(accepted)
    for plan in plans:
        plan.report_json()
    if self_refs:
        # 被跳过的行不能再当父行：再查一遍自引用，指向它们的置空 / 删掉（极少见）
        for plan in fk_plans:
            if plan.parent_table != table.name or plan.column not in position:
                continue
            orphans = await pg.fetchval(
                f"SELECT count(*) FROM {_quote(table.name)} c WHERE c.{_quote(plan.column)} IS NOT NULL AND NOT EXISTS "
                f"(SELECT 1 FROM {_quote(table.name)} p WHERE p.{_quote(plan.parent_column)} = c.{_quote(plan.column)})"
            )
            if not orphans:
                continue
            if plan.action == "null":
                await pg.execute(
                    f"UPDATE {_quote(table.name)} c SET {_quote(plan.column)} = NULL WHERE c.{_quote(plan.column)} IS NOT NULL AND NOT EXISTS "
                    f"(SELECT 1 FROM {_quote(table.name)} p WHERE p.{_quote(plan.parent_column)} = c.{_quote(plan.column)})"
                )
                report.issue(table.name, plan.column, f"自引用指向被跳过的 {table.name}", "置空", orphans)
            else:
                report.notes.append(f"{table.name}.{plan.column}：{orphans} 行自引用指向被跳过的行，外键会以 NOT VALID 装上")


async def _copy_batch(
    pg: asyncpg.Connection, table: Table, names: list[str], records: list[tuple], report: Report, stats: TableStats
) -> list[tuple]:
    if not records:
        return []
    try:
        async with pg.transaction():
            await pg.copy_records_to_table(table.name, records=records, columns=names)
        return records
    except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError, asyncpg.NotNullViolationError, asyncpg.DataError) as exc:
        first_error = exc.__class__.__name__
    # 这一批里有坏行：逐行重试，找出来跳过，其余照常导入
    accepted = []
    for record in records:
        try:
            async with pg.transaction():
                await pg.copy_records_to_table(table.name, records=[record], columns=names)
            accepted.append(record)
        except asyncpg.PostgresError as exc:
            stats.skipped_rows += 1
            report.issue(table.name, "*", f"导入被拒（{exc.__class__.__name__}）", "跳过整行")
    if len(accepted) == len(records):
        report.notes.append(f"{table.name}：一批导入报 {first_error}，逐行重试后全部成功")
    return accepted


def _normalize(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 9)
    return value


async def _sample_check(source: sqlite3.Connection, pg: asyncpg.Connection, table: Table, stats: TableStats, report: Report) -> None:
    """抽样：首行、末行和随机若干行，按主键在两边各取一次，SQLite 那边按同样的规则转换后逐列比较。"""
    pk = list(table.primary_key.columns)
    if len(pk) != 1:
        return
    key = pk[0].name
    source_columns = {row[1] for row in source.execute(f"PRAGMA table_info({_quote(table.name)})")}
    columns = [column for column in table.columns if column.name in source_columns]
    names = [column.name for column in columns]
    ids = [row[0] for row in source.execute(f"SELECT {_quote(key)} FROM {_quote(table.name)} ORDER BY {_quote(key)}")]
    if not ids:
        return
    picks = {ids[0], ids[-1], *random.sample(ids, min(SAMPLE_ROWS, len(ids)))}
    scratch = Report()
    plans = [ColumnPlan(table, column, scratch) for column in columns]
    select_list = ", ".join(_quote(name) for name in names)
    for pick in picks:
        raw = source.execute(f"SELECT {select_list} FROM {_quote(table.name)} WHERE {_quote(key)} = ?", (pick,)).fetchone()
        row = await pg.fetchrow(f"SELECT {select_list} FROM {_quote(table.name)} WHERE {_quote(key)} = $1", pick)
        if row is None:
            continue  # 被跳过的行（脏数据），已经计在 skipped 里
        stats.sample_checked += 1
        try:
            expected = [plan.convert(value) for plan, value in zip(plans, raw)]
        except Skip:
            continue
        for index, name in enumerate(names):
            got = row[name]
            want = expected[index]
            # 孤儿外键被置空的列：PG 是 NULL、SQLite 有值，这是预期的差异
            if got is None and want is not None and any(fk for fk in table.columns[name].foreign_keys):
                continue
            if _normalize(got) != _normalize(want):
                stats.sample_mismatches += 1
                report.issue(table.name, name, "抽样比对不一致", "需人工查看")
                break


async def _check_sequences(pg: asyncpg.Connection, tables: list[Table]) -> int:
    behind = 0
    for table in tables:
        for column in table.primary_key.columns:
            sequence = await pg.fetchval("SELECT pg_get_serial_sequence($1, $2)", table.name, column.name)
            if not sequence:
                continue
            row = await pg.fetchrow(f"SELECT last_value, is_called FROM {sequence}")
            next_value = row["last_value"] + 1 if row["is_called"] else row["last_value"]
            current_max = await pg.fetchval(f"SELECT COALESCE(MAX({_quote(column.name)}), 0) FROM {_quote(table.name)}")
            if next_value <= current_max:
                behind += 1
    return behind


def _print_summary(report: Report, seconds: float) -> None:
    log("")
    log("=" * 72)
    total_source = sum(stats.source_rows for stats in report.tables.values())
    total_copied = sum(stats.copied_rows for stats in report.tables.values())
    total_skipped = sum(stats.skipped_rows for stats in report.tables.values())
    log(f"完成：{len(report.tables)} 张表，SQLite {total_source} 行 → PG {total_copied} 行，跳过 {total_skipped} 行，用时 {seconds:.1f}s")
    bad = [stats for stats in report.tables.values() if stats.target_rows != stats.source_rows - stats.skipped_rows]
    if bad:
        log("行数不符的表：")
        for stats in bad:
            log(f"  {stats.name}: SQLite {stats.source_rows} − 跳过 {stats.skipped_rows} ≠ PG {stats.target_rows}")
    sampled = sum(stats.sample_checked for stats in report.tables.values())
    mismatches = sum(stats.sample_mismatches for stats in report.tables.values())
    log(f"抽样核对：{sampled} 行，不一致 {mismatches} 行")
    if report.issues:
        log("脏数据（表.列 / 问题 / 处理 / 次数）：")
        for (table, column, problem, action), count in sorted(report.issues.items()):
            log(f"  {table}.{column} / {problem} / {action} / {count}")
    else:
        log("脏数据：无")
    for note in report.notes:
        log(f"备注：{note}")


def _write_report(report: Report, path: Path, seconds: float) -> None:
    payload = {
        "seconds": round(seconds, 1),
        "tables": {
            name: {
                "source_rows": stats.source_rows,
                "copied_rows": stats.copied_rows,
                "skipped_rows": stats.skipped_rows,
                "target_rows": stats.target_rows,
                "seconds": round(stats.seconds, 2),
                "sample_checked": stats.sample_checked,
                "sample_mismatches": stats.sample_mismatches,
            }
            for name, stats in report.tables.items()
        },
        "issues": [
            {"table": table, "column": column, "problem": problem, "action": action, "count": count}
            for (table, column, problem, action), count in sorted(report.issues.items())
        ],
        "notes": report.notes,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告已写到 {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="把 KukeChat 的 SQLite 库迁到 PostgreSQL")
    parser.add_argument("--sqlite", required=True, help="SQLite 库文件（只读打开）")
    parser.add_argument("--pg", required=True, help="目标库，postgresql+asyncpg://用户:密码@主机:端口/库名")
    parser.add_argument("--batch", type=int, default=2000, help="每批 COPY 的行数（默认 2000）")
    parser.add_argument("--truncate", action="store_true", help="目标库已有数据时先清空（只在重跑演练 / 迁移失败重来时用）")
    parser.add_argument("--skip-check", action="store_true", help="跳过 SQLite 的 quick_check")
    parser.add_argument("--report", help="把统计结果另存成 JSON")
    args = parser.parse_args()
    if args.batch < 1:
        parser.error("--batch 至少为 1")
    return asyncio.run(migrate(args))


if __name__ == "__main__":
    sys.exit(main())
