"""测试用的假集成点：假 CCW 网关、假金币账本。全在内存里，不发任何外部请求。

用法：

    from app import integrations
    from integration_fakes import FakeCcwGateway, FakeCoinLedger

    with integrations.use_ccw_gateway(FakeCcwGateway()) as gateway:
        gateway.comments = [...]
        ...

或者在 asyncSetUp 里 `integrations.set_coin_ledger(fake)`，再 `addCleanup(integrations.set_coin_ledger, None)`。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from typing import Any

from app.integrations import CcwGateway, CcwUnavailable, CoinLedger, CoinsUnavailable, LedgerSource, StudentPayloads

# 假账本的充值页地址
TOPUP_URL = "https://coins.example/topup"


class FakeCcwGateway(CcwGateway):
    """假网关。数据直接改属性；每次取数都记在 calls 里。

    - comments：latest_comments 返回的评论（新的在前）。
    - students：{学生 oid: (资料, 主页统计)}；没登记的学生取不到（抛 CcwUnavailable）。
    - creations：{作品 oid: 作品详情}；没登记的作品取不到。
    - creation_fetch：给了就用它取作品详情，签名是同步的 (oid, access_key) -> dict，会放进线程里跑
      （模拟真实网关「同步请求放线程池」，测并发和耗时要用）。它抛的异常除了 AssertionError 都会包成 CcwUnavailable。
    - error：不为 None 时所有取数都抛它（不是 CcwUnavailable 的会包一层）。
    - is_available：False 表示「没有接入 CCW」。
    """

    def __init__(self, *, creation_fetch: Callable[[str, str], dict[str, Any]] | None = None, is_available: bool = True) -> None:
        self.comments: list[dict[str, Any]] = []
        self.students: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
        self.creations: dict[str, dict[str, Any]] = {}
        self.creation_fetch = creation_fetch
        self.error: Exception | None = None
        self.is_available = is_available
        self.calls: list[tuple[Any, ...]] = []

    def available(self) -> bool:
        return self.is_available

    def _check(self) -> None:
        if not self.is_available:
            raise CcwUnavailable("没有接入")
        if self.error is not None:
            raise self.error if isinstance(self.error, CcwUnavailable) else CcwUnavailable(f"{type(self.error).__name__}: {self.error}")

    async def latest_comments(self, subject_oid: str, subject_type: str) -> list[dict[str, Any]]:
        self.calls.append(("latest_comments", subject_oid, subject_type))
        self._check()
        return [dict(comment) for comment in self.comments]

    async def student_payloads(self, student_oid: str) -> StudentPayloads:
        self.calls.append(("student_payloads", student_oid))
        self._check()
        if student_oid not in self.students:
            raise CcwUnavailable("没有这个学生", upstream_status=404)
        profile, detail = self.students[student_oid]
        return StudentPayloads(profile=profile, detail=detail)

    async def creation_detail(self, oid: str, access_key: str = "") -> dict[str, Any]:
        self.calls.append(("creation_detail", oid, access_key))
        self._check()
        if self.creation_fetch is not None:
            try:
                return await asyncio.to_thread(self.creation_fetch, oid, access_key)
            except (CcwUnavailable, AssertionError):
                raise
            except Exception as exc:
                raise CcwUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if oid not in self.creations:
            raise CcwUnavailable("没有这个作品", upstream_status=404)
        return self.creations[oid]


class FakeCoinLedger(CoinLedger):
    """假账本。entries 是 (账号标识, 累计入账) 的列表，按最近入账时间倒序（刚入账的排最前）。

    每次查询都记在 calls 里：("credited_to", 账号) / ("recent_credits", limit) / ("all_credits", fresh)。
    - error：不为 None 时所有查询都抛 CoinsUnavailable（不是 CoinsUnavailable 的会包一层）。
    - delay：每次查询要花多久（秒），测「同时来的请求搭同一次核实」时用。
    - source_id：余额来源的标识；设成 None 表示「没配置」。
    """

    def __init__(self, source_id: str | None = "fake-source", source_setting: str = "fake_setting") -> None:
        self.entries: list[tuple[str, float]] = []
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None
        self.delay = 0.0
        self.source_id = source_id
        self.source_setting = source_setting
        self.cache_resets = 0

    def donate(self, account_id: str, amount: float) -> None:
        """这个账号的累计入账变成 amount，并排到最前面（刚入账）。"""
        self.entries = [(account_id, amount), *[(account, value) for account, value in self.entries if account != account_id]]

    def fill(self, count: int) -> None:
        """在最前面塞 count 个路人，把之前的人挤到后面去。"""
        base = len(self.entries)
        self.entries = [(f"{base + index:024x}", 1.0) for index in range(count)] + self.entries

    def configured(self) -> bool:
        return self.source_id is not None

    def source(self) -> LedgerSource:
        return LedgerSource(id=self.source_id, setting=self.source_setting if self.source_id is not None else None)

    def topup_url(self) -> str | None:
        return TOPUP_URL if self.source_id is not None else None

    def reset_cache(self) -> None:
        self.cache_resets += 1

    async def _query(self, *call: Any) -> dict[str, int]:
        self.calls.append(call)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.source_id is None:
            raise CoinsUnavailable("没有配置余额来源")
        if self.error is not None:
            raise self.error if isinstance(self.error, CoinsUnavailable) else CoinsUnavailable(f"请求失败：{type(self.error).__name__}")
        credits: dict[str, int] = {}
        for account, amount in self.entries:
            key = account.strip().lower()
            # 数额按整数用、向下取整；同一个账号出现两次取大的
            credits[key] = max(credits.get(key, 0), math.floor(amount))
        return credits

    async def credited_to(self, account_id: str) -> int:
        return (await self._query("credited_to", account_id)).get((account_id or "").strip().lower(), 0)

    async def recent_credits(self, limit: int | None = None) -> dict[str, int]:
        credits = await self._query("recent_credits", limit)
        return dict(list(credits.items())[:limit]) if limit else credits

    async def all_credits(self, *, fresh: bool = False) -> dict[str, int]:
        return await self._query("all_credits", fresh)
