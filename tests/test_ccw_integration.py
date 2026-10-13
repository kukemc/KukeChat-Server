"""CCW 相关业务逻辑（app/services/ccw.py）和集成点加载器（app/integrations）的测试。

取数全部走假网关（integration_fakes.FakeCcwGateway），不发任何外部请求。三块：

- 加载器：没有私有包时用占位实现；私有包存在但有问题时报错而不是悄悄退回占位实现。
- 接入了 CCW（假网关）：绑定口令的生成与匹配、资料同步、作品卡片、评论验证码改密。
- 没有接入 CCW（占位网关）：绑定未开放、同步和卡片静默跳过、worker 不启动。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app import integrations
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.security import verify_password
from app.integrations import CcwUnavailable, CoinsUnavailable, NullCcwGateway, NullCoinLedger, loader
from app.models.user import CcwBindingChallenge, User
from app.services import auth as auth_service
from app.services import ccw, response_cache
from integration_fakes import FakeCcwGateway, FakeCoinLedger

SUBJECT = "c0ffee" * 4
STUDENT, OTHER_STUDENT = "a" * 24, "b" * 24
CREATION = "1" * 24


def comment(code: str, student_oid: str = STUDENT, comment_id: int = 7, **commenter: Any) -> dict[str, Any]:
    return {"id": comment_id, "content": f"绑定 {code.lower()} 谢谢", "commenter": {"accountOid": student_oid, "name": "评论里的名字", "avatar": "https://img.example/comment.png", **commenter}}


def student_payloads(oid: str = STUDENT) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = {"body": {"studentOid": oid.upper(), "name": "小明", "avatar": "https://img.example/a.png", "bio": "你好", "memberArchive": {"homepageCover": "https://img.example/cover.png"}, "commentCount": 3}}
    detail = {"body": {"oid": oid, "followingCount": 1, "followerCount": 2, "likeCount": 3, "favoriteCount": 4, "creationCount": 5, "viewCount": 6}}
    return profile, detail


# ---------------------------------------------------------------- 加载器


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例从「还没选过默认实现」开始，结束后恢复现场
        saved = (loader._defaults, loader._gateway_override, loader._ledger_override, loader.PRIVATE_PACKAGE)
        loader._defaults, loader._gateway_override, loader._ledger_override = None, None, None

        def restore() -> None:
            loader._defaults, loader._gateway_override, loader._ledger_override, loader.PRIVATE_PACKAGE = saved

        self.addCleanup(restore)

    def _package(self, source: str) -> str:
        """在临时目录里造一个包，返回包名；用完从 sys.path 和 sys.modules 里清掉。"""
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        name = f"kc_fake_private_{len(sys.modules)}_{id(tmp)}"
        package = Path(tmp.name) / name
        package.mkdir()
        (package / "__init__.py").write_text(source, encoding="utf-8")
        sys.path.insert(0, tmp.name)
        self.addCleanup(sys.path.remove, tmp.name)
        self.addCleanup(sys.modules.pop, name, None)
        return name

    def test_without_private_package_null_implementations_are_used(self) -> None:
        loader.PRIVATE_PACKAGE = "app._definitely_not_there"
        self.assertFalse(integrations.private_package_present())
        integrations.load()
        self.assertIsInstance(integrations.get_ccw_gateway(), NullCcwGateway)
        self.assertIsInstance(integrations.get_coin_ledger(), NullCoinLedger)
        self.assertEqual(
            integrations.describe(),
            {"private": False, "ccw_gateway": "app.integrations.ccw_gateway.NullCcwGateway", "coin_ledger": "app.integrations.coin_ledger.NullCoinLedger"},
        )
        # 再调一次不会重新挑
        first = integrations.get_ccw_gateway()
        integrations.load()
        self.assertIs(integrations.get_ccw_gateway(), first)

    def test_null_implementations_refuse_everything(self) -> None:
        gateway, ledger = NullCcwGateway(), NullCoinLedger()
        self.assertEqual((gateway.available(), ledger.configured(), ledger.topup_url(), ledger.source().id, ledger.source().setting), (False, False, None, None, None))

        async def run() -> None:
            for call in (gateway.latest_comments(SUBJECT, "CREATION"), gateway.student_payloads(STUDENT), gateway.creation_detail(CREATION)):
                with self.assertRaises(CcwUnavailable) as caught:
                    await call
                self.assertIsNone(caught.exception.upstream_status)
            for call in (ledger.credited_to(STUDENT), ledger.recent_credits(), ledger.all_credits(fresh=True)):
                with self.assertRaises(CoinsUnavailable):
                    await call

        asyncio.run(run())

    def test_private_package_is_used_when_present(self) -> None:
        loader.PRIVATE_PACKAGE = self._package(
            "from integration_fakes import FakeCcwGateway, FakeCoinLedger\n"
            "def create_ccw_gateway():\n    return FakeCcwGateway()\n"
            "def create_coin_ledger():\n    return FakeCoinLedger()\n"
        )
        self.assertTrue(integrations.private_package_present())
        integrations.load()
        self.assertIsInstance(integrations.get_ccw_gateway(), FakeCcwGateway)
        self.assertIsInstance(integrations.get_coin_ledger(), FakeCoinLedger)
        self.assertTrue(integrations.describe()["private"])

    def test_broken_private_package_fails_loudly(self) -> None:
        cases = {
            # 私有包自己 import 不了：原样报出来，不能当成「没有私有包」
            "import kc_module_that_does_not_exist\n": ModuleNotFoundError,
            "raise RuntimeError('boom')\n": RuntimeError,
            # 缺工厂函数
            "def create_ccw_gateway():\n    return None\n": TypeError,
            "from integration_fakes import FakeCcwGateway\ndef create_ccw_gateway():\n    return FakeCcwGateway()\n": ImportError,
            # 工厂返回的不是接口的实现
            "from integration_fakes import FakeCcwGateway\ndef create_ccw_gateway():\n    return FakeCcwGateway()\ndef create_coin_ledger():\n    return object()\n": TypeError,
        }
        for source, error in cases.items():
            loader.PRIVATE_PACKAGE = self._package(source)
            loader._defaults = None
            with self.subTest(source=source), self.assertRaises(error):
                integrations.load()
            # 失败之后没有留下半截的默认实现
            self.assertIsNone(loader._defaults)

    def test_overrides_take_precedence_and_restore(self) -> None:
        loader.PRIVATE_PACKAGE = "app._definitely_not_there"
        gateway, ledger = FakeCcwGateway(), FakeCoinLedger()
        integrations.set_ccw_gateway(gateway)
        self.assertIs(integrations.get_ccw_gateway(), gateway)
        # 换网关不影响账本
        self.assertIsInstance(integrations.get_coin_ledger(), NullCoinLedger)
        with integrations.use_coin_ledger(ledger):
            self.assertIs(integrations.get_coin_ledger(), ledger)
            inner = FakeCcwGateway()
            with integrations.use_ccw_gateway(inner):
                self.assertIs(integrations.get_ccw_gateway(), inner)
            self.assertIs(integrations.get_ccw_gateway(), gateway)
        self.assertIsInstance(integrations.get_coin_ledger(), NullCoinLedger)
        integrations.set_ccw_gateway(None)
        self.assertIsInstance(integrations.get_ccw_gateway(), NullCcwGateway)


# ---------------------------------------------------------------- 业务逻辑


class CcwTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob")]
            db.add_all(users)
            await db.commit()
            self.ids = {user.username: user.id for user in users}
        self.gateway = self._make_gateway()
        integrations.set_ccw_gateway(self.gateway)
        self.addCleanup(integrations.set_ccw_gateway, None)
        for name, value in {"ccw_bind_subject_oid": SUBJECT, "ccw_bind_subject_type": "CREATION", "ccw_bind_guest_id": "guest-for-tests", "ccw_bind_challenge_minutes": 20}.items():
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await ccw.stop_ccw_binding_worker()
        await self.engine.dispose()

    def _make_gateway(self) -> Any:
        return FakeCcwGateway()

    async def _user(self, name: str = "alice") -> User:
        async with self.sessions() as db:
            return await db.get(User, self.ids[name])

    async def _challenge(self, name: str = "alice") -> CcwBindingChallenge:
        async with self.sessions() as db:
            return await ccw.create_binding_challenge(db, await db.get(User, self.ids[name]))

    async def _check(self, name: str | None = None) -> int:
        async with self.sessions() as db:
            return await ccw.check_binding_challenges_once(db, self.ids[name] if name else None)

    async def _latest(self, name: str = "alice") -> CcwBindingChallenge | None:
        async with self.sessions() as db:
            return await ccw.latest_binding_challenge(db, self.ids[name])

    async def _bind(self, name: str = "alice", oid: str = STUDENT) -> None:
        async with self.sessions() as db:
            (await db.get(User, self.ids[name])).ccw_student_oid = oid
            await db.commit()


class BindingTests(CcwTestCase):
    async def test_challenge_code_and_reuse(self) -> None:
        self.assertTrue(ccw.ccw_binding_configured())
        challenge = await self._challenge()
        self.assertRegex(challenge.code, r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
        self.assertEqual((challenge.status, challenge.subject_oid), ("pending", SUBJECT))
        self.assertEqual(ccw.ccw_bind_creation_url(), f"https://www.ccw.site/detail/{SUBJECT}")
        # 还没过期的口令直接复用，不重新生成
        self.assertEqual((await self._challenge()).id, challenge.id)
        self.assertNotEqual((await self._challenge("bob")).code, challenge.code)
        self.assertEqual(self.gateway.calls, [])

    async def test_missing_settings_mean_not_configured(self) -> None:
        for name in ("ccw_bind_subject_oid", "ccw_bind_guest_id"):
            with self.subTest(name=name), patch.object(settings, name, " "):
                self.assertFalse(ccw.ccw_binding_configured())
                with self.assertRaises(HTTPException) as caught:
                    await self._challenge()
                self.assertEqual(caught.exception.status_code, 503)
                self.assertIn("CCW_BIND_SUBJECT_OID", caught.exception.detail)

    async def test_matching_comment_binds_the_account(self) -> None:
        challenge = await self._challenge()
        self.gateway.students[STUDENT] = student_payloads()
        # 口令不在评论里：什么都不变
        self.gateway.comments = [comment("AAAA-AAAA")]
        self.assertEqual(await self._check("alice"), 0)
        self.assertEqual((await self._latest()).status, "pending")
        self.assertEqual(self.gateway.calls, [("latest_comments", SUBJECT, "CREATION")])

        self.gateway.comments = [comment("AAAA-AAAA", OTHER_STUDENT), comment(challenge.code, STUDENT.upper(), 99)]
        self.assertEqual(await self._check(), 1)
        latest = await self._latest()
        self.assertEqual((latest.status, latest.matched_student_oid, latest.matched_comment_id, latest.matched_name), ("verified", STUDENT, "99", "小明"))
        self.assertIsNotNone(latest.verified_at)
        user = await self._user()
        self.assertEqual(
            (user.ccw_student_oid, user.ccw_name, user.ccw_avatar_url, user.ccw_bio, user.ccw_homepage_cover_url),
            (STUDENT, "小明", "https://img.example/a.png", "你好", "https://img.example/cover.png"),
        )
        self.assertEqual(
            (user.ccw_following_count, user.ccw_follower_count, user.ccw_like_count, user.ccw_favorite_count, user.ccw_comment_count, user.ccw_creation_count, user.ccw_view_count),
            (1, 2, 3, 4, 3, 5, 6),
        )
        # 绑定只记 CCW 那边的资料，不动 KukeChat 自己的昵称头像
        self.assertEqual((user.nickname, user.avatar_url), (None, None))
        self.assertIn(("student_payloads", STUDENT), self.gateway.calls)

    async def test_binding_falls_back_to_the_commenter_when_profile_is_unavailable(self) -> None:
        challenge = await self._challenge()
        self.gateway.comments = [comment(challenge.code)]
        # 学生资料取不到（假网关里没登记这个学生）：用评论里带的名字和头像
        self.assertEqual(await self._check(), 1)
        user = await self._user()
        self.assertEqual((user.ccw_student_oid, user.ccw_name, user.ccw_avatar_url, user.ccw_follower_count), (STUDENT, "评论里的名字", "https://img.example/comment.png", None))

    async def test_account_already_bound_elsewhere_is_a_conflict(self) -> None:
        await self._bind("bob", STUDENT)
        challenge = await self._challenge()
        self.gateway.comments = [comment(challenge.code)]
        self.assertEqual(await self._check(), 0)
        self.assertEqual(((await self._latest()).status, (await self._user()).ccw_student_oid), ("conflict", None))

    async def test_odd_comments_are_ignored(self) -> None:
        challenge = await self._challenge()
        self.gateway.comments = [
            {"id": 1, "content": challenge.code},
            {"id": 2, "content": challenge.code, "commenter": "nope"},
            {"id": 3, "content": challenge.code, "commenter": {"accountOid": "not-an-oid"}},
            {"id": 4, "content": None, "commenter": {"accountOid": STUDENT}},
        ]
        self.assertEqual(await self._check(), 0)
        self.assertEqual((await self._latest()).status, "pending")

    async def test_expired_challenges_are_not_checked(self) -> None:
        challenge = await self._challenge()
        async with self.sessions() as db:
            (await db.get(CcwBindingChallenge, challenge.id)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        self.gateway.comments = [comment(challenge.code)]
        self.assertEqual(await self._check(), 0)
        # 没有待验证的口令就不去取评论
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual((await self._latest()).status, "expired")

    async def test_gateway_failure_propagates_as_unavailable(self) -> None:
        await self._challenge()
        self.gateway.error = OSError("down")
        with self.assertRaises(CcwUnavailable):
            await self._check()
        self.assertEqual((await self._latest()).status, "pending")

    async def test_worker_runs_only_when_configured(self) -> None:
        with patch.object(ccw, "AsyncSessionLocal", self.sessions), patch.object(settings, "ccw_bind_poll_interval_seconds", 3600):
            with patch.object(settings, "ccw_bind_guest_id", None):
                ccw.start_ccw_binding_worker()
                self.assertIsNone(ccw._ccw_binding_task)
            challenge = await self._challenge()
            self.gateway.comments = [comment(challenge.code)]
            ccw.start_ccw_binding_worker()
            task = ccw._ccw_binding_task
            self.assertIsNotNone(task)
            # 重复启动不会起第二个
            ccw.start_ccw_binding_worker()
            self.assertIs(ccw._ccw_binding_task, task)
            for _ in range(200):
                if (await self._latest()).status == "verified":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual((await self._latest()).status, "verified")
            await ccw.stop_ccw_binding_worker()
            self.assertTrue(task.cancelled())


class ProfileSyncTests(CcwTestCase):
    async def _sync(self, sync_profile: bool = False) -> User:
        async with self.sessions() as db:
            return await ccw.sync_bound_student(db, await db.get(User, self.ids["alice"]), sync_profile=sync_profile)

    async def test_sync_requires_binding(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await self._sync()
        self.assertEqual((caught.exception.status_code, self.gateway.calls), (400, []))

    async def test_sync_updates_ccw_fields_and_optionally_the_profile(self) -> None:
        await self._bind()
        self.gateway.students[STUDENT] = student_payloads()
        user = await self._sync()
        self.assertEqual((user.ccw_name, user.ccw_follower_count, user.nickname, user.bio), ("小明", 2, None, None))
        self.assertIsNotNone(user.ccw_synced_at)
        user = await self._sync(sync_profile=True)
        self.assertEqual((user.nickname, user.avatar_url, user.bio, user.profile_cover_url, user.profile_cover_moderation_status), ("小明", "https://img.example/a.png", "你好", "https://img.example/cover.png", "approved"))

    async def test_sync_failure_is_a_502_and_changes_nothing(self) -> None:
        await self._bind()
        # 没有这个学生 / 网关出错 / 取到了但里面的账号 oid 不对
        for problem in ("missing", "error", "garbage"):
            self.gateway.error = OSError("down") if problem == "error" else None
            self.gateway.students = {STUDENT: ({"body": {"studentOid": "not-an-oid"}}, None)} if problem == "garbage" else {}
            with self.subTest(problem=problem), self.assertRaises(HTTPException) as caught:
                await self._sync()
            self.assertEqual((caught.exception.status_code, caught.exception.detail), (502, "同步 CCW 账号信息失败"))
            self.assertIsNone((await self._user()).ccw_synced_at)

    async def test_unbind_clears_everything(self) -> None:
        await self._bind()
        self.gateway.students[STUDENT] = student_payloads()
        await self._sync()
        async with self.sessions() as db:
            user = await ccw.unbind_student(db, await db.get(User, self.ids["alice"]))
        self.assertEqual((user.ccw_student_oid, user.ccw_name, user.ccw_follower_count, user.ccw_synced_at), (None, None, None, None))


class PreviewTests(CcwTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        cache: dict[str, Any] = {}
        for patcher in (
            patch.object(response_cache, "get_ccw_creation", side_effect=cache.get),
            patch.object(response_cache, "set_ccw_creation", side_effect=cache.__setitem__),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_extract_creation_refs(self) -> None:
        text = f"看这个 https://www.ccw.site/detail/{CREATION.upper()}?accessKey=k1 ，还有 http://ccw.site/detail/{'2' * 24}。 重复 https://www.ccw.site/detail/{CREATION}?accessKey=k1 不是作品 https://www.ccw.site/student/{STUDENT}"
        self.assertEqual(ccw.extract_creation_refs(text), [{"oid": CREATION, "access_key": "k1"}, {"oid": "2" * 24, "access_key": ""}])
        self.assertEqual(ccw.extract_creation_refs(text, limit=1), [{"oid": CREATION, "access_key": "k1"}])
        self.assertEqual(ccw.extract_creation_refs("没有链接"), [])
        self.assertEqual(ccw.ccw_profile_url(STUDENT), f"https://www.ccw.site/student/{STUDENT}")

    async def test_preview_card_is_built_and_cached(self) -> None:
        self.gateway.creations[CREATION] = {
            "body": {
                "oid": CREATION.upper(),
                "title": "我的作品",
                "description": "介绍",
                "featuredCoverLink": "https://img.example/cover.png",
                "studentOid": STUDENT,
                "student": {"name": "小明", "avatar": "https://img.example/a.png"},
                "viewCount": 10,
                "likeCount": 2,
                "tags": ["游戏", 5, "动画"],
                "creationRelease": {"version": "1.2"},
            }
        }
        preview = await ccw.fetch_creation_preview(CREATION.upper(), " key ")
        self.assertEqual(
            {name: preview[name] for name in ("oid", "access_key", "url", "title", "cover_url", "author_name", "author_oid", "view_count", "version", "tags")},
            {
                "oid": CREATION,
                "access_key": "key",
                "url": f"https://www.ccw.site/detail/{CREATION}?accessKey=key",
                "title": "我的作品",
                "cover_url": "https://img.example/cover.png",
                "author_name": "小明",
                "author_oid": STUDENT,
                "view_count": 10,
                "version": "1.2",
                "tags": ["游戏", "动画"],
            },
        )
        self.assertEqual(await ccw.fetch_creation_preview(CREATION, "key"), preview)
        self.assertEqual(self.gateway.calls, [("creation_detail", CREATION, "key")])

    async def test_unresolvable_links_are_dropped_from_messages(self) -> None:
        self.gateway.creations[CREATION] = {"body": {"oid": CREATION, "title": "有"}}
        self.gateway.creations["3" * 24] = {"unexpected": True}
        text = f"https://www.ccw.site/detail/{'2' * 24} https://www.ccw.site/detail/{CREATION} https://www.ccw.site/detail/{'3' * 24}"
        with self.assertLogs("app.services.ccw", level=logging.WARNING) as logs:
            metadata = await ccw.enrich_metadata_with_previews(text, {"client": "kept"})
        self.assertEqual(([item["oid"] for item in metadata["ccw_creations"]], metadata["client"]), ([CREATION], "kept"))
        # 取不到的（没有这个作品）和看不懂的（没有 body）各记一条
        self.assertEqual(len(logs.records), 2)
        self.assertEqual(await ccw.enrich_metadata_with_previews("没有链接", {"client": "kept"}), {"client": "kept"})


class PasswordChallengeTests(CcwTestCase):
    async def _confirm(self, code: str) -> None:
        async with self.sessions() as db:
            await auth_service.confirm_ccw_password_challenge(db, await db.get(User, self.ids["alice"]), code, "reset", "new-password-123")

    async def test_code_in_my_own_comment_resets_the_password(self) -> None:
        await self._bind()
        async with self.sessions() as db:
            challenge = await auth_service.create_ccw_password_challenge(db, await db.get(User, self.ids["alice"]), "reset")
        # 别人发的同一个验证码不算
        self.gateway.comments = [comment(challenge.code, OTHER_STUDENT)]
        with self.assertRaises(HTTPException) as caught:
            await self._confirm(challenge.code)
        self.assertEqual(caught.exception.status_code, 404)
        self.gateway.comments = [comment(challenge.code, STUDENT.upper(), 41)]
        await self._confirm(f" {challenge.code.lower()} ")
        self.assertTrue(verify_password("new-password-123", (await self._user()).hashed_password))
        self.assertEqual(self.gateway.calls, [("latest_comments", SUBJECT, "CREATION")] * 2)


class HttpCcwTests(CcwTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.api.v1.router import api_router

        api = FastAPI()
        api.include_router(api_router)

        async def isolated_db():
            async with self.sessions() as session:
                yield session

        async def acting_user(db: Annotated[AsyncSession, Depends(get_db)]) -> User:
            return await db.get(User, self.ids["alice"])

        api.dependency_overrides[get_db] = isolated_db
        api.dependency_overrides[get_current_user] = acting_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)
        patcher = patch.object(response_cache, "get_ccw_creation", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _json(self, method: str, path: str, expected: int = 200, **kwargs: Any) -> Any:
        response = await self.client.request(method, f"/api/v1{path}", **kwargs)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    async def test_binding_flow(self) -> None:
        self.assertIsNone(await self._json("GET", "/ccw/binding/me"))
        challenge = await self._json("POST", "/ccw/binding/challenge")
        self.assertEqual((challenge["status"], challenge["subject_oid"], challenge["creation_url"]), ("pending", SUBJECT, f"https://www.ccw.site/detail/{SUBJECT}"))
        self.gateway.error = OSError("down")
        self.assertEqual((await self._json("POST", "/ccw/binding/check", 502))["detail"], "检查 CCW 评论失败，请稍后重试")
        self.gateway.error = None
        self.gateway.comments = [comment(challenge["code"])]
        self.gateway.students[STUDENT] = student_payloads()
        checked = await self._json("POST", "/ccw/binding/check")
        self.assertEqual((checked["status"], checked["matched_student_oid"], checked["matched_name"]), ("verified", STUDENT, "小明"))
        synced = await self._json("POST", "/ccw/me/sync", json={"sync_profile": True})
        self.assertEqual((synced["ccw_student_oid"], synced["nickname"]), (STUDENT, "小明"))
        self.assertIsNone((await self._json("DELETE", "/ccw/me"))["ccw_student_oid"])

    async def test_creation_preview_errors(self) -> None:
        self.assertEqual((await self._json("GET", "/ccw/creations/nope", 400))["detail"], "无效的作品 ID")
        self.gateway.creations[CREATION] = {"body": {"oid": CREATION, "title": "有"}}
        self.assertEqual((await self._json("GET", f"/ccw/creations/{CREATION.upper()}?accessKey=k"))["url"], f"https://www.ccw.site/detail/{CREATION}?accessKey=k")
        # 对方明确返回了错误码 / 连不上 / 内容看不懂，分两种提示
        self.assertEqual((await self._json("GET", f"/ccw/creations/{'2' * 24}", 502))["detail"], "CCW 作品接口请求失败")
        self.gateway.error = OSError("down")
        self.assertEqual((await self._json("GET", f"/ccw/creations/{CREATION}", 502))["detail"], "无法解析 CCW 作品详情")
        self.gateway.error = None
        self.gateway.creations[CREATION] = {"unexpected": True}
        self.assertEqual((await self._json("GET", f"/ccw/creations/{CREATION}", 502))["detail"], "无法解析 CCW 作品详情")


# ---------------------------------------------------------------- 没有接入 CCW


class WithoutGatewayTests(HttpCcwTests):
    """占位网关（公开仓库单独跑时就是它）：绑定未开放，同步和卡片静默跳过，不刷日志。"""

    def _make_gateway(self) -> Any:
        return NullCcwGateway()

    async def test_binding_flow(self) -> None:
        # 配置项都填了也没用：没有网关就是未开放
        self.assertFalse(ccw.ccw_gateway_available())
        self.assertFalse(ccw.ccw_binding_configured())
        self.assertEqual((await self._json("POST", "/ccw/binding/challenge", 503))["detail"], "本服务未开放 CCW 账号绑定。")
        self.assertIsNone(await self._json("GET", "/ccw/binding/me"))
        self.assertIsNone(await self._json("POST", "/ccw/binding/check"))

    async def test_creation_preview_errors(self) -> None:
        self.assertEqual((await self._json("GET", "/ccw/creations/nope", 400))["detail"], "无效的作品 ID")
        self.assertEqual((await self._json("GET", f"/ccw/creations/{CREATION}", 503))["detail"], "本服务未接入 CCW，无法获取作品信息")

    async def test_leftover_pending_challenge_is_left_alone(self) -> None:
        async with self.sessions() as db:
            db.add(CcwBindingChallenge(user_id=self.ids["alice"], code="ABCD-EFGH", subject_oid=SUBJECT, expires_at=datetime.now(UTC) + timedelta(minutes=5)))
            await db.commit()
        self.assertEqual(await self._check(), 0)
        self.assertEqual((await self._json("POST", "/ccw/binding/check"))["status"], "pending")

    async def test_sync_is_skipped_silently(self) -> None:
        await self._bind()
        before = await self._user()
        synced = await self._json("POST", "/ccw/me/sync", json={"sync_profile": True})
        after = await self._user()
        self.assertEqual((synced["ccw_student_oid"], synced["nickname"]), (STUDENT, None))
        self.assertEqual((after.ccw_synced_at, after.ccw_name, after.nickname), (before.ccw_synced_at, None, None))
        # 解绑不需要网关
        self.assertIsNone((await self._json("DELETE", "/ccw/me"))["ccw_student_oid"])

    async def test_previews_are_skipped_without_logging(self) -> None:
        text = f"https://www.ccw.site/detail/{CREATION}"
        with self.assertNoLogs("app.services.ccw", level=logging.WARNING):
            self.assertEqual(await ccw.previews_from_text(text), [])
            self.assertEqual(await ccw.enrich_metadata_with_previews(text, {"client": "kept"}), {"client": "kept"})
        # 直接要一张卡片：取不到
        with self.assertRaises(CcwUnavailable):
            await ccw.fetch_creation_preview(CREATION)

    async def test_worker_does_not_start(self) -> None:
        ccw.start_ccw_binding_worker()
        self.assertIsNone(ccw._ccw_binding_task)
        await ccw.stop_ccw_binding_worker()

    async def test_password_challenge_cannot_be_confirmed(self) -> None:
        await self._bind()
        async with self.sessions() as db:
            user = await db.get(User, self.ids["alice"])
            challenge = await auth_service.create_ccw_password_challenge(db, user, "change")
            with self.assertRaises(HTTPException) as caught:
                await auth_service.confirm_ccw_password_challenge(db, user, challenge.code, "change", "new-password-123")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual((await db.scalar(select(User.hashed_password).where(User.id == self.ids["alice"]))), "x")


if __name__ == "__main__":
    unittest.main()
