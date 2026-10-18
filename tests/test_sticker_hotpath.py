from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.sticker import FavoriteSticker, StickerAsset
from app.models.user import User
from app.services import stickers, storage


def image_bytes(format: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (48, 48), "red").save(output, format=format)
    return output.getvalue()


def animated_gif() -> bytes:
    output = BytesIO()
    Image.new("RGB", (24, 24), "red").save(
        output, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (24, 24), "blue")], duration=100, loop=0,
    )
    return output.getvalue()


class StickerHotpathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{(Path(self.tmp.name) / 'test.db').as_posix()}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add_all([User(username="one", hashed_password="x"), User(username="two", hashed_password="x")])
            await db.commit()

        self.client = Mock()
        self.patches = [
            patch.object(storage.settings, "storage_backend", "s3"),
            patch.object(storage.settings, "s3_bucket", "test-bucket"),
            patch.object(storage, "_s3_client", return_value=self.client),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()
        self.tmp.cleanup()

    def set_source(self, content: bytes, content_type: str) -> None:
        self.client.get_object.side_effect = lambda **kwargs: {
            "Body": BytesIO(content), "ContentLength": len(content), "ContentType": content_type,
        }

    async def favorite(self, user_id: int, source: str = "messages/source.png") -> FavoriteSticker:
        async with self.sessions() as db:
            return await stickers.add_favorite_sticker(db, user_id, f"/uploads/{source}")

    async def test_reorder_and_batch_delete(self) -> None:
        self.set_source(animated_gif(), "image/gif")
        ids = [(await self.favorite(1, f"messages/{name}.gif")).asset_id for name in ("a", "b", "c")]
        async with self.sessions() as db:
            # 新加的在前
            self.assertEqual([item.asset_id for item in await stickers.list_favorite_stickers(db, 1)], ids[::-1])
            ordered = await stickers.reorder_favorite_stickers(db, 1, [ids[1], ids[0], ids[2]])
            self.assertEqual([item.asset_id for item in ordered], [ids[1], ids[0], ids[2]])
            for wrong in ([ids[0], ids[1]], [ids[0], ids[0], ids[1], ids[2]], [*ids, 999]):
                with self.assertRaises(HTTPException) as caught:
                    await stickers.reorder_favorite_stickers(db, 1, wrong)
                self.assertEqual(caught.exception.status_code, 409)
        # 排过序之后新加的排最前
        newest = (await self.favorite(1, "messages/d.gif")).asset_id
        async with self.sessions() as db:
            self.assertEqual([item.asset_id for item in await stickers.list_favorite_stickers(db, 1)], [newest, ids[1], ids[0], ids[2]])
            self.assertEqual(await stickers.remove_favorite_stickers(db, 1, [ids[0], newest, 12345]), 2)
            self.assertEqual([item.asset_id for item in await stickers.list_favorite_stickers(db, 1)], [ids[1], ids[2]])
            # 别人的收藏删不到
            self.assertEqual(await stickers.remove_favorite_stickers(db, 2, [ids[1]]), 0)

    async def test_png_and_jpeg_each_fetch_from_s3_only_once(self) -> None:
        for fmt, suffix, content_type in (("PNG", "png", "image/png"), ("JPEG", "jpg", "image/jpeg")):
            with self.subTest(fmt=fmt):
                self.client.reset_mock()
                self.set_source(image_bytes(fmt), content_type)
                favorite = await self.favorite(1, f"messages/{fmt.lower()}.{suffix}")
                self.assertEqual(favorite.asset.content_type, "image/jpeg")
                self.assertTrue(favorite.asset.storage_path.startswith("stickers/"))
                self.client.get_object.assert_called_once_with(Bucket="test-bucket", Key=f"messages/{fmt.lower()}.{suffix}")
                self.assertEqual(self.client.upload_fileobj.call_count, 2)

    async def test_gif_preserves_animated_original_without_upload(self) -> None:
        original = animated_gif()
        with Image.open(BytesIO(original)) as gif:
            self.assertEqual(gif.n_frames, 2)
        self.set_source(original, "image/gif")
        favorite = await self.favorite(1, "messages/animated.gif")
        self.assertEqual(favorite.asset.storage_path, "messages/animated.gif")
        self.assertEqual(favorite.asset.content_type, "image/gif")
        self.client.get_object.assert_called_once()
        self.client.upload_fileobj.assert_not_called()

    async def test_concurrent_same_source_writes_once_even_for_two_users(self) -> None:
        original = image_bytes("PNG")
        entered = threading.Event()
        release = threading.Event()
        def delayed_get(**kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test S3 read timed out")
            return {"Body": BytesIO(original), "ContentLength": len(original), "ContentType": "image/png"}
        self.client.get_object.side_effect = delayed_get
        first = asyncio.create_task(self.favorite(1))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            second = asyncio.create_task(self.favorite(2))
            await asyncio.sleep(0.03)
            self.assertEqual(self.client.get_object.call_count, 1)
        finally:
            release.set()
        one, two = await asyncio.wait_for(asyncio.gather(first, second), 5)
        self.assertEqual(one.asset.id, two.asset.id)
        self.assertEqual(self.client.get_object.call_count, 1)
        self.assertEqual(self.client.upload_fileobj.call_count, 2)
        async with self.sessions() as db:
            self.assertEqual(len((await db.scalars(select(StickerAsset))).all()), 1)
            self.assertEqual(len((await db.scalars(select(FavoriteSticker))).all()), 2)
        self.assertEqual(len(stickers._SOURCE_LOCKS), 64)
        self.assertIs(stickers._source_lock("messages/source.png"), stickers._source_lock("messages/source.png"))

    async def test_failed_read_releases_lock_for_next_request(self) -> None:
        self.client.get_object.side_effect = HTTPException(status_code=502, detail="read failed")
        with self.assertRaises(HTTPException):
            await self.favorite(1)
        self.set_source(image_bytes("PNG"), "image/png")
        favorite = await asyncio.wait_for(self.favorite(1), 5)
        self.assertEqual(favorite.asset.content_type, "image/jpeg")
        self.assertEqual(self.client.get_object.call_count, 2)


if __name__ == "__main__":
    unittest.main()
