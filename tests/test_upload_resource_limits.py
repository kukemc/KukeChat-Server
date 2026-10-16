from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from PIL import Image

from app.services import legacy_uploads, storage


def image_bytes(format: str, size: tuple[int, int] = (500, 500)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "#579dd1").save(output, format=format)
    return output.getvalue()


def noisy_image_bytes(format: str, size: tuple[int, int]) -> bytes:
    """随机噪声 + 网格线：只有这种内容在「不缩图」时才压不到 3MB，
    用来验证公告长图是真的按原尺寸存下来的（默认位图字体画不了中文，所以不画字）。"""
    import random

    from PIL import ImageDraw

    width, height = size
    generator = random.Random(20240517)
    # 直接一次性生成整块噪声，逐像素 Python 循环在 900 万像素上要十几秒
    image = Image.frombytes("RGB", size, generator.randbytes(width * height * 3))
    draw = ImageDraw.Draw(image)
    for index in range(0, height, 60):
        draw.line((0, index, width, index), fill=(255, 0, 0), width=2)
    output = BytesIO()
    image.save(output, format=format)
    return output.getvalue()


class UploadResourceTests(unittest.TestCase):
    def test_small_compressed_bomb_rejected_before_decode(self) -> None:
        # A header alone is enough to reject an oversized raster.
        data = image_bytes("PNG", (4000, 4000))
        self.assertLess(len(data), storage.MAX_IMAGE_UPLOAD_BYTES)
        with self.assertRaises(HTTPException) as context:
            storage.process_image_upload(data, "messages", "bomb")
        self.assertEqual(context.exception.status_code, 413)

    def test_large_legacy_source_is_bounded(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "old.png"
            path.write_bytes(b"x" * (storage.MAX_LEGACY_IMAGE_BYTES + 8))
            with self.assertRaises(ValueError):
                legacy_uploads._read_legacy_image(path)

    def test_formats_gif_frames_and_smaller_thumbnails(self) -> None:
        for format in ("JPEG", "PNG", "WEBP"):
            with self.subTest(format=format):
                processed = storage.process_image_upload(image_bytes(format), "messages", "ok")
                self.assertEqual(processed.content_type, "image/jpeg")
                self.assertTrue(processed.thumbnail_data.startswith(b"\xff\xd8"))
                self.assertLess(len(processed.thumbnail_data), len(processed.data))
        output = BytesIO()
        Image.new("RGB", (32, 32), "red").save(
            output, format="GIF", save_all=True,
            append_images=[Image.new("RGB", (32, 32), "blue")], duration=100, loop=0,
        )
        animated = output.getvalue()
        processed = storage.process_image_upload(animated, "messages", "animated")
        self.assertEqual(processed.content_type, "image/gif")
        self.assertEqual(processed.data, animated)
        with Image.open(BytesIO(processed.data)) as gif:
            self.assertEqual(gif.n_frames, 2)

    def test_local_sticker_source_is_bounded_before_read_all(self) -> None:
        with TemporaryDirectory() as root, patch.object(storage.settings, "upload_dir", root), patch.object(storage.settings, "storage_backend", "local"):
            path = Path(root) / "messages" / "old.png"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x" * (storage.MAX_LEGACY_IMAGE_BYTES + 2))
            with self.assertRaises(HTTPException) as context:
                storage.get_object("messages/old.png", storage.MAX_LEGACY_IMAGE_BYTES)
            self.assertEqual(context.exception.status_code, 413)

    def test_legacy_cache_has_fixed_number_of_locks(self) -> None:
        self.assertEqual(len(legacy_uploads._MIGRATION_LOCKS), 64)
        self.assertIs(legacy_uploads._migration_lock("same"), legacy_uploads._migration_lock("same"))

    def test_updated_key_cache_is_bounded_and_expires(self) -> None:
        legacy_uploads._UPDATED_KEYS.clear()
        try:
            with patch.object(legacy_uploads, "_UPDATED_CACHE_LIMIT", 3):
                for i in range(5):
                    legacy_uploads._mark_updated(str(i))
                self.assertEqual(len(legacy_uploads._UPDATED_KEYS), 3)
                self.assertFalse(legacy_uploads._recently_updated("0"))
                self.assertTrue(legacy_uploads._recently_updated("4"))
                with patch.object(legacy_uploads, "monotonic", return_value=float("inf")):
                    self.assertFalse(legacy_uploads._recently_updated("4"))
        finally:
            legacy_uploads._UPDATED_KEYS.clear()


class LegacyRepeatedRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_legacy_url_scans_references_only_once(self) -> None:
        from unittest.mock import Mock

        source = Path("messages/old.png")
        db = Mock()
        db.commit = AsyncMock()
        update = AsyncMock()
        legacy_uploads._UPDATED_KEYS.clear()
        try:
            with (patch.object(legacy_uploads, "_legacy_request_source", return_value=("messages", str(source), False)),
                  patch.object(legacy_uploads, "local_path_for_key", return_value=source),
                  patch.object(legacy_uploads, "object_exists", return_value=True),
                  patch.object(legacy_uploads, "_update_message_references", update)):
                first = await legacy_uploads.migrate_legacy_upload_response(str(source), db, None)
                second = await legacy_uploads.migrate_legacy_upload_response(str(source), db, None)
            self.assertEqual(first.status_code, 302)
            self.assertEqual(second.status_code, 302)
            update.assert_awaited_once()
            db.commit.assert_awaited_once()
        finally:
            legacy_uploads._UPDATED_KEYS.clear()


class AnnouncementImageLimitTests(unittest.TestCase):
    """公告长图单独放宽；其它所有上传路径的限制逐项不变。"""

    def test_only_announcements_get_wider_limits(self) -> None:
        wide = storage.image_upload_limits("announcements")
        self.assertEqual(wide.max_bytes, storage.settings.image_announcement_max_bytes)
        self.assertEqual(wide.max_dimension, storage.settings.image_announcement_max_dimension)
        self.assertEqual(wide.max_pixels, storage.settings.image_announcement_max_pixels)
        # 目录名前后加斜杠也应该认
        self.assertEqual(storage.image_upload_limits("/announcements/"), wide)

        for folder in ("avatars", "messages", "posts", "teamup", "profile-covers", "stickers", "nope"):
            with self.subTest(folder=folder):
                self.assertEqual(storage.image_upload_limits(folder), storage.DEFAULT_IMAGE_UPLOAD_LIMITS)
        self.assertEqual(storage.DEFAULT_IMAGE_UPLOAD_LIMITS.max_bytes, storage.MAX_IMAGE_UPLOAD_BYTES)
        self.assertEqual(storage.DEFAULT_IMAGE_UPLOAD_LIMITS.max_pixels, storage.MAX_IMAGE_PIXELS)
        self.assertIsNone(storage.DEFAULT_IMAGE_UPLOAD_LIMITS.max_dimension)
        self.assertIsNone(storage.DEFAULT_IMAGE_UPLOAD_LIMITS.quality)

    def test_announcement_long_image_upload_keeps_full_height_and_bytes(self) -> None:
        # 1240x7526（9.33 MP，和真实公告长图同量级）噪声图远超 3MB / 2048 高。
        source = noisy_image_bytes("JPEG", (1240, 7526))
        self.assertGreater(len(source), storage.MAX_IMAGE_UPLOAD_BYTES)
        self.assertLessEqual(len(source), storage.settings.image_announcement_max_bytes)

        processed = storage.process_image_upload(source, "announcements", "long")
        self.assertEqual(processed.key, "announcements/long.jpg")
        self.assertEqual(processed.content_type, "image/jpeg")
        with Image.open(BytesIO(processed.data)) as stored:
            # 不缩：高度必须还是 7526，不再是 2048
            self.assertEqual(stored.size, (1240, 7526))
        # 缩略图仍然是小图，列表/骨架用的还是它
        with Image.open(BytesIO(processed.thumbnail_data)) as thumb:
            self.assertLessEqual(max(thumb.size), storage.settings.image_thumbnail_max_dimension)

    def test_announcement_pixel_cap_is_still_enforced(self) -> None:
        # 放宽到 4000 万像素，但 7000x7000 = 4900 万仍然要被挡在解码前。
        data = image_bytes("PNG", (7000, 7000))
        self.assertLess(len(data), storage.settings.image_announcement_max_bytes)
        with self.assertRaises(HTTPException) as context:
            storage.process_image_upload(data, "announcements", "too-many-pixels")
        self.assertEqual(context.exception.status_code, 413)

    def test_announcement_byte_cap_is_still_enforced(self) -> None:
        limit = storage.settings.image_announcement_max_bytes
        with self.assertRaises(HTTPException) as context:
            storage.process_image_upload(b"x" * (limit + 1), "announcements", "too-big")
        self.assertEqual(context.exception.status_code, 413)

    def test_other_folders_still_reject_the_announcement_long_image(self) -> None:
        source = noisy_image_bytes("JPEG", (1240, 7526))
        self.assertGreater(len(source), storage.MAX_IMAGE_UPLOAD_BYTES)
        for folder in ("messages", "teamup", "posts", "avatars", "profile-covers", "stickers"):
            with self.subTest(folder=folder):
                with self.assertRaises(HTTPException) as context:
                    storage.process_image_upload(source, folder, "long")
                self.assertEqual(context.exception.status_code, 413)
                self.assertIn("3MB", str(context.exception.detail))

    def test_other_folders_still_reject_oversized_pixels_and_still_shrink(self) -> None:
        # 12MP 像素上限没被动过：4000x4000 = 1600 万按老规矩拒绝。
        bomb = image_bytes("PNG", (4000, 4000))
        for folder in ("messages", "teamup", "posts", "avatars"):
            with self.subTest(folder=folder):
                with self.assertRaises(HTTPException) as context:
                    storage.process_image_upload(bomb, folder, "bomb")
                self.assertEqual(context.exception.status_code, 413)
        # 缩图行为也没被动过：超过 2048 的仍然被压到 2048。
        wide = image_bytes("PNG", (3000, 1500))
        processed = storage.process_image_upload(wide, "teamup", "wide")
        with Image.open(BytesIO(processed.data)) as stored:
            self.assertEqual(stored.size, (2048, 1024))

    def test_explicit_limits_override_folder_default(self) -> None:
        # 显式传一组更紧的限制时以传入的为准（放宽是「按目录」而不是「按函数」）。
        tight = storage.ImageUploadLimits(max_bytes=64 * 1024, max_dimension=None, max_pixels=storage.MAX_IMAGE_PIXELS, quality=None)
        source = noisy_image_bytes("JPEG", (1240, 7526))
        self.assertGreater(len(source), tight.max_bytes)
        with self.assertRaises(HTTPException) as context:
            storage.process_image_upload(source, "announcements", "tight", tight)
        self.assertEqual(context.exception.status_code, 413)
        # 同一张图用公告目录的默认限制能过
        self.assertEqual(storage.process_image_upload(source, "announcements", "loose").key, "announcements/loose.jpg")


class AnnouncementUploadEndpointTests(unittest.IsolatedAsyncioTestCase):
    """走真实的 save_image_upload（本地存储），验证公告路径与其它路径的真实差异。

    这里不经过鉴权/审核中间件，只把一个带 uploads 路由的最小 Starlette 应用装进
    request.scope，让 public_url_for_key 的 url_for 能解析出真实 URL。
    """

    async def _upload(self, folder: str, content: bytes):
        from starlette.applications import Starlette
        from starlette.datastructures import Headers, UploadFile as StarletteUploadFile
        from starlette.requests import Request
        from starlette.routing import Mount

        from app.api.v1 import uploads as uploads_module

        file = StarletteUploadFile(
            filename="long.jpg",
            file=BytesIO(content),
            headers=Headers({"content-type": "image/jpeg"}),
        )
        # 装一个最小应用，只为让 url_for("uploads", ...) 能解析
        app = Starlette(routes=[Mount("/uploads", app=lambda scope, receive, send: None, name="uploads")])
        scope = {
            "type": "http",
            "headers": [],
            "method": "POST",
            "path": "/api/v1/uploads/x",
            "root_path": "",
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "app": app,
        }
        request = Request(scope)
        with TemporaryDirectory() as root, \
                patch.object(storage.settings, "upload_dir", root), \
                patch.object(storage.settings, "storage_backend", "local"):
            result = await uploads_module.save_image_upload(file, folder, request)
            written = sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*") if p.is_file())
        return result, written, root

    async def test_announcement_endpoint_accepts_long_image_and_others_do_not(self) -> None:
        source = noisy_image_bytes("JPEG", (1240, 7526))
        # 前提：这张图比普通上限大，但没超过公告上限
        self.assertGreater(len(source), storage.MAX_IMAGE_UPLOAD_BYTES)
        self.assertLessEqual(len(source), storage.settings.image_announcement_max_bytes)

        announcement, written, _root = await self._upload("announcements", source)
        # 原图 + 缩略图都落了盘
        self.assertEqual(len(written), 2)
        self.assertTrue(any(name.startswith("announcements/") and not name.startswith("announcements/thumbs/") for name in written))
        self.assertTrue(any(name.startswith("announcements/thumbs/") for name in written))
        self.assertTrue(announcement.url.startswith("http://testserver/uploads/announcements/"))
        self.assertTrue(announcement.url.endswith(".jpg"))
        self.assertIsNotNone(announcement.thumbnail_url)
        self.assertTrue(announcement.thumbnail_url.startswith("http://testserver/uploads/announcements/thumbs/"))

        for folder in ("messages", "teamup", "posts", "avatars", "profile-covers"):
            with self.subTest(folder=folder):
                with self.assertRaises(HTTPException) as context:
                    await self._upload(folder, source)
                self.assertEqual(context.exception.status_code, 413)
                self.assertEqual(context.exception.detail, "Image size must not exceed 3MB")


class DecodeSlotTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_keeps_worker_slot_until_thread_exits(self) -> None:
        import threading

        entered = threading.Event()
        unblock = threading.Event()
        old_slots = storage.IMAGE_WORKERS
        storage.IMAGE_WORKERS = asyncio.Semaphore(1)
        try:
            first = asyncio.create_task(storage.run_image_worker(lambda: (entered.set(), unblock.wait(2))))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(storage.IMAGE_WORKERS._value, 0)
            second = asyncio.create_task(storage.run_image_worker(lambda: "second"))
            await asyncio.sleep(0.02)
            self.assertFalse(second.done())
            unblock.set()
            self.assertEqual(await asyncio.wait_for(second, 2), "second")
        finally:
            unblock.set()
            storage.IMAGE_WORKERS = old_slots


if __name__ == "__main__":
    # Optional small, reproducible local microbenchmark (not a CI assertion):
    # python tests/test_upload_resource_limits.py --bench
    import sys
    if "--bench" in sys.argv:
        sample = image_bytes("PNG", (1600, 1200))
        start = perf_counter()
        for i in range(5):
            storage.process_image_upload(sample, "messages", str(i))
        print(f"5 x 1.92MP PNG to JPEG + thumbnail: {perf_counter() - start:.3f}s")
        sys.argv.remove("--bench")
    unittest.main()

