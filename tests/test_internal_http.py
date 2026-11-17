"""internal_http.urlopen：复用长连接，断了的空闲连接自动换新，异常和标准库 urlopen 一致。"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from app.services import internal_http


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    peers: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        Handler.peers.append(self.client_address)
        if self.path == "/slow":
            time.sleep(1.5)
        status = 422 if self.path == "/bad" else 200
        data = json.dumps({"echo": body.decode(), "token": self.headers.get("X-Moderation-Token")}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if self.path == "/close":
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    Handler.peers = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    httpd.shutdown()
    httpd.server_close()


def post(url, payload="x", timeout=5.0):
    request = Request(url, data=payload.encode(), headers={"Content-Type": "text/plain", "X-Moderation-Token": "t"}, method="POST")
    with internal_http.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def test_reuses_connection_and_sends_headers(server):
    base, _ = server
    assert post(f"{base}/a", "hello") == {"echo": "hello", "token": "t"}
    post(f"{base}/a")
    post(f"{base}/a")
    assert len({peer for peer in Handler.peers}) == 1  # 同一个客户端端口 = 同一条连接


def test_server_closed_connection_is_replaced(server):
    base, _ = server
    post(f"{base}/close")
    assert post(f"{base}/a", "again")["echo"] == "again"


def test_http_error_like_stdlib(server):
    base, _ = server
    with pytest.raises(HTTPError) as info:
        post(f"{base}/bad", "body")
    assert info.value.code == 422 and b"body" in info.value.read()


def test_timeout_raises_timeout_error(server):
    base, _ = server
    with pytest.raises(TimeoutError):
        post(f"{base}/slow", timeout=0.3)


def test_unreachable_raises_url_error():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(URLError) as info:
        post(f"http://127.0.0.1:{port}/a")
    assert not isinstance(info.value.reason, TimeoutError)
