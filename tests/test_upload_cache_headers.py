from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import CachedUploadFiles, app
from app.core.config import settings


class UploadCacheHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        isolated = FastAPI()
        isolated.mount("/uploads", CachedUploadFiles(directory=self.root), name="uploads")
        self.client = TestClient(isolated)
        self.addCleanup(self.client.close)

    def put(self, path: str, data: bytes = b"sample file content") -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def test_media_get_head_and_conditional_requests(self) -> None:
        self.put("avatars/old-avatar.png")
        self.put("messages/12345678-1234-4234-8234-123456789012.ogg")
        self.put("posts/thumbs/old-image.jpg")
        for path in (
            "avatars/old-avatar.png",
            "messages/12345678-1234-4234-8234-123456789012.ogg",
            "posts/thumbs/old-image.jpg",
        ):
            with self.subTest(path=path):
                url = f"/uploads/{path}"
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "public, max-age=3600")
                self.assertNotIn("immutable", response.headers["cache-control"])
                self.assertEqual(self.client.head(url).headers["cache-control"], "public, max-age=3600")
                not_modified = self.client.get(url, headers={"If-None-Match": response.headers["etag"]})
                self.assertEqual(not_modified.status_code, 304)
                self.assertEqual(not_modified.content, b"")
                self.assertEqual(not_modified.headers["cache-control"], "public, max-age=3600")
                since = self.client.get(url, headers={"If-Modified-Since": response.headers["last-modified"]})
                self.assertEqual(since.status_code, 304)
                self.assertEqual(since.headers["cache-control"], "public, max-age=3600")

    def test_audio_range_and_head_preserve_file_response(self) -> None:
        path = "messages/old-voice.webm"
        self.put(path, b"0123456789")
        url = f"/uploads/{path}"
        response = self.client.get(url, headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"2345")
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(response.headers["cache-control"], "public, max-age=3600")
        head = self.client.head(url, headers={"Range": "bytes=2-5"})
        self.assertEqual(head.status_code, 206)
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["cache-control"], "public, max-age=3600")

    def test_other_files_and_missing_uploads_are_not_publicly_cached(self) -> None:
        for path in (
            "messages/private-body.txt",
            "reports/evidence.jpg",
            "reports/thumbs/evidence.jpg",
            "admin/evidence.ogg",
            "messages/thumbs/not-an-image.ogg",
            "avatars/nested/unexpected.jpg",
            "messages/secret.json",
        ):
            with self.subTest(path=path):
                self.put(path)
                response = self.client.get(f"/uploads/{path}")
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("cache-control", response.headers)
        self.assertEqual(self.client.get("/uploads/messages/missing.jpg").status_code, 404)
        self.assertNotIn("cache-control", self.client.get("/uploads/messages/missing.jpg").headers)

    def test_local_app_mount_uses_cached_static_files(self) -> None:
        if settings.storage_backend == "local":
            mount = next(route for route in app.routes if getattr(route, "path", None) == "/uploads")
            self.assertIsInstance(mount.app, CachedUploadFiles)


if __name__ == "__main__":
    unittest.main()
