"""Isolated ASGI tests: never import app.main or read production .env."""
from __future__ import annotations

import ast
import json
import logging
import unittest
from pathlib import Path
from contextlib import asynccontextmanager
from io import StringIO

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient

from app.core.transport_metrics import TransportMetrics, TransportMetricsMiddleware


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logs = StringIO()
        self.logger = logging.Logger("transport-test", level=logging.INFO)
        self.handler = logging.StreamHandler(self.logs)
        self.logger.addHandler(self.handler)
        self.metrics = TransportMetrics(logger=self.logger)

        @asynccontextmanager
        async def lifespan(_app):
            await self.metrics.start()
            try:
                yield
            finally:
                await self.metrics.stop()

        app = FastAPI(lifespan=lifespan)
        app.add_middleware(GZipMiddleware, minimum_size=100, compresslevel=4)
        app.add_middleware(TransportMetricsMiddleware, metrics=self.metrics)

        @app.post("/api/v1/echo/{item}")
        async def echo(item: str, request: Request):
            return Response(await request.body())

        @app.get("/api/v1/stream")
        async def stream():
            async def chunks():
                yield b"abc"
                yield b"def"
            return StreamingResponse(chunks(), media_type="application/octet-stream")

        @app.get("/api/v1/large")
        async def large():
            return Response(b"a" * 2048)

        @app.api_route("/uploads/messages/voice.ogg", methods=["GET", "HEAD"])
        async def media(request: Request):
            headers = {"ETag": "public-tag", "Cache-Control": "public, max-age=3600"}
            if request.headers.get("if-none-match") == "public-tag":
                return Response(status_code=304, headers=headers)
            if request.method == "HEAD":
                return Response(headers={**headers, "Content-Length": "10"})
            if request.headers.get("range"):
                return Response(b"2345", status_code=206, headers={**headers, "Content-Range": "bytes 2-5/10"})
            return Response(b"0123456789", headers=headers)

        @app.get("/api/v1/fail/{token}")
        async def fail(token: str):
            raise RuntimeError("generic failure")

        self.app = app
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def take(self):
        self.metrics.flush()
        lines = self.logs.getvalue().splitlines()
        self.logs.seek(0)
        self.logs.truncate()
        return [json.loads(line) for line in lines]

    def test_gzip_bytes_are_encoded_asgi_bytes_and_streams_are_counted(self):
        zipped = self.client.get("/api/v1/large", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(zipped.headers["content-encoding"], "gzip")
        self.assertEqual(zipped.content, b"a" * 2048)
        streaming = self.client.get("/api/v1/stream", headers={"Accept-Encoding": "identity"})
        self.assertEqual(streaming.content, b"abcdef")
        rows = self.take()[0]["buckets"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["count"], 2)
        # gzip body is not the 2048 decoded bytes returned by TestClient.
        self.assertLess(rows[0]["response_body_bytes"], 2048)
        self.assertGreater(rows[0]["response_body_bytes"], 6)
        self.assertEqual(sum(rows[0]["header_ms_buckets"]), 2)
        self.assertEqual(sum(rows[0]["final_ms_buckets"]), 2)
        self.assertEqual(self.take(), [])  # Window reset, no empty-window logging.

    def test_request_bytes_count_only_body_read_by_application(self):
        data = b"private token and body" * 10
        res = self.client.post("/api/v1/echo/sensitive", content=data, headers={"Accept-Encoding": "identity"})
        self.assertEqual(res.content, data)
        row = self.take()[0]["buckets"][0]
        self.assertEqual((row["request_body_bytes"], row["response_body_bytes"]), (len(data), len(data)))
        self.assertNotIn("private token", self.logs.getvalue())

    def test_head_range_304_and_media_suffix(self):
        url = "/uploads/messages/voice.ogg"
        full = self.client.get(url, headers={"Accept-Encoding": "identity"})
        part = self.client.get(url, headers={"Range": "bytes=2-5", "Accept-Encoding": "identity"})
        head = self.client.head(url, headers={"Accept-Encoding": "identity"})
        modified = self.client.get(url, headers={"If-None-Match": "public-tag", "Accept-Encoding": "identity"})
        self.assertEqual([full.status_code, part.status_code, head.status_code, modified.status_code], [200, 206, 200, 304])
        rows = self.take()[0]["buckets"]
        self.assertEqual(sum(row["response_body_bytes"] for row in rows), 14)
        self.assertEqual({row["media_suffix"] for row in rows}, {"ogg"})
        self.assertEqual({row["status_class"] for row in rows}, {"2xx", "3xx"})
        self.assertEqual({row["route_group"] for row in rows}, {"public_media"})
        self.assertEqual(next(row for row in rows if row["method"] == "HEAD")["response_body_bytes"], 0)

    def test_private_media_unknown_method_and_10000_dynamic_paths_do_not_cardinalize_or_leak(self):
        for i in range(10000):
            self.metrics.record(method=f"CUSTOM_{i}", path=f"/api/v1/secrets/token_{i}?query=private_{i}",
                                status=200, request_bytes=0, response_bytes=0, header_ms=1, final_ms=2)
        self.metrics.record(method="GET", path="/uploads/reports/secret-photo.jpg", status=200,
                            request_bytes=0, response_bytes=1, header_ms=None, final_ms=20)
        self.metrics.record(method="GET", path="/uploads/messages/private.ogg", status=404,
                            request_bytes=0, response_bytes=1, header_ms=None, final_ms=20)
        self.metrics.record(method="GET", path="/token=secret/location.jpg", status=0,
                            request_bytes=0, response_bytes=0, header_ms=None, final_ms=1, disconnected=True)
        records = self.take()
        self.assertEqual(len(records), 1)
        rows = records[0]["buckets"]
        self.assertEqual(sum(row["count"] for row in rows), 10003)
        self.assertLess(len(rows), 500)
        self.assertIn({"method": "OTHER", "route_group": "api", "status_class": "2xx", "media_suffix": "none"},
                      [{k: row[k] for k in ("method", "route_group", "status_class", "media_suffix")} for row in rows])
        self.assertTrue(any(row["status_class"] == "error" for row in rows))
        text = self.logs.getvalue()
        for sensitive in ("token_", "secret-photo", "secret", "private", "/api/v1/", "location.jpg"):
            self.assertNotIn(sensitive, text)

    def test_exception_never_logs_path_and_disconnected_not_persisted(self):
        with self.assertRaisesRegex(RuntimeError, "generic failure"):
            self.client.get("/api/v1/fail/one-time-secret", headers={"Accept-Encoding": "identity"})
        rows = self.take()[0]["buckets"]
        self.assertEqual(rows[0]["status_class"], "error")
        self.assertNotIn("one-time-secret", self.logs.getvalue())

    def test_main_registers_observer_after_gzip_without_importing_settings(self):
        source = Path(__file__).resolve().parents[1] / "app" / "main.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        calls = [
            node.value.args[0].id for node in tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "add_middleware"
            and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "app"
            and node.value.args and isinstance(node.value.args[0], ast.Name)
        ]
        # Starlette's add_middleware inserts at index 0: last registration is outermost.
        registration = source.read_text(encoding="utf-8")
        self.assertIn("UploadAwareGZipMiddleware", calls)
        self.assertLess(registration.index("app.add_middleware(UploadAwareGZipMiddleware"),
                        registration.index("app.add_middleware(TransportMetricsMiddleware"))
        self.assertNotIn("slow_request_logger", registration)
        self.assertIn("if transport_metrics is not None:", registration)

    def test_disabled_configuration_requires_no_observer(self):
        # Production adds the middleware only when enabled. The disabled app has
        # no per-request callbacks or periodic task and emits no metrics logs.
        bare = FastAPI()

        @bare.get("/health")
        async def health():
            return {"ok": True}

        with TestClient(bare) as client:
            self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(self.take(), [])


if __name__ == "__main__":
    unittest.main()
