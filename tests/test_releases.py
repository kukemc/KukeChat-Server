"""发版服务（docs/releases.md）的测试。存储用本地目录，直传的预签名打桩，对象用「直接写进本地目录」模拟客户端已经传到桶里。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.config import settings
from app.core.database import Base, configure_sqlite_connection, get_db
from app.core.security import create_access_token
from app.models.enums import PlatformRole
from app.models.release import AppReleaseArtifact
from app.models.report import AdminAuditLog
from app.models.user import User
from app.services import releases as release_service
from app.services import storage
from app.services import system_settings

SHA_A = hashlib.sha256(b"a" * 40).hexdigest()


class ReleaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        release_service.invalidate_cache()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{(Path(self.tmp.name) / 'r.db').as_posix()}")
        event.listen(self.engine.sync_engine, "connect", configure_sqlite_connection)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            root = User(username="root", hashed_password="x", platform_role=PlatformRole.ADMIN)
            carol = User(username="carol", hashed_password="x")
            db.add_all([root, carol])
            await db.commit()
            self.ids = {"root": root.id, "carol": carol.id}

        self.direct = True
        self.put_calls: list[tuple[str, int]] = []

        def fake_put(key: str, size: int, expires_in: int = 900) -> tuple[str, dict[str, str]]:
            self.put_calls.append((key, size))
            return f"https://bucket.test/{key}?sig=put", {"Content-Type": "application/octet-stream"}

        for patcher in (
            patch.object(settings, "upload_dir", str(Path(self.tmp.name) / "uploads")),
            patch.object(settings, "storage_backend", "local"),
            patch.object(release_service, "direct_upload_enabled", lambda: self.direct),
            patch.object(storage, "presign_put", fake_put),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        from app.api.v1.router import api_router

        api = FastAPI()
        api.include_router(api_router)

        async def isolated_db():
            async with self.sessions() as session:
                yield session

        api.dependency_overrides[get_db] = isolated_db
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.engine.dispose()
        release_service.invalidate_cache()

    def auth(self, who: str | None = "root") -> dict[str, str]:
        return {"Authorization": f"Bearer {create_access_token(self.ids[who])}"} if who else {}

    async def call(self, method: str, path: str, expected: int, *, who: str | None = "root", headers: dict[str, str] | None = None, **kwargs: Any) -> Any:
        response = await self.client.request(method, f"/api/v1{path}", headers={**self.auth(who), **(headers or {})}, **kwargs)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json() if response.content else None

    async def code(self, method: str, path: str, expected: int, **kwargs: Any) -> str | None:
        body = await self.call(method, path, expected, **kwargs)
        detail = body.get("detail")
        return detail.get("code") if isinstance(detail, dict) else detail

    def put_object(self, key: str, data: bytes) -> Path:
        target = Path(settings.upload_dir) / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    async def make(self, version: str, *, platform: str = "android", channel: str = "stable", build: int = 1, **extra: Any) -> dict[str, Any]:
        return await self.call("POST", "/admin/releases", 201, json={"platform": platform, "channel": channel, "version": version, "build": build, **extra})

    async def ship(self, version: str, data: bytes = b"a" * 40, **extra: Any) -> dict[str, Any]:
        """建草稿 → 直传 → complete → 发布。"""
        release = await self.make(version, **extra)
        ticket = await self.call(
            "POST", f"/admin/releases/{release['id']}/artifacts/uploads", 201,
            json={"file_name": f"KukeChat-{version}.apk", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        )
        self.put_object(self.put_calls[-1][0], data)
        await self.call("POST", f"/admin/releases/{release['id']}/artifacts/{ticket['artifact']['id']}/complete", 200)
        return await self.call("POST", f"/admin/releases/{release['id']}/publish", 200, json={})


class VersionTests(unittest.TestCase):
    def test_ordering(self) -> None:
        cmp = release_service.compare_versions
        self.assertEqual(cmp("1.10.0", "1.9.9"), 1)
        self.assertEqual(cmp("2.0.0-beta.1", "2.0.0"), -1)
        self.assertEqual(cmp("2.0.0-beta.2", "2.0.0-beta.10"), -1)
        self.assertEqual(cmp("v1.4", "1.4.0"), 0)
        self.assertEqual(cmp("2.0.0-dev", "2.0.0"), -1)
        self.assertEqual(cmp("2.0.0+5", "2.0.0"), 0)
        with self.assertRaises(Exception):
            release_service.version_key("abc")
        with self.assertRaises(Exception):
            release_service.normalize_version("1.2")
        self.assertEqual(release_service.normalize_version("v1.2.3"), "1.2.3")


class AdminReleaseTests(ReleaseCase):
    async def test_permissions(self) -> None:
        await self.call("GET", "/admin/releases", 401, who=None)
        await self.call("GET", "/admin/releases", 403, who="carol")
        self.assertEqual((await self.call("GET", "/admin/releases/config", 200))["direct_upload"], True)

    async def test_direct_upload_publish_and_latest(self) -> None:
        release = await self.make("2.0.1", build=201, changelog="2.0.1 更新内容包括：\n- 修好了", min_supported_version="1.3.0")
        self.assertFalse(release["published"])
        rid = release["id"]
        # 没有包不能发布
        self.assertEqual(await self.code("POST", f"/admin/releases/{rid}/publish", 409, json={}), "no_artifacts")
        ticket = await self.call("POST", f"/admin/releases/{rid}/artifacts/uploads", 201, json={"file_name": "../KukeChat.apk", "size": 40, "sha256": SHA_A.upper()})
        key, size = self.put_calls[-1]
        self.assertTrue(key.startswith("releases/android/stable/2.0.1/") and key.endswith("/KukeChat.apk") and size == 40)
        self.assertEqual((ticket["upload"]["method"], ticket["artifact"]["status"], ticket["artifact"]["sha256"]), ("PUT", "pending", SHA_A))
        aid = ticket["artifact"]["id"]
        # pending 的包不算
        self.assertEqual(await self.code("POST", f"/admin/releases/{rid}/publish", 409, json={}), "no_artifacts")
        self.assertEqual(await self.code("POST", f"/admin/releases/{rid}/artifacts/{aid}/complete", 409), "upload_incomplete")
        self.put_object(key, b"a" * 40)
        done = await self.call("POST", f"/admin/releases/{rid}/artifacts/{aid}/complete", 200)
        self.assertEqual(done["artifacts"][0]["status"], "ready")
        # 草稿对外不可见
        latest = await self.call("GET", "/releases/latest?platform=android&current=1.0.0", 200, who=None)
        self.assertEqual((latest["latest"], latest["update_available"]), (None, False))
        published = await self.call("POST", f"/admin/releases/{rid}/publish", 200, json={})
        self.assertTrue(published["published"] and published["published_at"])

        latest = await self.call("GET", "/releases/latest?platform=android&current=2.0.0", 200, who=None)
        self.assertEqual((latest["update_available"], latest["forced"], latest["latest"]["version"]), (True, False, "2.0.1"))
        artifact = latest["latest"]["artifact"]
        self.assertEqual((artifact["size"], artifact["sha256"], artifact["file_name"]), (40, SHA_A, "KukeChat.apk"))
        self.assertTrue(artifact["url"].endswith(key))
        self.assertNotIn("storage_key", artifact)
        forced = await self.call("GET", "/releases/latest?platform=android&current=1.2.9", 200, who=None)
        self.assertEqual((forced["update_available"], forced["forced"]), (True, True))
        same = await self.call("GET", "/releases/latest?platform=android&current=v2.0.1", 200, who=None)
        self.assertEqual((same["update_available"], same["forced"]), (False, False))
        self.assertEqual(await self.code("GET", "/releases/latest?platform=android&current=abc", 422, who=None), "invalid_version")
        self.assertEqual(await self.code("GET", "/releases/latest?platform=ios", 422, who=None), "invalid_platform")

        # 已发布：只能改更新日志和最低版本
        self.assertEqual(await self.code("PATCH", f"/admin/releases/{rid}", 409, json={"version": "2.0.2"}), "release_published")
        await self.call("PATCH", f"/admin/releases/{rid}", 200, json={"min_supported_version": None})
        self.assertFalse((await self.call("GET", "/releases/latest?platform=android&current=1.0.0", 200, who=None))["forced"])
        # 最后一个包删不掉、已发布删不掉
        self.assertEqual(await self.code("DELETE", f"/admin/releases/{rid}/artifacts/{aid}", 409), "last_artifact")
        self.assertEqual(await self.code("DELETE", f"/admin/releases/{rid}", 409), "release_published")
        await self.call("POST", f"/admin/releases/{rid}/unpublish", 200)
        self.assertIsNone((await self.call("GET", "/releases/latest?platform=android", 200, who=None))["latest"])
        await self.call("DELETE", f"/admin/releases/{rid}", 204)
        self.assertFalse((Path(settings.upload_dir) / key).exists())

        async with self.sessions() as db:
            actions = [row.action for row in (await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.id))).scalars()]
        for action in ("release.create", "release.artifact.upload_ticket", "release.artifact.add", "release.publish", "release.unpublish", "release.delete"):
            self.assertIn(action, actions)

    async def test_size_mismatch_deletes_object(self) -> None:
        rid = (await self.make("1.0.0"))["id"]
        ticket = await self.call("POST", f"/admin/releases/{rid}/artifacts/uploads", 201, json={"file_name": "a.apk", "size": 40, "sha256": SHA_A})
        path = self.put_object(self.put_calls[-1][0], b"short")
        self.assertEqual(await self.code("POST", f"/admin/releases/{rid}/artifacts/{ticket['artifact']['id']}/complete", 422), "size_mismatch")
        self.assertFalse(path.exists())
        self.assertEqual((await self.call("GET", f"/admin/releases/{rid}", 200))["artifacts"], [])

    async def test_validation_and_duplicates(self) -> None:
        self.assertEqual(await self.code("POST", "/admin/releases", 422, json={"platform": "android", "version": "1.2"}), "invalid_version")
        self.assertEqual(await self.code("POST", "/admin/releases", 422, json={"platform": "ios", "version": "1.2.0"}), "invalid_platform")
        rid = (await self.make("1.2.0"))["id"]
        self.assertEqual(await self.code("POST", "/admin/releases", 409, json={"platform": "android", "version": "1.2.0"}), "release_exists")
        await self.make("1.2.0", channel="beta")  # 不同渠道可以同号
        upload = {"file_name": "a.apk", "size": 40}
        self.assertEqual(await self.code("POST", f"/admin/releases/{rid}/artifacts/uploads", 422, json={**upload, "sha256": "xyz"}), "invalid_sha256")
        self.assertEqual(
            await self.code("POST", f"/admin/releases/{rid}/artifacts/uploads", 413, json={**upload, "size": settings.release_max_artifact_bytes + 1, "sha256": SHA_A}),
            "file_too_large",
        )
        # pending 的同名单被新的替换，ready 的同名 409
        await self.call("POST", f"/admin/releases/{rid}/artifacts/uploads", 201, json={**upload, "sha256": SHA_A})
        ticket = await self.call("POST", f"/admin/releases/{rid}/artifacts/uploads", 201, json={**upload, "sha256": SHA_A})
        self.assertEqual(len((await self.call("GET", f"/admin/releases/{rid}", 200))["artifacts"]), 1)
        self.put_object(self.put_calls[-1][0], b"a" * 40)
        await self.call("POST", f"/admin/releases/{rid}/artifacts/{ticket['artifact']['id']}/complete", 200)
        self.assertEqual(await self.code("POST", f"/admin/releases/{rid}/artifacts/uploads", 409, json={**upload, "sha256": SHA_A}), "artifact_exists")
        self.assertEqual(await self.code("POST", f"/admin/releases/{rid}/artifacts/external", 422, json={"url": "ftp://x/a.apk"}), "invalid_url")

    async def test_external_url_and_multipart_fallback(self) -> None:
        self.direct = False
        rid = (await self.make("3.0.0", platform="windows"))["id"]
        self.assertEqual(
            await self.code("POST", f"/admin/releases/{rid}/artifacts/uploads", 409, json={"file_name": "a.exe", "size": 40, "sha256": SHA_A}),
            "direct_upload_unavailable",
        )
        data = b"MZ" + b"x" * 100
        self.assertEqual(
            await self.code("POST", f"/admin/releases/{rid}/artifacts", 422, files={"file": ("setup.exe", data)}, data={"sha256": SHA_A}), "sha256_mismatch"
        )
        uploaded = await self.call("POST", f"/admin/releases/{rid}/artifacts", 201, files={"file": ("setup.exe", data)}, data={"arch": "x64"})
        item = uploaded["artifacts"][0]
        self.assertEqual((item["size"], item["sha256"], item["arch"], item["status"]), (len(data), hashlib.sha256(data).hexdigest(), "x64", "ready"))
        self.assertEqual((Path(settings.upload_dir) / item["storage_key"]).read_bytes(), data)
        external = await self.call(
            "POST", f"/admin/releases/{rid}/artifacts/external", 201, json={"url": "https://mirror.example/KukeChat_arm64-setup.exe?x=1", "arch": "arm64"}
        )
        self.assertEqual(external["artifacts"][1]["file_name"], "KukeChat_arm64-setup.exe")
        await self.call("POST", f"/admin/releases/{rid}/publish", 200, json={})
        arm = await self.call("GET", "/releases/latest?platform=windows&arch=arm64&current=2.0.0", 200, who=None)
        self.assertEqual(arm["latest"]["artifact"]["url"], "https://mirror.example/KukeChat_arm64-setup.exe?x=1")
        x64 = await self.call("GET", "/releases/latest?platform=windows&arch=x64", 200, who=None)
        self.assertEqual(x64["latest"]["artifact"]["file_name"], "setup.exe")
        self.assertEqual(len(x64["latest"]["artifacts"]), 2)

        # 旧的 /desktop-update.json 元数据被发版服务覆盖，没有 android 版本的继续用手填值
        async with self.sessions() as db:
            await system_settings.update_download_settings(db, self.ids["root"], "https://old/pc.exe", "https://old/a.apk", None, "1.0.0", "1.0.0")
            meta = await system_settings.export_public_download_metadata(db)
            manual = await system_settings.get_download_settings(db)
        self.assertEqual((meta["pc_version"], meta["android_url"], meta["mobile_version"]), ("3.0.0", "https://old/a.apk", "1.0.0"))
        self.assertTrue(meta["pc_url"].endswith("/setup.exe"))
        self.assertEqual(manual.pc_url, "https://old/pc.exe")

    async def test_latest_is_by_version_and_beta_sees_stable(self) -> None:
        await self.ship("2.0.0", build=200)
        # 发布更旧的版本：要 force
        old = await self.make("1.9.0", build=190)
        ticket = await self.call("POST", f"/admin/releases/{old['id']}/artifacts/external", 201, json={"url": "https://x/a.apk"})
        self.assertTrue(ticket["artifacts"])
        self.assertEqual(await self.code("POST", f"/admin/releases/{old['id']}/publish", 409, json={}), "version_not_newer")
        await self.call("POST", f"/admin/releases/{old['id']}/publish", 200, json={"force": True})
        self.assertEqual((await self.call("GET", "/releases/latest?platform=android", 200, who=None))["latest"]["version"], "2.0.0")
        await self.ship("2.1.0-beta.1", channel="beta", build=205)
        self.assertEqual((await self.call("GET", "/releases/latest?platform=android", 200, who=None))["latest"]["version"], "2.0.0")
        beta = await self.call("GET", "/releases/latest?platform=android&channel=beta&current=2.0.0", 200, who=None)
        self.assertEqual((beta["latest"]["version"], beta["update_available"]), ("2.1.0-beta.1", True))
        history = await self.call("GET", "/releases?platform=android", 200, who=None)
        self.assertEqual(([item["version"] for item in history["items"]], history["total"]), (["2.0.0", "1.9.0"], 2))
        beta_history = await self.call("GET", "/releases?platform=android&channel=beta&limit=1", 200, who=None)
        self.assertEqual(([item["version"] for item in beta_history["items"]], beta_history["total"]), (["2.1.0-beta.1"], 3))

    async def test_release_tokens(self) -> None:
        created = await self.call("POST", "/admin/releases/tokens", 201, json={"label": "我的电脑"})
        token = created["token"]
        self.assertTrue(token.startswith("kcr_") and created["item"]["prefix"] == token[:10])
        header = {"X-Kuke-Release-Token": token}
        # 令牌能发版，不能管令牌
        release = await self.call("POST", "/admin/releases", 201, who=None, headers=header, json={"platform": "android", "version": "1.0.0"})
        self.assertEqual(await self.code("GET", "/admin/releases/tokens", 403, who=None, headers=header), "token_not_allowed")
        async with self.sessions() as db:
            audit = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "release.create"))).scalar_one()
            self.assertEqual(audit.actor_id, self.ids["root"])
            self.assertIn('"via_token"', audit.details)
        listed = await self.call("GET", "/admin/releases/tokens", 200)
        self.assertTrue(listed["items"][0]["last_used_at"])
        self.assertNotIn("token", listed["items"][0])
        self.assertEqual(await self.code("GET", "/admin/releases", 401, who=None, headers={"X-Kuke-Release-Token": "kcr_nope"}), "invalid_release_token")
        # 签发人不再是管理员 → 失效
        async with self.sessions() as db:
            root = await db.get(User, self.ids["root"])
            root.platform_role = PlatformRole.USER
            await db.commit()
        self.assertEqual(await self.code("GET", f"/admin/releases/{release['id']}", 401, who=None, headers=header), "invalid_release_token")
        async with self.sessions() as db:
            root = await db.get(User, self.ids["root"])
            root.platform_role = PlatformRole.ADMIN
            await db.commit()
        await self.call("DELETE", f"/admin/releases/tokens/{created['item']['id']}", 204)
        self.assertEqual(await self.code("GET", "/admin/releases", 401, who=None, headers=header), "invalid_release_token")

    async def test_stale_pending_uploads_are_swept(self) -> None:
        rid = (await self.make("1.0.0"))["id"]
        await self.call("POST", f"/admin/releases/{rid}/artifacts/uploads", 201, json={"file_name": "a.apk", "size": 40, "sha256": SHA_A})
        path = self.put_object(self.put_calls[-1][0], b"a" * 40)
        async with self.sessions() as db:
            row = (await db.execute(select(AppReleaseArtifact))).scalar_one()
            row.created_at = row.created_at - release_service.PENDING_TTL * 2
            await db.commit()
        self.assertEqual((await self.call("GET", f"/admin/releases/{rid}", 200))["artifacts"], [])
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
