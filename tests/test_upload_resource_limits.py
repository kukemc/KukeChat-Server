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
