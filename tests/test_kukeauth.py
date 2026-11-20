"""KukeAuth（docs/kukeauth.md）：OAuth 流程的各种攻击情况、账号站接口、和聊天登录的边界。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.kukeauth import router as kukeauth_router
from app.api.v1.admin_kukeauth import router as admin_router
from app.core import rate_limit
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.core.security import create_access_token, hash_password
from app.models.enums import PlatformRole, UserStatus
from app.models.kukeauth import OAuthClient, OAuthRefreshToken
from app.models.user import CcwBindingChallenge, User
from app.services import auth as auth_service
from app.services import ccw as ccw_service
from app.services import kukeauth as service

ISSUER = "https://auth.test"
CALLBACK = "https://feedback.test/cb"
ORIGIN = {"Origin": ISSUER}
_KEY = ec.generate_private_key(ec.SECP256R1()).private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class Base_(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 't.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override_db():
            async with self.sessions() as session:
                yield session

        api = FastAPI()
        api.include_router(kukeauth_router)
        api.include_router(admin_router, prefix="/api/v1/admin")

        @api.get("/chat/me")
        async def chat_me(user: Annotated[User, Depends(get_current_user)]) -> dict:
            return {"id": user.id}

        api.dependency_overrides[get_db] = override_db
        self.api = api
        self.patches = [
            patch.object(settings, "kukeauth_issuer", ISSUER),
            patch.object(settings, "kukeauth_signing_key", _KEY),
            patch.object(settings, "cookie_secure", True),
        ]
        for item in self.patches:
            item.start()
        rate_limit.reset()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url=ISSUER, follow_redirects=False)
        async with self.sessions() as db:
            db.add(User(username="alice", hashed_password=hash_password("password-1"), nickname="爱丽丝", registration_ip="10.0.0.1", email_verified_at=datetime.now(UTC), email="a@example.com"))
            db.add(
                OAuthClient(
                    client_id="feedback",
                    secret_hash=service.hash_secret("s3cret"),
                    name="反馈站",
                    redirect_uris=json.dumps([CALLBACK]),
                    post_logout_redirect_uris=json.dumps(["https://feedback.test/bye"]),
                    allowed_scopes=json.dumps(["openid", "profile", "email", "ccw", "offline_access"]),
                )
            )
            db.add(OAuthClient(client_id="desktop", name="桌面端", redirect_uris=json.dumps(["http://127.0.0.1/cb"]), allowed_scopes=json.dumps(["openid", "profile"])))
            await db.commit()

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        for item in self.patches:
            item.stop()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def login(self, username: str = "alice", password: str = "password-1") -> httpx.Response:
        return await self.http.post("/account/api/login", json={"username": username, "password": password}, headers=ORIGIN)

    def params(self, challenge: str, **extra: str) -> dict[str, str]:
        return {
            "response_type": "code",
            "client_id": "feedback",
            "redirect_uri": CALLBACK,
            "scope": "openid profile email offline_access",
            "state": "st",
            "nonce": "nn",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **extra,
        }

    async def get_code(self, challenge: str, **extra: str) -> str:
        response = await self.http.get("/oauth/authorize", params=self.params(challenge, **extra))
        self.assertEqual(response.status_code, 302, response.text)
        location = response.headers["location"]
        if location.startswith(CALLBACK):  # 已经同意过，不用再确认
            return parse_qs(urlsplit(location).query)["code"][0]
        request_id = parse_qs(urlsplit(location).query)["request"][0]
        approved = await self.http.post(f"/account/api/requests/{request_id}/approve", headers=ORIGIN)
        self.assertEqual(approved.status_code, 200, approved.text)
        query = parse_qs(urlsplit(approved.json()["redirect_url"]).query)
        self.assertEqual(query["state"], ["st"])
        self.assertEqual(query["iss"], [ISSUER])
        return query["code"][0]

    async def exchange(self, code: str, verifier: str, **extra: str) -> httpx.Response:
        return await self.http.post(
            "/oauth/token",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier, "client_id": "feedback", "client_secret": "s3cret", **extra},
        )

    async def user(self, username: str = "alice") -> User:
        async with self.sessions() as db:
            return (await db.execute(select(User).where(User.username == username))).scalar_one()


class AuthorizeTests(Base_):
    async def test_bad_client_or_redirect_never_redirects(self) -> None:
        _, challenge = pkce()
        for extra in ({"client_id": "nope"}, {"redirect_uri": "https://evil.test/cb"}, {"redirect_uri": CALLBACK + "/x"}):
            response = await self.http.get("/oauth/authorize", params=self.params(challenge, **extra))
            self.assertEqual(response.status_code, 400, extra)
            self.assertNotIn("location", response.headers)

    async def test_missing_pkce_or_state_and_bad_scope_are_refused(self) -> None:
        _, challenge = pkce()
        params = self.params(challenge)
        del params["code_challenge"]
        for query, error in (
            (params, "invalid_request"),
            (self.params(challenge, code_challenge_method="plain"), "invalid_request"),
            (self.params(challenge, state=""), "invalid_request"),
            (self.params(challenge, scope="openid admin"), "invalid_scope"),
            (self.params(challenge, response_type="token"), "unsupported_response_type"),
        ):
            response = await self.http.get("/oauth/authorize", params=query)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(parse_qs(urlsplit(response.headers["location"]).query)["error"], [error])

    async def test_prompt_none_without_session(self) -> None:
        _, challenge = pkce()
        response = await self.http.get("/oauth/authorize", params=self.params(challenge, prompt="none"))
        self.assertEqual(parse_qs(urlsplit(response.headers["location"]).query)["error"], ["login_required"])
        await self.login()
        response = await self.http.get("/oauth/authorize", params=self.params(challenge, prompt="none"))
        self.assertEqual(parse_qs(urlsplit(response.headers["location"]).query)["error"], ["consent_required"])

    async def test_loopback_port_only_for_public_clients(self) -> None:
        _, challenge = pkce()
        desktop = self.params(challenge, client_id="desktop", scope="openid", redirect_uri="http://127.0.0.1:53124/cb")
        self.assertEqual((await self.http.get("/oauth/authorize", params=desktop)).status_code, 302)
        self.assertEqual((await self.http.get("/oauth/authorize", params={**desktop, "redirect_uri": "http://127.0.0.1:53124/evil"})).status_code, 400)
        self.assertEqual((await self.http.get("/oauth/authorize", params={**desktop, "redirect_uri": "http://localhost:53124/cb"})).status_code, 400)

    async def test_approve_needs_session_and_same_origin(self) -> None:
        _, challenge = pkce()
        location = (await self.http.get("/oauth/authorize", params=self.params(challenge))).headers["location"]
        request_id = parse_qs(urlsplit(location).query)["request"][0]
        self.assertEqual((await self.http.post(f"/account/api/requests/{request_id}/approve", headers=ORIGIN)).status_code, 401)
        await self.login()
        self.assertEqual((await self.http.post(f"/account/api/requests/{request_id}/approve", headers={"Origin": "https://evil.test"})).status_code, 403)
        self.assertEqual((await self.http.post(f"/account/api/requests/{request_id}/approve")).status_code, 403)

    async def test_deny_returns_access_denied(self) -> None:
        _, challenge = pkce()
        location = (await self.http.get("/oauth/authorize", params=self.params(challenge))).headers["location"]
        request_id = parse_qs(urlsplit(location).query)["request"][0]
        denied = await self.http.post(f"/account/api/requests/{request_id}/deny", headers=ORIGIN)
        self.assertEqual(parse_qs(urlsplit(denied.json()["redirect_url"]).query)["error"], ["access_denied"])

    async def test_granted_user_skips_the_page(self) -> None:
        await self.login()
        verifier, challenge = pkce()
        await self.exchange(await self.get_code(challenge), verifier)
        response = await self.http.get("/oauth/authorize", params=self.params(challenge, prompt="none"))
        self.assertIn("code", parse_qs(urlsplit(response.headers["location"]).query))
        self.assertTrue(response.headers["location"].startswith(CALLBACK))

    async def test_prompt_login_needs_a_fresh_login(self) -> None:
        await self.login()
        _, challenge = pkce()
        location = (await self.http.get("/oauth/authorize", params=self.params(challenge, prompt="login"))).headers["location"]
        request_id = parse_qs(urlsplit(location).query)["request"][0]
        self.assertTrue((await self.http.get(f"/account/api/requests/{request_id}")).json()["needs_login"])
        self.assertEqual((await self.http.post(f"/account/api/requests/{request_id}/approve", headers=ORIGIN)).status_code, 401)
        await self.login()
        self.assertEqual((await self.http.post(f"/account/api/requests/{request_id}/approve", headers=ORIGIN)).status_code, 200)

    async def test_wrong_host_is_404(self) -> None:
        response = await self.http.get("/oauth/jwks", headers={"Host": "chat-api.test"})
        self.assertEqual(response.status_code, 404)


class TokenTests(Base_):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.login()

    async def test_full_flow(self) -> None:
        verifier, challenge = pkce()
        response = await self.exchange(await self.get_code(challenge), verifier)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        jwks = (await self.http.get("/oauth/jwks")).json()
        claims = jwt.decode(body["id_token"], jwks["keys"][0], algorithms=["ES256"], audience="feedback", issuer=ISSUER, options={"verify_at_hash": False})
        self.assertEqual(claims["nonce"], "nn")
        self.assertTrue(claims["sub"].startswith("acc_"))
        info = (await self.http.get("/oauth/userinfo", headers={"Authorization": f"Bearer {body['access_token']}"})).json()
        self.assertEqual(info["sub"], claims["sub"])
        self.assertEqual(info["preferred_username"], "alice")
        self.assertEqual(info["name"], "爱丽丝")
        self.assertEqual(info["email"], "a@example.com")

    async def test_pkce_and_redirect_and_secret_are_checked(self) -> None:
        verifier, challenge = pkce()
        self.assertEqual((await self.exchange(await self.get_code(challenge), secrets.token_urlsafe(48))).json()["error"], "invalid_grant")
        self.assertEqual((await self.exchange(await self.get_code(challenge), verifier, redirect_uri="https://feedback.test/other")).json()["error"], "invalid_grant")
        wrong_secret = await self.exchange(await self.get_code(challenge), verifier, client_secret="nope")
        self.assertEqual(wrong_secret.status_code, 401)
        self.assertEqual(wrong_secret.json()["error"], "invalid_client")

    async def test_basic_auth(self) -> None:
        verifier, challenge = pkce()
        code = await self.get_code(challenge)
        response = await self.http.post(
            "/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier}, auth=("feedback", "s3cret")
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def test_code_replay_revokes_tokens(self) -> None:
        verifier, challenge = pkce()
        code = await self.get_code(challenge)
        first = (await self.exchange(code, verifier)).json()
        self.assertEqual((await self.exchange(code, verifier)).json()["error"], "invalid_grant")
        refreshed = await self.http.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": "feedback", "client_secret": "s3cret"})
        self.assertEqual(refreshed.json()["error"], "invalid_grant")

    async def test_concurrent_exchange_has_one_winner(self) -> None:
        verifier, challenge = pkce()
        code = await self.get_code(challenge)
        results = await asyncio.gather(self.exchange(code, verifier), self.exchange(code, verifier))
        self.assertEqual(sorted(r.status_code for r in results), [200, 400])

    async def test_expired_code(self) -> None:
        verifier, challenge = pkce()
        code = await self.get_code(challenge)
        with patch.object(service, "_now", return_value=datetime.now(UTC) + timedelta(seconds=61)):
            self.assertEqual((await self.exchange(code, verifier)).json()["error"], "invalid_grant")

    async def test_refresh_rotates_and_reuse_revokes_family(self) -> None:
        verifier, challenge = pkce()
        first = (await self.exchange(await self.get_code(challenge), verifier)).json()
        form = {"grant_type": "refresh_token", "client_id": "feedback", "client_secret": "s3cret"}
        second = (await self.http.post("/oauth/token", data={**form, "refresh_token": first["refresh_token"]})).json()
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])
        self.assertEqual((await self.http.post("/oauth/token", data={**form, "refresh_token": first["refresh_token"]})).json()["error"], "invalid_grant")
        self.assertEqual((await self.http.post("/oauth/token", data={**form, "refresh_token": second["refresh_token"]})).json()["error"], "invalid_grant")

    async def test_refresh_token_of_another_client(self) -> None:
        verifier, challenge = pkce()
        first = (await self.exchange(await self.get_code(challenge), verifier)).json()
        response = await self.http.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "desktop", "refresh_token": first["refresh_token"]})
        self.assertEqual(response.json()["error"], "invalid_grant")

    async def test_revoke(self) -> None:
        verifier, challenge = pkce()
        first = (await self.exchange(await self.get_code(challenge), verifier)).json()
        self.assertEqual((await self.http.post("/oauth/revoke", data={"token": first["refresh_token"], "client_id": "feedback", "client_secret": "s3cret"})).status_code, 200)
        response = await self.http.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "feedback", "client_secret": "s3cret", "refresh_token": first["refresh_token"]})
        self.assertEqual(response.json()["error"], "invalid_grant")

    async def test_tokens_do_not_cross_with_chat(self) -> None:
        verifier, challenge = pkce()
        body = (await self.exchange(await self.get_code(challenge), verifier)).json()
        self.assertEqual((await self.http.get("/chat/me", headers={"Authorization": f"Bearer {body['access_token']}"})).status_code, 401)
        self.assertEqual((await self.http.get("/chat/me", headers={"Authorization": f"Bearer {body['id_token']}"})).status_code, 401)
        user = await self.user()
        self.assertEqual((await self.http.get("/oauth/userinfo", headers={"Authorization": f"Bearer {create_access_token(user.id)}"})).status_code, 401)
        # id_token 不能当 access token 用
        self.assertEqual((await self.http.get("/oauth/userinfo", headers={"Authorization": f"Bearer {body['id_token']}"})).status_code, 401)

    async def test_unverified_email_never_leaves(self) -> None:
        async with self.sessions() as db:
            user = (await db.execute(select(User).where(User.username == "alice"))).scalar_one()
            user.email_verified_at = None
            user.ccw_student_oid = "a" * 24
            await db.commit()
        verifier, challenge = pkce()
        body = (await self.exchange(await self.get_code(challenge), verifier)).json()
        info = (await self.http.get("/oauth/userinfo", headers={"Authorization": f"Bearer {body['access_token']}"})).json()
        self.assertNotIn("email", info)
        self.assertNotIn("ccw_student_oid", info)  # 没要 ccw 这个 scope

    async def test_logout_needs_our_id_token_and_registered_target(self) -> None:
        verifier, challenge = pkce()
        body = (await self.exchange(await self.get_code(challenge), verifier)).json()
        self.assertEqual((await self.http.get("/oauth/logout")).status_code, 400)
        self.assertEqual((await self.http.get("/oauth/logout", params={"id_token_hint": body["id_token"], "post_logout_redirect_uri": "https://evil.test/"})).status_code, 400)
        response = await self.http.get("/oauth/logout", params={"id_token_hint": body["id_token"], "post_logout_redirect_uri": "https://feedback.test/bye"})
        self.assertEqual(response.headers["location"], "https://feedback.test/bye")
        self.http.cookies.clear()
        self.assertEqual((await self.http.get("/account/api/me")).status_code, 401)


class BanTests(Base_):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()

        async def admin() -> User:
            async with self.sessions() as db:
                return (await db.execute(select(User).where(User.username == "root"))).scalar_one()

        self.api.dependency_overrides[get_current_admin] = admin
        async with self.sessions() as db:
            db.add(User(username="root", hashed_password="x", platform_role=PlatformRole.ADMIN, registration_ip="10.0.0.9"))
            await db.commit()

    async def test_chat_ban_does_not_block_oauth(self) -> None:
        async with self.sessions() as db:
            user = (await db.execute(select(User).where(User.username == "alice"))).scalar_one()
            user.status = UserStatus.DISABLED
            user.ban_reason = "聊天违规"
            await db.commit()
        self.assertEqual((await self.login()).status_code, 200)
        verifier, challenge = pkce()
        self.assertEqual((await self.exchange(await self.get_code(challenge), verifier)).status_code, 200)

    async def test_account_ban_blocks_everything_and_does_not_touch_chat_status(self) -> None:
        await self.login()
        verifier, challenge = pkce()
        body = (await self.exchange(await self.get_code(challenge), verifier)).json()
        user = await self.user()
        self.assertEqual((await self.http.put(f"/api/v1/admin/kukeauth/users/{user.id}/ban", json={"reason": "瞎搞"})).status_code, 200)
        self.assertEqual((await self.http.get("/account/api/me")).status_code, 401)
        self.assertEqual((await self.http.get("/oauth/userinfo", headers={"Authorization": f"Bearer {body['access_token']}"})).status_code, 401)
        refreshed = await self.http.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "feedback", "client_secret": "s3cret", "refresh_token": body["refresh_token"]})
        self.assertEqual(refreshed.json()["error"], "invalid_grant")
        login = await self.login()
        self.assertEqual(login.status_code, 403)
        self.assertEqual(login.json()["detail"]["reason"], "瞎搞")
        self.assertEqual((await self.user()).status, UserStatus.ACTIVE)

        self.assertEqual((await self.http.delete(f"/api/v1/admin/kukeauth/users/{user.id}/ban")).status_code, 200)
        self.assertEqual((await self.login()).status_code, 200)

    async def test_expired_ban_stops_applying(self) -> None:
        user = await self.user()
        until = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        await self.http.put(f"/api/v1/admin/kukeauth/users/{user.id}/ban", json={"reason": "x", "until": until})
        self.assertEqual((await self.login()).status_code, 200)

    async def test_client_admin(self) -> None:
        created = await self.http.post("/api/v1/admin/kukeauth/clients", json={"client_id": "svc", "name": "服务", "redirect_uris": ["https://svc.test/cb"]})
        self.assertEqual(created.status_code, 201, created.text)
        secret = created.json()["client_secret"]
        self.assertTrue(secret.startswith("kcs_"))
        async with self.sessions() as db:
            row = (await db.execute(select(OAuthClient).where(OAuthClient.client_id == "svc"))).scalar_one()
            self.assertEqual(row.secret_hash, service.hash_secret(secret))
        self.assertNotIn("client_secret", (await self.http.get("/api/v1/admin/kukeauth/clients")).json()["items"][-1])
        self.assertEqual((await self.http.post("/api/v1/admin/kukeauth/clients", json={"client_id": "b", "name": "x", "redirect_uris": ["http://evil.test/cb"]})).status_code, 422)
        self.assertEqual((await self.http.post("/api/v1/admin/kukeauth/clients", json={"client_id": "bad-scope", "name": "x", "redirect_uris": ["https://a.test/"], "allowed_scopes": ["admin"]})).status_code, 422)


class AccountTests(Base_):
    async def register(self, username: str = "bobby", ip: str = "10.0.0.2", **extra: str) -> httpx.Response:
        return await self.http.post(
            "/account/api/register", json={"username": username, "password": "password-2", "nickname": "鲍比", **extra}, headers={**ORIGIN, "X-Real-IP": ip}
        )

    async def test_register_logs_in_but_cannot_authorize_until_verified(self) -> None:
        response = await self.register()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(response.json()["verified"])
        self.assertTrue((await self.user("bobby")).official_group_pending)
        self.assertEqual((await self.http.get("/account/api/me")).json()["username"], "bobby")
        cookie = next(iter(self.http.cookies.jar))
        self.assertEqual(cookie.name, "__Host-kuke_auth")
        self.assertTrue(cookie.secure)
        self.assertFalse(cookie.domain_specified)

        _, challenge = pkce()
        location = (await self.http.get("/oauth/authorize", params=self.params(challenge))).headers["location"]
        request_id = parse_qs(urlsplit(location).query)["request"][0]
        self.assertTrue((await self.http.get(f"/account/api/requests/{request_id}")).json()["needs_verification"])
        refused = await self.http.post(f"/account/api/requests/{request_id}/approve", headers=ORIGIN)
        self.assertEqual(refused.json()["detail"]["code"], "verification_required")

    async def test_one_account_per_ip_stays(self) -> None:
        self.assertEqual((await self.register()).status_code, 201)
        self.http.cookies.clear()
        self.assertEqual((await self.register("carol")).status_code, 409)
        self.assertEqual((await self.register("carol", "10.0.1.2")).status_code, 201)

    async def test_forwarded_for_cannot_bypass_the_ip_rule(self) -> None:
        headers = {**ORIGIN, "X-Real-IP": "10.0.0.2"}
        for name, forged in (("dave", "1.1.1.1"), ("erin", "2.2.2.2")):
            response = await self.http.post("/account/api/register", json={"username": name, "password": "password-2"}, headers={**headers, "X-Forwarded-For": forged})
            self.assertEqual(response.status_code, 201 if name == "dave" else 409)

    async def test_short_password_and_bad_origin(self) -> None:
        self.assertEqual((await self.http.post("/account/api/register", json={"username": "shorty", "password": "1234567"}, headers=ORIGIN)).status_code, 422)
        self.assertEqual((await self.http.post("/account/api/register", json={"username": "shorty", "password": "password-2"})).status_code, 403)

    async def test_web_signup_joins_official_group_at_first_chat_login(self) -> None:
        await self.register()
        calls: list[int] = []

        async def fake_add(_db, user_id: int) -> None:
            calls.append(user_id)

        async with self.sessions() as db:
            user = (await db.execute(select(User).where(User.username == "bobby"))).scalar_one()
            with patch.object(auth_service, "add_user_to_official_group", fake_add):
                await auth_service.join_pending_official_group(db, user)
                await auth_service.join_pending_official_group(db, user)
        self.assertEqual(len(calls), 1)
        self.assertFalse((await self.user("bobby")).official_group_pending)

    async def test_login_rate_limit(self) -> None:
        for _ in range(10):
            self.assertEqual((await self.login(password="wrong-pass")).status_code, 401)
        limited = await self.login(password="wrong-pass")
        self.assertEqual(limited.status_code, 429)
        self.assertIn("retry-after", limited.headers)

    async def test_edit_profile(self) -> None:
        await self.login()
        response = await self.http.patch("/account/api/me", json={"nickname": "新名字", "bio": "你好"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["nickname"], "新名字")
        self.assertEqual(response.json()["bio"], "你好")
        # 外站图片地址不能当头像
        self.assertEqual((await self.http.patch("/account/api/me", json={"avatar_url": "https://evil.test/a.png"}, headers=ORIGIN)).status_code, 422)
        # 只能改这三项，别的字段不生效
        await self.http.patch("/account/api/me", json={"platform_role": "ADMIN", "username": "root2"}, headers=ORIGIN)
        user = await self.user()
        self.assertEqual(user.platform_role, PlatformRole.USER)

    async def test_change_password_keeps_this_session_only(self) -> None:
        await self.login()
        other = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api), base_url=ISSUER)
        await other.post("/account/api/login", json={"username": "alice", "password": "password-1"}, headers=ORIGIN)
        self.assertEqual((await self.http.post("/account/api/me/password", json={"current_password": "wrong", "new_password": "password-9"}, headers=ORIGIN)).status_code, 400)
        self.assertEqual((await self.http.post("/account/api/me/password", json={"current_password": "password-1", "new_password": "password-9"}, headers=ORIGIN)).status_code, 200)
        self.assertEqual((await self.http.get("/account/api/me")).status_code, 200)
        self.assertEqual((await other.get("/account/api/me")).status_code, 401)
        await other.aclose()
        self.assertEqual((await self.login(password="password-9")).status_code, 200)

    async def test_email_verification(self) -> None:
        await self.register()
        sent: list[tuple[str, str]] = []

        def fake_send(receiver: str, _subject: str, body: str) -> tuple[bool, None]:
            sent.append((receiver, body))
            return True, None

        with patch("app.api.kukeauth.send_email_internal", fake_send):
            self.assertEqual((await self.http.post("/account/api/email/send", json={"email": "Bob@Example.com"}, headers=ORIGIN)).status_code, 200)
            # 同一个邮箱 60 秒内只发一次
            self.assertEqual((await self.http.post("/account/api/email/send", json={"email": "bob@example.com"}, headers=ORIGIN)).status_code, 429)
        code = re.search(r"\d{6}", sent[0][1]).group()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assertEqual((await self.http.post("/account/api/email/verify", json={"email": "bob@example.com", "code": wrong}, headers=ORIGIN)).status_code, 400)
        # 错满 5 次，正确的码也不行了
        self.assertEqual((await self.http.post("/account/api/email/verify", json={"email": "bob@example.com", "code": code}, headers=ORIGIN)).status_code, 400)

        rate_limit.reset()
        sent.clear()
        with patch("app.api.kukeauth.send_email_internal", fake_send):
            await self.http.post("/account/api/email/send", json={"email": "bob@example.com"}, headers=ORIGIN)
        code = re.search(r"\d{6}", sent[0][1]).group()
        verified = await self.http.post("/account/api/email/verify", json={"email": "bob@example.com", "code": code}, headers=ORIGIN)
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertTrue(verified.json()["verified"])
        self.assertEqual(verified.json()["email"], "bob@example.com")

    async def test_verified_email_wins_over_squatter(self) -> None:
        async with self.sessions() as db:
            db.add(User(username="squatter", hashed_password="x", email="bob@example.com", registration_ip="10.0.0.7"))
            await db.commit()
            bob = User(username="real", hashed_password="x", registration_ip="10.0.0.8")
            db.add(bob)
            await db.commit()
            await service.apply_verified_email(db, bob, "bob@example.com")
        self.assertIsNone((await self.user("squatter")).email)
        self.assertEqual((await self.user("real")).email, "bob@example.com")
        async with self.sessions() as db:
            third = User(username="third", hashed_password="x", registration_ip="10.0.0.10")
            db.add(third)
            await db.commit()
            with self.assertRaises(Exception) as caught:
                await service.apply_verified_email(db, third, "bob@example.com")
            self.assertEqual(caught.exception.status_code, 409)

    async def test_email_reset_does_not_reveal_accounts_and_revokes_sessions(self) -> None:
        await self.login()
        sent: list[str] = []

        def fake_send(_receiver: str, _subject: str, body: str) -> tuple[bool, None]:
            sent.append(body)
            return True, None

        with patch("app.api.kukeauth.send_email_internal", fake_send):
            missing = await self.http.post("/account/api/password-reset/email/send", json={"email": "nobody@example.com"}, headers=ORIGIN)
            found = await self.http.post("/account/api/password-reset/email/send", json={"email": "a@example.com"}, headers=ORIGIN)
        self.assertEqual((missing.status_code, missing.json()), (found.status_code, found.json()))
        self.assertEqual(len(sent), 1)
        code = re.search(r"\d{6}", sent[0]).group()
        confirmed = await self.http.post("/account/api/password-reset/email/confirm", json={"email": "a@example.com", "code": code, "new_password": "password-7"}, headers=ORIGIN)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual((await self.http.get("/account/api/me")).status_code, 401)
        self.assertEqual((await self.login(password="password-7")).status_code, 200)

    async def test_grants_page_and_revoke(self) -> None:
        await self.login()
        verifier, challenge = pkce()
        body = (await self.exchange(await self.get_code(challenge), verifier)).json()
        items = (await self.http.get("/account/api/me/grants")).json()["items"]
        self.assertEqual([item["client_id"] for item in items], ["feedback"])
        self.assertEqual((await self.http.delete("/account/api/me/grants/feedback", headers=ORIGIN)).status_code, 200)
        self.assertEqual((await self.http.get("/account/api/me/grants")).json()["items"], [])
        refreshed = await self.http.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "feedback", "client_secret": "s3cret", "refresh_token": body["refresh_token"]})
        self.assertEqual(refreshed.json()["error"], "invalid_grant")
        async with self.sessions() as db:
            self.assertTrue((await db.execute(select(OAuthRefreshToken))).scalars().first().revoked_at)

    async def bind_ccw(self, username: str = "alice", oid: str = "a" * 24) -> None:
        async with self.sessions() as db:
            user = (await db.execute(select(User).where(User.username == username))).scalar_one()
            challenge = CcwBindingChallenge(user_id=user.id, code="ABCD-EFGH", subject_oid="b" * 24, expires_at=datetime.now(UTC) + timedelta(minutes=5))
            db.add(challenge)
            await db.flush()
            comments = [{"id": 1, "content": "abcd-efgh", "commenter": {"accountOid": oid, "name": "CCW 名字"}}]
            with patch.object(ccw_service, "fetch_student_payloads", side_effect=ccw_service.CcwUnavailable("离线")):
                await ccw_service._match_challenge(db, challenge, comments)
            await db.commit()
            self.last_challenge_status = challenge.status
            await db.delete(challenge)
            await db.commit()

    async def test_connections_list(self) -> None:
        await self.login()
        item = (await self.http.get("/account/api/connections")).json()["items"][0]
        self.assertEqual((item["provider"], item["connected"], item["account"], item["can_unbind"]), ("ccw", False, None, False))
        await self.bind_ccw()
        item = (await self.http.get("/account/api/connections")).json()["items"][0]
        self.assertTrue(item["connected"])
        self.assertEqual(item["account"]["name"], "CCW 名字")
        self.assertEqual(item["account"]["url"], f"https://www.ccw.site/student/{'a' * 24}")
        self.assertNotIn("student_oid", item["account"])

    async def test_unbind_needs_password_and_same_origin(self) -> None:
        await self.login()
        await self.bind_ccw()
        url = "/account/api/connections/ccw/unbind"
        self.assertEqual((await self.http.post(url, json={"password": "password-1"})).status_code, 403)
        wrong = await self.http.post(url, json={"password": "nope"}, headers=ORIGIN)
        self.assertEqual(wrong.json()["detail"]["code"], "bad_password")
        self.assertTrue((await self.user()).ccw_student_oid)
        done = await self.http.post(url, json={"password": "password-1"}, headers=ORIGIN)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertIsNone(done.json()["me"]["ccw"])
        self.assertFalse(done.json()["items"][0]["connected"])
        self.assertIsNone((await self.user()).ccw_name)
        self.assertEqual((await self.http.post(url, json={"password": "password-1"}, headers=ORIGIN)).status_code, 404)
        self.assertEqual((await self.http.post("/account/api/connections/github/unbind", json={"password": "password-1"}, headers=ORIGIN)).status_code, 404)

    async def test_cannot_unbind_the_only_verification(self) -> None:
        self.assertEqual((await self.register()).status_code, 201)
        await self.bind_ccw("bobby")
        self.assertTrue((await self.http.get("/account/api/me")).json()["verified"])
        item = (await self.http.get("/account/api/connections")).json()["items"][0]
        self.assertFalse(item["can_unbind"])
        refused = await self.http.post("/account/api/connections/ccw/unbind", json={"password": "password-2"}, headers=ORIGIN)
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "last_method"))
        self.assertTrue((await self.user("bobby")).ccw_student_oid)

    async def test_unbound_ccw_can_move_to_another_account(self) -> None:
        self.assertEqual((await self.register()).status_code, 201)
        await self.bind_ccw("alice")
        await self.bind_ccw("bobby")
        self.assertEqual(self.last_challenge_status, "conflict")
        self.assertIsNone((await self.user("bobby")).ccw_student_oid)
        self.http.cookies.clear()
        await self.login()
        self.assertEqual((await self.http.post("/account/api/connections/ccw/unbind", json={"password": "password-1"}, headers=ORIGIN)).status_code, 200)
        await self.bind_ccw("bobby")
        self.assertEqual(self.last_challenge_status, "verified")
        self.assertEqual((await self.user("bobby")).ccw_student_oid, "a" * 24)

    async def test_discovery_and_unconfigured(self) -> None:
        doc = (await self.http.get("/.well-known/openid-configuration")).json()
        self.assertEqual(doc["issuer"], ISSUER)
        self.assertEqual(doc["code_challenge_methods_supported"], ["S256"])
        with patch.object(settings, "kukeauth_signing_key", None):
            self.assertEqual((await self.http.get("/oauth/jwks")).status_code, 503)


class DeveloperTests(Base_):
    """开发者在账号站自助建应用：建出来就能走完整流程，授权页标出开发者，删掉后令牌失效。"""

    APP = {"name": "小工具", "redirect_uris": ["https://tool.test/cb"], "allowed_scopes": ["openid", "profile"]}

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.sessions() as db:
            db.add(User(username="bobby", hashed_password=hash_password("password-2"), registration_ip="10.0.0.2"))
            db.add(User(username="carol", hashed_password=hash_password("password-3"), registration_ip="10.0.0.3", email_verified_at=datetime.now(UTC), email="c@example.com"))
            await db.commit()

    async def create(self, **extra) -> httpx.Response:  # noqa: ANN003
        return await self.http.post("/account/api/developer/apps", json={**self.APP, **extra}, headers=ORIGIN)

    async def flow(self, client_id: str, secret: str | None) -> dict:
        verifier, challenge = pkce()
        params = {**self.params(challenge), "client_id": client_id, "redirect_uri": "https://tool.test/cb", "scope": "openid profile"}
        location = (await self.http.get("/oauth/authorize", params=params)).headers["location"]
        info = None
        if "/continue?" in location:
            request_id = parse_qs(urlsplit(location).query)["request"][0]
            info = (await self.http.get(f"/account/api/requests/{request_id}")).json()
            location = (await self.http.post(f"/account/api/requests/{request_id}/approve", headers=ORIGIN)).json()["redirect_url"]
        code = parse_qs(urlsplit(location).query)["code"][0]
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": "https://tool.test/cb", "code_verifier": verifier, "client_id": client_id}
        if secret:
            data["client_secret"] = secret
        token = await self.http.post("/oauth/token", data=data)
        return {"info": info, "token": token}

    async def test_create_and_sign_in(self) -> None:
        await self.login()
        created = await self.create(homepage_url="https://tool.test")
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertTrue(body["client_id"].startswith("app-"))
        self.assertTrue(body["client_secret"].startswith("kcs_"))
        self.assertFalse(body["public"])
        listed = (await self.http.get("/account/api/developer/apps")).json()
        self.assertEqual([item["client_id"] for item in listed["items"]], [body["client_id"]])
        self.assertNotIn("client_secret", listed["items"][0])

        result = await self.flow(body["client_id"], body["client_secret"])
        client = result["info"]["client"]
        self.assertFalse(client["official"])
        self.assertEqual(client["developer"]["username"], "alice")
        self.assertEqual(client["homepage_url"], "https://tool.test")
        self.assertEqual(result["token"].status_code, 200, result["token"].text)
        access = result["token"].json()["access_token"]
        self.assertEqual((await self.http.get("/oauth/userinfo", headers={"Authorization": f"Bearer {access}"})).json()["preferred_username"], "alice")
        grants = (await self.http.get("/account/api/me/grants")).json()["items"]
        self.assertFalse(next(item for item in grants if item["client_id"] == body["client_id"])["official"])

        # 删掉以后：还没过期的 access token 也读不到资料，授权记录一起没了
        self.assertEqual((await self.http.delete(f"/account/api/developer/apps/{body['client_id']}", headers=ORIGIN)).status_code, 200)
        self.assertEqual((await self.http.get("/oauth/userinfo", headers={"Authorization": f"Bearer {access}"})).status_code, 401)
        self.assertEqual((await self.http.get("/account/api/me/grants")).json()["items"], [])
        self.assertEqual((await self.http.get("/account/api/developer/apps")).json()["items"], [])

    async def test_official_clients_stay_official(self) -> None:
        await self.login()
        _verifier, challenge = pkce()
        location = (await self.http.get("/oauth/authorize", params=self.params(challenge))).headers["location"]
        request_id = parse_qs(urlsplit(location).query)["request"][0]
        client = (await self.http.get(f"/account/api/requests/{request_id}")).json()["client"]
        self.assertTrue(client["official"])
        self.assertIsNone(client["developer"])

    async def test_public_app_has_no_secret(self) -> None:
        await self.login()
        body = (await self.create(public=True, redirect_uris=["http://127.0.0.1/cb"])).json()
        self.assertIsNone(body["client_secret"])
        self.assertTrue(body["public"])
        self.assertEqual((await self.http.post(f"/account/api/developer/apps/{body['client_id']}/rotate-secret", headers=ORIGIN)).status_code, 400)

    async def test_rotate_secret(self) -> None:
        await self.login()
        body = (await self.create()).json()
        rotated = await self.http.post(f"/account/api/developer/apps/{body['client_id']}/rotate-secret", headers=ORIGIN)
        self.assertEqual(rotated.status_code, 200)
        self.assertEqual((await self.flow(body["client_id"], body["client_secret"]))["token"].status_code, 401)
        self.assertEqual((await self.flow(body["client_id"], rotated.json()["client_secret"]))["token"].status_code, 200)

    async def test_needs_verified_account_and_same_origin(self) -> None:
        self.assertEqual((await self.http.get("/account/api/developer/apps")).status_code, 401)
        await self.login("bobby", "password-2")
        self.assertFalse((await self.http.get("/account/api/developer/apps")).json()["can_create"])
        refused = await self.create()
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()["detail"]["code"], "verification_required")
        self.http.cookies.clear()
        await self.login()
        self.assertEqual((await self.http.post("/account/api/developer/apps", json=self.APP)).status_code, 403)
        self.assertEqual((await self.http.post("/account/api/developer/apps", json=self.APP, headers={"Origin": "https://evil.test"})).status_code, 403)

    async def test_validation(self) -> None:
        await self.login()
        for extra in (
            {"redirect_uris": ["http://tool.test/cb"]},
            {"redirect_uris": ["https://tool.test/cb#x"]},
            {"redirect_uris": ["https://user:pw@tool.test/cb"]},
            {"redirect_uris": ["javascript:alert(1)"]},
            {"redirect_uris": ["  "]},
            {"post_logout_redirect_uris": ["http://tool.test/bye"]},
            {"allowed_scopes": ["openid", "admin"]},
            {"allowed_scopes": []},
            {"name": "KukeAuth 官方"},
            {"name": "   "},
            {"homepage_url": "http://tool.test"},
            {"icon_url": "javascript:alert(1)"},
        ):
            response = await self.create(**extra)
            self.assertEqual(response.status_code, 422, (extra, response.text))
        self.assertEqual((await self.http.get("/account/api/developer/apps")).json()["items"], [])

    async def test_cannot_touch_other_peoples_apps(self) -> None:
        await self.login()
        body = (await self.create()).json()
        self.http.cookies.clear()
        await self.login("carol", "password-3")
        path = f"/account/api/developer/apps/{body['client_id']}"
        self.assertEqual((await self.http.patch(path, json={"name": "抢走"}, headers=ORIGIN)).status_code, 404)
        self.assertEqual((await self.http.post(f"{path}/rotate-secret", headers=ORIGIN)).status_code, 404)
        self.assertEqual((await self.http.delete(path, headers=ORIGIN)).status_code, 404)
        self.assertEqual((await self.http.get("/account/api/developer/apps")).json()["items"], [])
        # 后台建的官方应用也动不了
        self.assertEqual((await self.http.delete("/account/api/developer/apps/feedback", headers=ORIGIN)).status_code, 404)

    async def test_update_and_scope_change_applies(self) -> None:
        await self.login()
        body = (await self.create(homepage_url="https://tool.test")).json()
        path = f"/account/api/developer/apps/{body['client_id']}"
        updated = await self.http.patch(path, json={"name": "新名字", "allowed_scopes": ["openid"], "homepage_url": None}, headers=ORIGIN)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["name"], "新名字")
        self.assertEqual(updated.json()["allowed_scopes"], ["openid"])
        self.assertIsNone(updated.json()["homepage_url"])
        self.assertEqual(updated.json()["redirect_uris"], ["https://tool.test/cb"])
        _verifier, challenge = pkce()
        params = {**self.params(challenge), "client_id": body["client_id"], "redirect_uri": "https://tool.test/cb", "scope": "openid profile"}
        location = (await self.http.get("/oauth/authorize", params=params)).headers["location"]
        self.assertIn("error=invalid_scope", location)
        # 公开 / 机密在建好后不能改
        self.assertFalse((await self.http.patch(path, json={"public": True}, headers=ORIGIN)).json()["public"])

    async def test_admin_disable_blocks_and_owner_cannot_reenable(self) -> None:
        await self.login()
        body = (await self.create()).json()
        async with self.sessions() as db:
            client = (await db.execute(select(OAuthClient).where(OAuthClient.client_id == body["client_id"]))).scalar_one()
            client.disabled = True
            await db.commit()
        _verifier, challenge = pkce()
        params = {**self.params(challenge), "client_id": body["client_id"], "redirect_uri": "https://tool.test/cb"}
        self.assertEqual((await self.http.get("/oauth/authorize", params=params)).status_code, 400)
        path = f"/account/api/developer/apps/{body['client_id']}"
        self.assertTrue((await self.http.patch(path, json={"disabled": False}, headers=ORIGIN)).json()["disabled"])

    async def test_limit(self) -> None:
        await self.login()
        for _ in range(service.MAX_DEVELOPER_CLIENTS):
            self.assertEqual((await self.create()).status_code, 201)
        over = await self.create()
        self.assertEqual(over.status_code, 409)
        self.assertEqual(over.json()["detail"]["code"], "app_limit")
