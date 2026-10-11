"""Exercise the middleware wired in main.py without importing its production settings.

Importing app.main creates settings/upload_dir and may read .env; instead compile only
its middleware/static-file classes and actual add_middleware declaration into an
isolated FastAPI app backed by a temporary upload directory.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient


MAIN_PATH = Path(__file__).resolve().parents[1] / "app" / "main.py"


def _isolated_main_classes():
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    names = {"CachedUploadFiles", "UploadAwareGZipMiddleware"}
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in names]
    namespace = {
        "PurePosixPath": PurePosixPath,
        "Response": Response,
        "StaticFiles": StaticFiles,
        "GZipMiddleware": GZipMiddleware,
        "settings": SimpleNamespace(storage_backend="local"),
    }
    exec(compile(ast.Module(body=classes, type_ignores=[]), str(MAIN_PATH), "exec"), namespace)

    middleware_calls = [
        node for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "add_middleware"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "app"
        and node.value.args
        and isinstance(node.value.args[0], ast.Name)
        and node.value.args[0].id in {"GZipMiddleware", "UploadAwareGZipMiddleware"}
    ]
    if len(middleware_calls) != 1:
        raise AssertionError("main.py must register exactly one compression middleware")
    call = middleware_calls[0].value
    options = {keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords}
    if options.get("minimum_size") != 1024:
        raise AssertionError("test must exercise main.py's 1024-byte gzip threshold")
    return namespace, namespace[call.args[0].id], options


class MediaGZipTests(unittest.TestCase):
    def setUp(self) -> None:
        namespace, middleware, options = _isolated_main_classes()
        self.settings = namespace["settings"]
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.app = FastAPI()
        self.app.add_middleware(middleware, **options)
        self.app.mount("/uploads", namespace["CachedUploadFiles"](directory=self.root), name="uploads")

        @self.app.get("/json")
        def large_json():
            return {"payload": "x" * 2048}

        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def put(self, path: str, content: bytes) -> str:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return f"/uploads/{path}"

    def test_encoded_media_keeps_identity_range_cache_and_head_headers(self) -> None:
        # Already encoded media is usually high-entropy; gzip still costs CPU even
        # when its wire size is larger than the original, and can break Range.
        media = {
            "avatars/photo.JPEG": b"\xff\xd8\xff" + os.urandom(4096),
            "posts/thumbs/preview.gif": b"GIF89a" + os.urandom(4096),
            "messages/voice.ogg": b"OggS" + os.urandom(4096),
        }
        for path, content in media.items():
            with self.subTest(path=path):
                url = self.put(path, content)
                for accept in ("gzip", "br, gzip", "br"):
                    with self.subTest(accept_encoding=accept):
                        headers = {"Accept-Encoding": accept}
                        full = self.client.get(url, headers=headers)
                        self.assertEqual(full.status_code, 200)
                        self.assertEqual(full.content, content)
                        self.assertEqual(full.headers["content-length"], str(len(content)))
                        self.assertNotIn("content-encoding", full.headers)
                        self.assertEqual(full.headers["cache-control"], "public, max-age=3600")
                        self.assertIn("etag", full.headers)

                        part = self.client.get(url, headers={**headers, "Range": "bytes=1024-3071"})
                        self.assertEqual(part.status_code, 206)
                        self.assertEqual(part.content, content[1024:3072])
                        self.assertEqual(part.headers["content-length"], "2048")
                        self.assertEqual(part.headers["content-range"], f"bytes 1024-3071/{len(content)}")
                        self.assertNotIn("content-encoding", part.headers)
                        self.assertEqual(part.headers["etag"], full.headers["etag"])
                        self.assertEqual(part.headers["cache-control"], "public, max-age=3600")

                        not_modified = self.client.get(url, headers={**headers, "If-None-Match": full.headers["etag"]})
                        self.assertEqual(not_modified.status_code, 304)
                        self.assertEqual(not_modified.content, b"")
                        self.assertNotIn("content-encoding", not_modified.headers)
                        self.assertEqual(not_modified.headers["cache-control"], "public, max-age=3600")
                        self.assertEqual(not_modified.headers["etag"], full.headers["etag"])

                        head = self.client.head(url, headers=headers)
                        self.assertEqual(head.status_code, 200)
                        self.assertEqual(head.content, b"")
                        self.assertEqual(head.headers["content-length"], str(len(content)))
                        self.assertNotIn("content-encoding", head.headers)
                        self.assertEqual(head.headers["etag"], full.headers["etag"])
                        range_head = self.client.head(url, headers={**headers, "Range": "bytes=1024-3071"})
                        self.assertEqual(range_head.status_code, 206)
                        self.assertEqual(range_head.content, b"")
                        self.assertEqual(range_head.headers["content-range"], part.headers["content-range"])
                        self.assertEqual(range_head.headers["content-length"], "2048")
                        self.assertNotIn("content-encoding", range_head.headers)

    def test_json_still_gzips_only_when_accepted(self) -> None:
        for accept in ("gzip", "br, gzip"):
            with self.subTest(accept_encoding=accept):
                response = self.client.get("/json", headers={"Accept-Encoding": accept})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-encoding"], "gzip")
                self.assertIn("Accept-Encoding", response.headers["vary"])
                self.assertEqual(response.json(), {"payload": "x" * 2048})
        response = self.client.get("/json", headers={"Accept-Encoding": "br"})
        self.assertNotIn("content-encoding", response.headers)

    def test_unlisted_uploads_and_s3_configuration_keep_original_gzip_behavior(self) -> None:
        for path in ("messages/note.txt", "reports/evidence.jpg", "avatars/nested/picture.jpg", "messages/thumbs/audio.ogg"):
            with self.subTest(path=path):
                text = self.put(path, b"x" * 2048)
                self.assertEqual(self.client.get(text, headers={"Accept-Encoding": "gzip"}).headers["content-encoding"], "gzip")
        picture = self.put("avatars/picture.jpg", b"x" * 2048)
        self.settings.storage_backend = "s3"
        # The mounted static route is only a test double for a legacy S3 /uploads response.
        # Switching backend must not change the gzip policy for that route.
        self.assertEqual(self.client.get(picture, headers={"Accept-Encoding": "gzip"}).headers["content-encoding"], "gzip")


if __name__ == "__main__":
    unittest.main()
