"""调内部服务（内容审核、二审、语音识别）用的 HTTP：复用长连接。

审核服务在另一台机器上，走 Nagi 项目内网，一个来回约 175ms。原来用 urllib 的 urlopen，每次都新建连接，
平白多一个来回（实测 380ms 对 175ms）。这里用一个进程级的 urllib3 连接池（boto3 已经带了 urllib3），线程安全，
调用方都是 asyncio.to_thread 里同步调。

urlopen(request, timeout) 和标准库的同名函数用法一样、抛的异常也一样（HTTPError / URLError / TimeoutError），
调用方和测试（patch 模块里的 urlopen）都不用改。
"""

from __future__ import annotations

import io
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import urllib3
from urllib3.exceptions import HTTPError as Urllib3Error
from urllib3.exceptions import NewConnectionError, ProtocolError
from urllib3.exceptions import TimeoutError as Urllib3Timeout

# 每个 worker 进程一个池；每个地址最多留 16 条空闲连接，忙的时候超出的照样新建（block=False），用完就关
_pool = urllib3.PoolManager(num_pools=8, maxsize=16, block=False, retries=False)


class _Response:
    def __init__(self, status: int, data: bytes, headers: Any) -> None:
        self.status = status
        self.headers = headers
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def urlopen(request: Request, timeout: float) -> _Response:
    url = request.full_url
    headers = dict(request.header_items())
    method = request.get_method()
    for attempt in range(2):
        try:
            response = _pool.request(
                method,
                url,
                body=request.data,
                headers=headers,
                timeout=urllib3.Timeout(connect=min(timeout, 5.0), read=timeout),
                retries=False,
                redirect=False,
            )
            break
        except NewConnectionError as exc:  # 连不上（它在 urllib3 里算 ConnectTimeoutError 的子类，要先接）
            raise URLError(OSError(str(exc))) from exc
        except Urllib3Timeout as exc:
            raise TimeoutError(str(exc)) from exc
        except ProtocolError as exc:
            # 复用的空闲连接被对端或中间的代理关掉了：换一条新连接重发一次。审核 / 识别重复发没有副作用
            if attempt == 0:
                continue
            raise URLError(OSError(str(exc))) from exc
        except Urllib3Error as exc:
            raise URLError(OSError(str(exc))) from exc
    if response.status >= 400:
        raise HTTPError(url, response.status, response.reason or "", response.headers, io.BytesIO(response.data))  # type: ignore[arg-type]
    return _Response(response.status, response.data, response.headers)
