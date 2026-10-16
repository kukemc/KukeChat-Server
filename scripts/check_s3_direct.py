#!/usr/bin/env python
"""群文件直传的兼容性检查（docs/group-plus.md 第 5 节「雨云配置」）：在服务器上带着生产环境变量跑，对私有桶实测一遍。

    python scripts/check_s3_direct.py              # 只检查，CORS 只读出来看
    python scripts/check_s3_direct.py --apply-cors # 先把 CORS 规则写到私有桶上（控制台没有 CORS 设置时用），再检查

查的东西：预签名 PUT（带 Content-Length）能传、长度不对会不会被拒、head、预签名 GET 的下载头、
CORS 预检、不带签名能不能读（桶是不是真的私有），最后删掉测试对象。
读的是后端自己的配置（app.core.config.settings），不打印任何密钥，也不打印带签名的 URL。只用标准库和 boto3。
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services import storage  # noqa: E402

CORS_RULE = {
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET", "PUT", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600,
}
FILENAME = "直传检查 test.txt"

results: list[tuple[str, str, str]] = []


def report(status: str, item: str, detail: str = "") -> None:
    results.append((status, item, detail))
    print(f"[{status}] {item}" + (f"：{detail}" if detail else ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _ssl_context():
    """有的服务器上 Python 找不到系统根证书（urllib 校验 https 失败）：用 botocore 自带的那份，和后端访问存储时一致。"""
    import ssl
    from pathlib import Path as _Path

    import botocore

    bundle = _Path(botocore.__file__).parent / "cacert.pem"
    return ssl.create_default_context(cafile=str(bundle)) if bundle.exists() else ssl.create_default_context()


_opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_ssl_context()))


def http(method: str, url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with _opener.open(request, timeout=20) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def show_cors(bucket: str) -> None:
    try:
        rules = storage._client_for(bucket).get_bucket_cors(Bucket=bucket).get("CORSRules", [])
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code == "NoSuchCORSConfiguration":
            # 雨云不提供桶级 CORS 规则，但默认对所有来源放行：真正要看的是下面的预检结果
            report("INFO", "桶的 CORS 配置", "桶上没有规则；以下面的预检结果为准（雨云默认对所有来源放行）")
        else:
            report("WARN", "桶的 CORS 配置", f"读不出来（{code or exc.__class__.__name__}），以下面的预检结果为准")
        return
    except BotoCoreError as exc:
        report("WARN", "桶的 CORS 配置", f"读不出来（{exc.__class__.__name__}）")
        return
    if not rules:
        report("WARN", "桶的 CORS 配置", "规则为空")
    for index, rule in enumerate(rules, 1):
        print(
            f"    规则 {index}：Origins={rule.get('AllowedOrigins')} Methods={rule.get('AllowedMethods')} "
            f"Headers={rule.get('AllowedHeaders')} Expose={rule.get('ExposeHeaders')} MaxAge={rule.get('MaxAgeSeconds')}"
        )
    if rules:
        report("OK", "桶的 CORS 配置", f"{len(rules)} 条规则")


def apply_cors(bucket: str) -> None:
    try:
        storage._client_for(bucket).put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [CORS_RULE]})
    except (BotoCoreError, ClientError) as exc:
        code = exc.response.get("Error", {}).get("Code", "") if isinstance(exc, ClientError) else exc.__class__.__name__
        if code == "NotImplemented":
            report("INFO", "写入 CORS 规则", "服务商不支持写入（雨云就是这样，默认已放行），以下面的预检结果为准")
        else:
            report("FAIL", "写入 CORS 规则", f"失败（{code}）")
        return
    report("PASS", "写入 CORS 规则", "已写入，读回来如下")


def main() -> int:
    parser = argparse.ArgumentParser(description="检查对象存储是否支持群文件直传")
    parser.add_argument("--apply-cors", action="store_true", help="先把直传需要的 CORS 规则写到私有桶上")
    args = parser.parse_args()

    bucket = settings.s3_private_bucket
    if settings.storage_backend != "s3":
        report("FAIL", "存储后端", f"STORAGE_BACKEND 是 {settings.storage_backend}，直传需要 s3")
        return 1
    if not bucket:
        report("FAIL", "私有桶", "没有配置 S3_PRIVATE_BUCKET")
        return 1
    endpoint = settings.s3_endpoint_url or ""
    print(f"endpoint：{urlsplit(endpoint).netloc or '(默认 AWS)'}，私有桶：{bucket}，主桶：{settings.s3_bucket}")
    if endpoint and not endpoint.lower().startswith("https://"):
        report("WARN", "endpoint 不是 https", "预签名 URL 会是 http 的，https 页面里的浏览器会当成混合内容拦掉；请把 S3_ENDPOINT_URL 改成 https://")
    if bucket == settings.s3_bucket:
        report("WARN", "私有桶和主桶是同一个", "主桶开着公开读，群文件会被直链读到")

    if args.apply_cors:
        apply_cors(bucket)
    show_cors(bucket)

    key = f"{storage.PRIVATE_PREFIX}_direct-check/{uuid4().hex}"
    wrong_key = f"{key}-wrong"
    data = ("KukeChat 直传检查 " * 64).encode("utf-8")
    try:
        # 1. 预签名 PUT，长度正确
        url, headers = storage.presign_put(key, len(data), 300)
        if "X-Amz-SignedHeaders=" in url and "content-length" in url.split("X-Amz-SignedHeaders=", 1)[1].split("&", 1)[0]:
            report("OK", "签名包含 Content-Length")
        else:
            report("WARN", "签名不包含 Content-Length", "长度只能靠 complete 时的 head 检查")
        status, _h, body = http("PUT", url, data=data, headers=headers)
        if 200 <= status < 300:
            report("PASS", "预签名 PUT 上传", f"HTTP {status}")
        else:
            report("FAIL", "预签名 PUT 上传", f"HTTP {status} {body[:200]!r}")

        # 2. 长度不对
        wrong_url, wrong_headers = storage.presign_put(wrong_key, len(data), 300)
        status, _h, _b = http("PUT", wrong_url, data=data[:-7], headers=wrong_headers)
        if 400 <= status < 500:
            report("PASS", "长度不对的 PUT 被拒", f"HTTP {status}")
        elif 200 <= status < 300:
            report("WARN", "长度不对的 PUT 没被拒", f"HTTP {status}：服务商不校验签名里的 Content-Length，complete 时的大小检查是唯一的防线")
        else:
            report("FAIL", "长度不对的 PUT", f"HTTP {status}（意料之外）")

        # 3. head
        size = storage.object_size(key)
        if size == len(data):
            report("PASS", "head_object", f"{size} 字节")
        else:
            report("FAIL", "head_object", f"期望 {len(data)} 字节，实际 {size}")

        # 4. 预签名 GET 和下载头
        ascii_name = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "._-") else "_" for ch in FILENAME)
        disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(FILENAME, safe='')}"
        get_url = storage.presign_get(key, 300, disposition)
        status, got_headers, body = http("GET", get_url)
        if status == 200 and body == data:
            report("PASS", "预签名 GET 下载", "内容一致")
        else:
            report("FAIL", "预签名 GET 下载", f"HTTP {status}，内容{'一致' if body == data else '不一致'}")
        got_disposition = got_headers.get("content-disposition", "")
        if got_disposition.startswith("attachment") and "filename*=UTF-8''" in got_disposition:
            report("PASS", "Content-Disposition", got_disposition)
        else:
            report("FAIL", "Content-Disposition", f"返回的是 {got_disposition!r}，服务商可能不支持 response-content-disposition")
        got_type = got_headers.get("content-type", "")
        report("PASS" if got_type.startswith("application/octet-stream") else "WARN", "Content-Type", got_type or "(没有)")

        # 5. CORS 预检
        status, cors_headers, _b = http(
            "OPTIONS",
            url,
            headers={"Origin": "https://example.com", "Access-Control-Request-Method": "PUT", "Access-Control-Request-Headers": "content-type"},
        )
        allow_origin = cors_headers.get("access-control-allow-origin", "")
        allow_methods = cors_headers.get("access-control-allow-methods", "")
        if 200 <= status < 300 and allow_origin in {"*", "https://example.com"} and ("PUT" in allow_methods.upper() or allow_methods.strip() == "*"):
            report("PASS", "CORS 预检（PUT）", f"Allow-Origin={allow_origin} Allow-Methods={allow_methods}")
        else:
            report("FAIL", "CORS 预检（PUT）", f"HTTP {status} Allow-Origin={allow_origin or '(无)'}：浏览器里的直传会失败，请配置 CORS（或加 --apply-cors）")

        # 6. 不带签名读：桶必须是私有的
        parts = urlsplit(get_url)
        status, _h, _b = http("GET", urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
        if status in {401, 403}:
            report("PASS", "不带签名读被拒（桶是私有的）", f"HTTP {status}")
        elif 200 <= status < 300:
            report("FAIL", "不带签名也能读", "这个桶开着公开读，请在控制台关掉")
        else:
            report("WARN", "不带签名读", f"HTTP {status}（不是 401/403，请人工确认）")
    except Exception as exc:  # noqa: BLE001
        report("FAIL", "检查中断", f"{exc.__class__.__name__}: {str(exc)[:200]}")
    finally:
        # 7. 清理
        storage.delete_objects([key, wrong_key])
        try:
            left = storage.object_size(key)
        except Exception:  # noqa: BLE001
            left = -1
        report("PASS" if left is None else "WARN", "删除测试对象", "已删除" if left is None else "没删掉，请手动删除 group-files/_direct-check/ 下的对象")

    failed = [item for status, item, _ in results if status == "FAIL"]
    warned = [item for status, item, _ in results if status == "WARN"]
    print()
    print("结论：" + ("FAIL，" + "、".join(failed) if failed else "PASS") + (f"（注意：{'、'.join(warned)}）" if warned else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
