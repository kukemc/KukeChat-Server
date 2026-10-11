from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from app.services import storage


class S3CacheHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.bucket_patch = patch.object(storage.settings, "s3_bucket", "test-bucket")
        self.backend_patch = patch.object(storage.settings, "storage_backend", "s3")
        self.client_patch = patch.object(storage, "_s3_client", return_value=self.client)
        for item in (self.bucket_patch, self.backend_patch, self.client_patch):
            item.start()
            self.addCleanup(item.stop)

    def test_public_images_thumbnails_and_voice_have_one_hour_metadata(self) -> None:
        keys = (
            "avatars/avatar.jpg", "profile-covers/cover.png", "messages/photo.gif",
            "posts/post.webp", "teamup/team.jpeg", "announcements/news.jpg",
            "stickers/sticker.jpg", "avatars/thumbs/avatar.jpg",
            "messages/thumbs/photo.jpg", "posts/thumbs/post.jpg",
            "messages/voice.webm", "messages/voice.ogg", "messages/voice.mp3",
            "messages/voice.m4a", "messages/voice.aac", "messages/voice.wav",
        )
        for key in keys:
            with self.subTest(key=key):
                self.client.reset_mock()
                self.assertEqual(storage.put_object(key, b"media", "image/jpeg"), key)
                args, kwargs = self.client.upload_fileobj.call_args
                self.assertEqual((args[1], args[2]), ("test-bucket", key))
                self.assertEqual(args[0].read(), b"media")
                self.assertEqual(kwargs["ExtraArgs"], {
                    "ContentType": "image/jpeg", "CacheControl": "public, max-age=3600",
                })

    def test_other_paths_and_media_types_do_not_receive_cache_metadata(self) -> None:
        keys = (
            "reports/evidence.jpg", "reports/thumbs/evidence.jpg",
            "admin/evidence.ogg", "messages/private-body.txt", "messages/secret.json",
            "messages/thumbs/not-an-image.ogg", "avatars/nested/unexpected.jpg",
            "messages/nested/voice.webm", "messages/voice.bin",
            "posts/voice.ogg", "stickers/voice.webm", "exports/archive.zip",
        )
        for key in keys:
            with self.subTest(key=key):
                self.client.reset_mock()
                stream = BytesIO(b"private")
                self.assertEqual(storage.put_object(key, stream, "application/octet-stream"), key)
                args, kwargs = self.client.upload_fileobj.call_args
                self.assertIs(args[0], stream)
                self.assertEqual(args[2], key)
                self.assertEqual(kwargs["ExtraArgs"], {"ContentType": "application/octet-stream"})

    def test_existing_s3_objects_keep_legacy_local_fallback(self) -> None:
        with TemporaryDirectory() as root, patch.object(storage.settings, "upload_dir", root):
            path = Path(root) / "messages" / "legacy.jpg"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"old image")
            self.client.get_object.side_effect = ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject",
            )
            stored = storage.get_object("messages/legacy.jpg")
            self.assertEqual(stored.data, b"old image")
            self.assertEqual(stored.content_type, "image/jpeg")
            self.client.upload_fileobj.assert_not_called()


if __name__ == "__main__":
    unittest.main()
