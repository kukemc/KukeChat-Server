"""聊天图片直传（docs/chat-features.md「图片直传」）。开关和预签名打桩，对象用本地存储模拟「客户端已经传到桶里」。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI
from PIL import Image
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.config import settings
from app.core.database import Base, configure_sqlite_connection, get_db
from app.core.dependencies import get_current_user
from app.models.image_upload import ImageUpload
from app.models.user import User
from app.services import image_uploads
from app.services import storage


def jpeg(size: tuple[int, int] = (64, 48), *, exif: bool = False, color=(200, 40, 40)) -> bytes:
    out = BytesIO()
    image = Image.new("RGB", size, color)
    if exif:
        data = Image.Exif()
        data[0x010F] = "PhoneMaker"  # Make
        data[0x8825] = {2: (30.0, 15.0, 0.0)}  # GPSInfo
        image.save(out, format="JPEG", quality=80, exif=data.tobytes())
    else:
        image.save(out, format="JPEG", quality=80)
    return out.getvalue()


def gif() -> bytes:
    out = BytesIO()
    frames = [Image.new("P", (40, 30), i * 40) for i in range(3)]
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
    return out.getvalue()


def png(size: tuple[int, int] = (32, 32)) -> bytes:
    out = BytesIO()
    Image.new("RGBA", size, (0, 0, 0, 0)).save(out, format="PNG")
    return out.getvalue()


class DirectImageUploadCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{(Path(self.tmp.name) / 'direct.db').as_posix()}")
        event.listen(self.engine.sync_engine, "connect", configure_sqlite_connection)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob")]
            db.add_all(users)
            await db.commit()
            self.ids = {user.username: user.id for user in users}

        self.presigned: list[tuple[str, int, str]] = []

        def fake_put(key: str, size: int, expires_in: int = 900, content_type: str = storage.OCTET_STREAM):
            self.presigned.append((key, size, content_type))
            return f"https://bucket.test/{key}?sig=put", {"Content-Type": content_type, "Cache-Control": "public, max-age=3600"}

        self.enabled = True
        for patcher in (
            patch.object(settings, "upload_dir", str(Path(self.tmp.name) / "uploads")),
            patch.object(settings, "storage_backend", "local"),
            patch.object(storage, "direct_image_upload_enabled", lambda: self.enabled),
            patch.object(storage, "presign_put", fake_put),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        from app.api.v1.router import api_router

        self.acting = "alice"
        api = FastAPI()
        api.include_router(api_router)

        async def isolated_db():
            async with self.sessions() as session:
                yield session

        async def acting_user(db: Annotated[AsyncSession, Depends(get_db)]) -> User:
            return await db.get(User, self.ids[self.acting])

        api.dependency_overrides[get_db] = isolated_db
        api.dependency_overrides[get_current_user] = acting_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.engine.dispose()

    # ------------------------------------------------------------ 工具

    async def call(self, method: str, path: str, expected: int, *, as_: str = "alice", **kwargs: Any) -> Any:
        self.acting = as_
        response = await self.client.request(method, f"/api/v1{path}", **kwargs)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    async def open(self, image: bytes, thumb: bytes, content_type: str = "image/jpeg", **kwargs: Any) -> dict:
        return await self.call(
            "POST", "/uploads/message-image/direct", 201,
            json={"content_type": content_type, "size": len(image), "thumbnail_size": len(thumb)}, **kwargs,
        )

    def stored(self, key: str) -> Path:
        return Path(settings.upload_dir) / key

    def put(self, key: str, data: bytes) -> None:
        """模拟客户端 PUT 到预签名 URL。"""
        target = self.stored(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def keys(self) -> tuple[str, str]:
        (image_key, _, _), (thumb_key, _, _) = self.presigned[-2:]
        return image_key, thumb_key

    async def pending_count(self) -> int:
        async with self.sessions() as db:
            return len((await db.execute(select(ImageUpload))).scalars().all())

    # ------------------------------------------------------------ 用例

    # ------------------------------------------------------------ 中转直传（/message-image/relay）

    async def relay(self, image: bytes, thumb: bytes, content_type: str = "image/jpeg", expected: int = 201) -> dict:
        return await self.call(
            "POST", "/uploads/message-image/relay", expected,
            files={"image": ("i", image, content_type), "thumbnail": ("t", thumb, "image/jpeg")},
        )

    def stored_bytes(self, url: str) -> bytes:
        key = storage.storage_key_from_url(url)
        self.assertIsNotNone(key, url)
        return self.stored(key).read_bytes()

    async def test_relay_clean_jpeg_stored_as_is(self) -> None:
        image, thumb = jpeg((800, 600)), jpeg((200, 150))
        body = await self.relay(image, thumb)
        self.assertEqual(body["content_type"], "image/jpeg")
        self.assertEqual(self.stored_bytes(body["url"]), image, "合格的主图原样写进去，不重新编码")
        self.assertEqual(self.stored_bytes(body["thumbnail_url"]), thumb)
        self.assertEqual(self.presigned, [], "中转不签预签名 URL")
        self.assertEqual(await self.pending_count(), 0, "中转不开上传单")

    async def test_relay_strips_exif(self) -> None:
        body = await self.relay(jpeg((300, 200), exif=True), jpeg((100, 66)))
        with Image.open(BytesIO(self.stored_bytes(body["url"]))) as stored:
            self.assertNotIn("exif", stored.info)

    async def test_relay_gif_kept(self) -> None:
        image = gif()
        body = await self.relay(image, jpeg((40, 30)), "image/gif")
        self.assertEqual(body["content_type"], "image/gif")
        self.assertEqual(self.stored_bytes(body["url"]), image)

    async def test_relay_wrong_type_is_reprocessed(self) -> None:
        # 声称是 JPEG 实际是 PNG：按普通上传处理，转成 JPEG
        body = await self.relay(png((64, 64)), jpeg((32, 32)))
        self.assertEqual(body["content_type"], "image/jpeg")
        with Image.open(BytesIO(self.stored_bytes(body["url"]))) as stored:
            self.assertEqual(stored.format, "JPEG")

    async def test_relay_truncated_jpeg_is_not_stored_as_is(self) -> None:
        full = jpeg((400, 300), color=(10, 200, 90))
        half = full[: len(full) // 2]
        self.acting = "alice"
        response = await self.client.post(
            "/api/v1/uploads/message-image/relay",
            files={"image": ("i", half, "image/jpeg"), "thumbnail": ("t", jpeg((100, 75)), "image/jpeg")},
        )
        self.assertIn(response.status_code, (201, 400), response.text)
        if response.status_code == 201:
            self.assertNotEqual(self.stored_bytes(response.json()["url"]), half, "截断的图不能原样存")

    async def test_relay_rejects_non_image(self) -> None:
        await self.relay(b"not an image at all", jpeg((10, 10)), expected=400)

    async def test_clean_jpeg_is_used_as_is(self) -> None:
        image, thumb = jpeg((1200, 900)), jpeg((360, 270))
        ticket = await self.open(image, thumb)
        image_key, thumb_key = self.keys()
        self.assertRegex(image_key, r"^messages/[0-9a-f]{32}\.jpg$")
        self.assertEqual(thumb_key, storage.thumbnail_key_for_key(image_key))
        self.assertEqual(self.presigned[-2][1:], (len(image), "image/jpeg"))
        self.assertEqual(self.presigned[-1][1:], (len(thumb), "image/jpeg"))
        self.assertEqual(ticket["image"]["url"], f"https://bucket.test/{image_key}?sig=put")
        self.assertEqual(ticket["image"]["headers"]["Content-Type"], "image/jpeg")
        self.assertEqual(ticket["thumbnail"]["method"], "PUT")
        self.put(image_key, image)
        self.put(thumb_key, thumb)
        with patch.object(storage, "store_processed_image") as reprocess:
            done = await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 201)
        reprocess.assert_not_called()
        self.assertTrue(done["url"].endswith(image_key))
        self.assertTrue(done["thumbnail_url"].endswith(thumb_key))
        self.assertEqual((done["content_type"], done["filename"]), ("image/jpeg", image_key.rsplit("/", 1)[-1]))
        # 原样保留，后端没有重新编码
        self.assertEqual(self.stored(image_key).read_bytes(), image)
        self.assertEqual(self.stored(thumb_key).read_bytes(), thumb)
        self.assertEqual(await self.pending_count(), 0)
        # 上传单用完就没了
        again = await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 404)
        self.assertEqual(again["detail"]["code"], "upload_not_found")

    async def test_exif_location_is_stripped_by_server(self) -> None:
        image, thumb = jpeg((800, 600), exif=True), jpeg((360, 270))
        self.assertIn("exif", Image.open(BytesIO(image)).info)
        ticket = await self.open(image, thumb)
        image_key, thumb_key = self.keys()
        self.put(image_key, image)
        self.put(thumb_key, thumb)
        done = await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 201)
        self.assertTrue(done["url"].endswith(image_key))
        rewritten = self.stored(image_key).read_bytes()
        self.assertNotEqual(rewritten, image)
        with Image.open(BytesIO(rewritten)) as result:
            self.assertEqual(result.format, "JPEG")
            self.assertNotIn("exif", result.info)
            self.assertEqual(result.size, (800, 600))

    async def test_oversized_dimensions_are_shrunk(self) -> None:
        image, thumb = jpeg((3000, 1000)), jpeg((360, 120))
        ticket = await self.open(image, thumb)
        image_key, thumb_key = self.keys()
        self.put(image_key, image)
        self.put(thumb_key, thumb)
        await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 201)
        with Image.open(self.stored(image_key)) as result:
            self.assertLessEqual(max(result.size), settings.image_max_dimension)

    async def test_gif_with_bad_thumbnail_keeps_the_gif(self) -> None:
        animation, bad_thumb = gif(), png()
        ticket = await self.open(animation, bad_thumb, "image/gif")
        image_key, thumb_key = self.keys()
        self.assertTrue(image_key.endswith(".gif"))
        self.put(image_key, animation)
        self.put(thumb_key, bad_thumb)
        done = await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 201)
        self.assertEqual(done["content_type"], "image/gif")
        self.assertEqual(self.stored(image_key).read_bytes(), animation)
        with Image.open(self.stored(thumb_key)) as result:
            self.assertEqual(result.format, "JPEG")

    async def test_size_mismatch_deletes_objects(self) -> None:
        image, thumb = jpeg(), jpeg((30, 20))
        ticket = await self.open(image, thumb)
        image_key, thumb_key = self.keys()
        self.put(image_key, image + b"extra")
        self.put(thumb_key, thumb)
        failed = await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 422)
        self.assertEqual(failed["detail"]["code"], "upload_size_mismatch")
        self.assertFalse(self.stored(image_key).exists())
        self.assertFalse(self.stored(thumb_key).exists())
        self.assertEqual(await self.pending_count(), 0)

    async def test_not_an_image_is_rejected(self) -> None:
        junk, thumb = b"<html>not an image</html>", jpeg((30, 20))
        ticket = await self.open(junk, thumb)
        image_key, thumb_key = self.keys()
        self.put(image_key, junk)
        self.put(thumb_key, thumb)
        await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 400)
        self.assertFalse(self.stored(image_key).exists())
        self.assertFalse(self.stored(thumb_key).exists())

    async def test_incomplete_upload_keeps_ticket(self) -> None:
        image, thumb = jpeg(), jpeg((30, 20))
        ticket = await self.open(image, thumb)
        image_key, thumb_key = self.keys()
        self.put(image_key, image)
        missing = await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 409)
        self.assertEqual(missing["detail"]["code"], "upload_incomplete")
        self.put(thumb_key, thumb)
        await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 201)

    async def test_only_the_uploader_can_complete(self) -> None:
        image, thumb = jpeg(), jpeg((30, 20))
        ticket = await self.open(image, thumb)
        image_key, thumb_key = self.keys()
        self.put(image_key, image)
        self.put(thumb_key, thumb)
        await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 404, as_="bob")
        await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 201)

    async def test_open_checks(self) -> None:
        image = jpeg()
        bad_type = await self.call("POST", "/uploads/message-image/direct", 422, json={"content_type": "image/png", "size": 10, "thumbnail_size": 10})
        self.assertEqual(bad_type["detail"]["code"], "unsupported_image_type")
        too_big = await self.call(
            "POST", "/uploads/message-image/direct", 413,
            json={"content_type": "image/jpeg", "size": storage.image_upload_limits("messages").max_bytes + 1, "thumbnail_size": 10},
        )
        self.assertEqual(too_big["detail"]["code"], "image_too_large")
        big_thumb = await self.call(
            "POST", "/uploads/message-image/direct", 413,
            json={"content_type": "image/jpeg", "size": 10, "thumbnail_size": storage.DIRECT_THUMBNAIL_MAX_BYTES + 1},
        )
        self.assertEqual(big_thumb["detail"]["code"], "thumbnail_too_large")
        empty = await self.call("POST", "/uploads/message-image/direct", 400, json={"content_type": "image/jpeg", "size": 0, "thumbnail_size": 10})
        self.assertEqual(empty["detail"]["code"], "empty_file")
        self.enabled = False
        off = await self.call("POST", "/uploads/message-image/direct", 409, json={"content_type": "image/jpeg", "size": len(image), "thumbnail_size": 10})
        self.assertEqual(off["detail"]["code"], "direct_upload_unavailable")
        self.assertEqual(await self.pending_count(), 0)

    async def test_pending_limit(self) -> None:
        with patch.object(image_uploads, "MAX_PENDING_PER_USER", 2):
            await self.open(jpeg(), jpeg((30, 20)))
            await self.open(jpeg(), jpeg((30, 20)))
            limited = await self.call(
                "POST", "/uploads/message-image/direct", 429, json={"content_type": "image/jpeg", "size": 10, "thumbnail_size": 10},
            )
            self.assertEqual(limited["detail"]["code"], "too_many_pending_uploads")
            # 别人不受影响
            await self.open(jpeg(), jpeg((30, 20)), as_="bob")

    async def test_expired_ticket_and_sweep(self) -> None:
        image, thumb = jpeg(), jpeg((30, 20))
        ticket = await self.open(image, thumb)
        image_key, thumb_key = self.keys()
        self.put(image_key, image)
        self.put(thumb_key, thumb)
        later = datetime.now(UTC) + timedelta(seconds=image_uploads.UPLOAD_TTL_SECONDS + 1)
        with patch.object(image_uploads, "_now", lambda: later):
            expired = await self.call("POST", f"/uploads/message-image/direct/{ticket['upload_id']}/complete", 410)
            self.assertEqual(expired["detail"]["code"], "upload_expired")
            async with self.sessions() as db:
                self.assertEqual(await image_uploads.sweep(db), 1)
        self.assertFalse(self.stored(image_key).exists())
        self.assertFalse(self.stored(thumb_key).exists())
        self.assertEqual(await self.pending_count(), 0)


class PresignPutHeadersCase(unittest.TestCase):
    """真的签一次（本地算签名，不发请求）：聊天图的 key 要把 Content-Type 和 Cache-Control 都签进去，群文件照旧。"""

    def setUp(self) -> None:
        for patcher in (
            patch.object(settings, "storage_backend", "s3"),
            patch.object(settings, "s3_endpoint_url", "https://s3.example.test"),
            patch.object(settings, "s3_bucket", "main"),
            patch.object(settings, "s3_private_bucket", "private"),
            patch.object(settings, "s3_access_key_id", "AKIDEXAMPLE"),
            patch.object(settings, "s3_secret_access_key", "secret"),
            patch.object(settings, "s3_region", "us-east-1"),
            patch.object(storage, "_S3_CLIENT", None),
            patch.object(storage, "_S3_PRESIGN_CLIENT", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_message_image_headers(self) -> None:
        url, headers = storage.presign_put("messages/abc.jpg", 123, 900, "image/jpeg")
        self.assertEqual(headers, {"Content-Type": "image/jpeg", "Cache-Control": storage._PUBLIC_CACHE_CONTROL})
        self.assertIn("/main/messages/abc.jpg", url)
        self.assertIn("cache-control", url.lower())

    def test_group_file_headers_unchanged(self) -> None:
        url, headers = storage.presign_put("group-files/1/abc", 5)
        self.assertEqual(headers, {"Content-Type": storage.OCTET_STREAM})
        self.assertIn("/private/group-files/1/abc", url)

    def test_enabled_only_with_s3(self) -> None:
        self.assertTrue(storage.direct_image_upload_enabled())
        with patch.object(settings, "direct_image_uploads", False):
            self.assertFalse(storage.direct_image_upload_enabled())
        with patch.object(settings, "storage_backend", "local"):
            self.assertFalse(storage.direct_image_upload_enabled())


if __name__ == "__main__":
    unittest.main()
