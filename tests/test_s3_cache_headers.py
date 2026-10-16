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


class S3PrivateBucketRoutingTests(unittest.TestCase):
    """群文件私有桶（docs/group-plus.md 第 5 节）：group-files/ 前缀走 S3_PRIVATE_BUCKET，其余照旧走主桶。"""

    def setUp(self) -> None:
        self.client = Mock()
        for item in (
            patch.object(storage.settings, "s3_bucket", "public-bucket"),
            patch.object(storage.settings, "s3_private_bucket", "private-bucket"),
            patch.object(storage.settings, "storage_backend", "s3"),
            patch.object(storage, "_s3_client", return_value=self.client),
        ):
            item.start()
            self.addCleanup(item.stop)

    def test_group_file_keys_use_private_bucket(self) -> None:
        storage.put_object("group-files/7/abc", b"x", "application/octet-stream")
        self.assertEqual(self.client.upload_fileobj.call_args.args[1], "private-bucket")
        storage.put_object("avatars/a.jpg", b"x", "image/jpeg")
        self.assertEqual(self.client.upload_fileobj.call_args.args[1], "public-bucket")
        self.client.get_object.return_value = {"Body": BytesIO(b"data"), "ContentLength": 4}
        storage.get_object("group-files/7/abc")
        self.assertEqual(self.client.get_object.call_args.kwargs["Bucket"], "private-bucket")
        self.client.head_object.return_value = {"ContentLength": 9}
        self.assertEqual(storage.object_size("group-files/7/abc"), 9)
        self.assertEqual(self.client.head_object.call_args.kwargs["Bucket"], "private-bucket")
        self.assertTrue(storage.object_exists("messages/a.jpg"))
        self.assertEqual(self.client.head_object.call_args.kwargs["Bucket"], "public-bucket")
        self.client.head_object.side_effect = ClientError({"Error": {"Code": "404", "Message": "missing"}}, "HeadObject")
        self.assertIsNone(storage.object_size("group-files/7/missing"))
        # 一次删除里混着两个桶的 key：按桶分开删
        storage.delete_objects(["group-files/7/abc", "avatars/a.jpg", "group-files/8/def"])
        calls = {call.kwargs["Bucket"]: [o["Key"] for o in call.kwargs["Delete"]["Objects"]] for call in self.client.delete_objects.call_args_list}
        self.assertEqual(calls, {"private-bucket": ["group-files/7/abc", "group-files/8/def"], "public-bucket": ["avatars/a.jpg"]})
        self.assertTrue(storage.direct_transfer_enabled())

    def test_without_private_bucket_everything_stays_in_main_bucket(self) -> None:
        with patch.object(storage.settings, "s3_private_bucket", None):
            storage.put_object("group-files/7/abc", b"x", "application/octet-stream")
            self.assertEqual(self.client.upload_fileobj.call_args.args[1], "public-bucket")
            self.assertFalse(storage.direct_transfer_enabled())
        with patch.object(storage.settings, "storage_backend", "local"):
            self.assertFalse(storage.direct_transfer_enabled())

    def test_presigned_urls_are_sigv4_and_pin_length(self) -> None:
        for item in (
            patch.object(storage, "_S3_PRESIGN_CLIENT", None),
            patch.object(storage.settings, "s3_endpoint_url", "https://s3.example.com"),
            patch.object(storage.settings, "s3_access_key_id", "AKTEST"),
            patch.object(storage.settings, "s3_secret_access_key", "secret"),
            patch.object(storage.settings, "s3_region", None),
        ):
            item.start()
            self.addCleanup(item.stop)
        url, headers = storage.presign_put("group-files/7/abc", 123, 900)
        self.assertEqual(headers, {"Content-Type": "application/octet-stream"})
        self.assertIn("/private-bucket/group-files/7/abc?", url)
        self.assertIn("X-Amz-Algorithm=AWS4-HMAC-SHA256", url)
        self.assertIn("X-Amz-SignedHeaders=content-length%3Bcontent-type%3Bhost", url)
        self.assertIn("X-Amz-Expires=900", url)
        get_url = storage.presign_get("group-files/7/abc", 300, "attachment; filename*=UTF-8''%E6%96%87")
        self.assertIn("/private-bucket/group-files/7/abc?", get_url)
        self.assertIn("response-content-disposition=attachment", get_url)
        self.assertIn("response-content-type=application%2Foctet-stream", get_url)
        self.assertNotIn("secret", url + get_url)


if __name__ == "__main__":
    unittest.main()
