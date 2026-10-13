#!/usr/bin/env python
"""对 scripts/jury_demo.py 起的联调实例做一遍 HTTP 自测（docs/jury-v2.md 第 1、2、3 节）。

    python scripts/jury_demo.py              # 先在另一个终端把服务起起来
    python scripts/jury_demo_check.py        # 再跑这个
    python scripts/jury_demo_check.py --base http://127.0.0.1:8793

每个请求都带客户端证明头（算法见 app/core/request_guard.py）。检查分三种结果：
  PASS / FAIL  接口能不能用、关键字段对不对
  NOTE         和契约对不上、或者客户端容易踩的地方（不算失败，汇总在最后）

委员侧接口里的 case_id 是「评审编号」（每人每案不同），公示栏和后台的是案件的真实编号，两者不能互相代入
（契约 1.0）。这里凡是调委员侧接口用的都是从委员侧接口拿到的编号。

注意：这个脚本会投票、考试、练习、发观点、改开关、改判案件、任命委员，还会投合议票和复审票、申诉、申请复议、标判例、
在议事厅发帖表决。跑完之后用
`python scripts/jury_demo.py --reset` 重建，才是留给客户端的干净初始数据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from jury_demo import ACCOUNTS, DEFAULT_PORT, DEMO_TARGETS, PASSWORD

UTC = timezone.utc
PROOF_SALT = "KukeChat"
NAIVE_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")

# docs/jury-v2.md 第 1、2、3 节里写明的字段
RULE_KEYS = {
    "seats_per_case", "quick_consensus_votes", "full_consensus_votes", "timeout_hours", "assignment_ttl_hours", "daily_votes", "term_days",
    "context_near", "content_retention_days", "publish_delay_hours", "min_sample", "max_ban_hours", "severe_min_votes", "need_context_votes",
}
CONTENT_KEYS = {"case_id", "kind", "reasons", "report_count", "messages", "post", "case_origin"}
QUESTION_KEYS = {"kind", "reasons", "report_count", "messages", "post"}
PUBLIC_KEYS = {
    "case_id", "kind", "reasons", "report_count", "outcome", "category", "category_label", "level", "source", "tally", "votes", "closed_at", "hours",
    "overturned", "has_content", "case_origin", "precedent_id",
}
STATUS_KEYS = {
    "status", "eligible", "requirements", "term_ends_at", "review_note", "stats", "daily_limit", "voted_today", "tier", "available_cases", "restriction",
    "exam", "exam_required", "points", "tier_progress", "badge_visible", "warnings", "cooldown_until", "recuse_left_today", "daily_resets_at", "recuse_resets_at", "senior",
}
CONTRACT: dict[str, set[str]] = {
    "config": {
        "version", "capabilities", "switches", "tiers", "votes", "rules", "opinions", "eligibility",
        "categories", "points", "suspend", "exam", "practice", "recuse", "abstain", "panel", "appeals", "reconsider", "hall",
    },
    "config.panel": {"seats", "consensus_votes", "timeout_hours"},
    "config.appeals": {"seats", "consensus_votes", "timeout_hours"},
    "config.reconsider": {"window_days", "text_min"},
    "config.hall": {
        "title_max", "content_max", "comment_max", "posts_per_day", "comments_per_day", "vote_days", "quorum_ratio", "quorum_min", "pass_num", "pass_den", "pass_ratio",
        "guidelines", "report_reasons", "auto_hide_reports", "reports_per_day",
    },
    "config.switches": {"auto_appoint", "appeals", "machine_review"},
    "config.rules": RULE_KEYS,
    "config.opinions": {"max_length", "per_case", "per_minute", "per_day", "auto_hide_reports", "report_reasons", "like_reward_threshold"},
    "config.eligibility": {"min_account_age_days", "min_recent_messages", "recent_activity_days"},
    "config.tiers[]": {"key", "label", "requirements", "demote_below"},
    "config.tiers[].requirements": {"votes_settled", "agreement_rate", "quality_rate", "quality_min", "days_served"},
    "config.tiers[].demote_below": {"agreement_rate", "window", "min_votes", "to"},
    "config.categories[]": {"key", "label", "default_level"},
    "config.points": {
        "vote_agreed", "vote_disagreed", "quality_correct", "quality_wrong", "daily_first", "streak_7", "opinion_liked", "exam_passed", "overturn_adjust",
        "panel_agreed", "panel_disagreed", "review_vote",
    },
    "config.suspend": {"min_settled", "agreement_rate", "quality_min", "quality_rate"},
    "config.exam": {"total", "pass_score", "time_limit_minutes", "grace_seconds", "attempts_max", "window_days", "retry_hours", "valid_days"},
    "config.practice": {"daily_limit"},
    "config.recuse": {"daily_limit"},
    "config.abstain": {"window", "min_votes", "warn_rate", "cooldown_rate", "cooldown_hours"},
    "JuryStatus": STATUS_KEYS,
    "JuryStatus.exam": {"available", "passed", "passed_at", "attempts_used", "attempts_max", "next_attempt_at", "last_score", "pass_score", "total"},
    "JuryStatus.tier_progress": {"next_tier", "requirements"},
    "JuryStatus.warnings[]": {"code", "message", "current", "threshold", "sample"},
    "overview": {
        "jurors", "backlog", "settled", "window_days", "sample", "sample_enough", "median_hours", "consensus_rate", "violation_rate", "overturn_rate",
        "counts", "me", "daily", "generated_at",
    },
    "overview.jurors": {"total", "trainee", "regular", "senior"},
    "overview.backlog": {"jury", "panel"},
    "overview.settled": {"d7", "d30", "total"},
    "overview.counts": {"consensus", "escalated", "violation", "clean", "overturned"},
    "overview.me": {"status", "tier", "available_cases", "voted_today", "daily_limit"},
    "overview.daily[]": {"date", "queued", "settled"},
    "JuryCaseView": CONTENT_KEYS | {"assigned_at", "expires_at", "suggested_category"},
    "messages[]": {"speaker", "is_target", "type", "content", "images", "created_at", "offset"},
    "post": {"content", "image_urls", "created_at"},
    "vote result": {"case_id", "settled", "case", "reward"},
    "reward": {"points", "total", "items"},
    "reward.items[]": {"kind", "points", "label"},
    "JuryCaseDetail": CONTENT_KEYS
    | {"content_available", "my_vote", "stage", "votes_cast", "tally", "verdict", "is_quality_check", "opinions_enabled", "opinion_count", "public_case_id", "precedent", "can_mark_precedent"},
    "JuryCaseDetail.precedent": {"id", "title", "mine"},
    "PanelCaseView": CONTENT_KEYS | {"assigned_at", "expires_at", "suggested_category", "origin", "panel"},
    "PanelCaseView.origin": {"reason", "tally", "opinions"},
    "PanelCaseView.panel": {"seats", "votes_cast", "expires_at"},
    "tally.panel": {"none", "l1", "l2", "l3"},
    "AppealReviewView": {
        "review_id", "type", "kind", "source", "category", "category_label", "level", "penalty", "messages", "post", "content", "origin", "appeal_text", "appealed_at",
        "expires_at", "seats", "votes_cast", "verdict", "reconsider_text", "requested_at",
    },
    "AppealReviewView.penalty": {"kind", "hours"},
    "AppealReviewView.content": {"text", "image_count"},
    "AppealReviewView.origin": {"tally", "opinions"},
    "AppealReviewView.verdict": {"outcome", "source", "closed_at"},
    "AppealVoteResult": {"review_id", "settled", "outcome", "reward"},
    "AppealMineItem": {"review_id", "type", "kind", "source", "category_label", "level", "penalty", "my_vote", "voted_at", "outcome", "settled_at"},
    "Precedent": {"id", "case_id", "title", "summary", "kind", "outcome", "category", "category_label", "level", "by", "mine", "created_at"},
    "PrecedentDetail": {"id", "case_id", "title", "summary", "kind", "outcome", "category", "category_label", "level", "by", "mine", "created_at", "content", "content_hidden_reason", "case"},
    "PrecedentDetail.case": {"messages", "post"},
    "HallPost": {
        "id", "kind", "title", "content", "author", "author_tier", "status", "pinned", "vote", "admin_note", "comment_count", "can_comment", "mine", "can_edit", "can_delete",
        "reported", "created_at", "updated_at", "state", "content_truncated",
    },
    "HallPost.vote": {"yes", "no", "ends_at", "my_vote", "can_vote", "quorum", "pass_num", "pass_den"},
    "HallComment": {"id", "parent_id", "author", "author_tier", "content", "state", "mine", "reported", "created_at", "reply_to"},
    "JuryCaseDetail.my_vote": {"vote", "note", "voted_at", "alias", "agreed", "points"},
    "JuryCaseDetail.verdict": {"outcome", "source", "category", "category_label", "level", "closed_at", "hours", "overturned", "overturn_note", "explanation"},
    "tally": {"none", "l1", "l2", "l3", "abstain", "need_context"},
    "Opinion": {"id", "parent_id", "alias", "tier", "vote", "content", "state", "is_note", "mine", "like_count", "liked", "reported", "created_at", "reply_to_alias", "replies"},
    "JuryVoteItem": {
        "case_id", "kind", "primary_reason", "vote", "voted_at", "alias", "stage", "outcome", "verdict_source", "verdict_level", "closed_at", "agreed",
        "overturned", "is_quality_check", "opinion_count", "can_view", "public_case_id", "vote_stage",
    },
    "stats": {
        "votes_total", "votes_settled", "votes_agreed", "agreement_rate", "golden_total", "golden_correct", "golden_rate", "abstain_total", "abstain_rate",
        "opinions_total", "likes_received", "trend",
    },
    "stats.trend[]": {"date", "votes", "settled", "agreed"},
    "PublicCase": PUBLIC_KEYS,
    "PublicCaseDetail": PUBLIC_KEYS | {"content", "content_hidden_reason", "overturn_note"},
    "PublicCaseDetail.content": {"text", "image_count", "created_at"},
    "ExamState": {
        "available", "eligible", "passed", "passed_at", "total", "pass_score", "time_limit_minutes", "attempts_used", "attempts_max", "window_days",
        "next_attempt_at", "last_result", "active", "server_time",
    },
    "ExamState.last_result": {"score", "passed", "submitted_at"},
    "ExamStart": {"attempt_id", "expires_at", "questions", "server_time"},
    "ExamQuestion": QUESTION_KEYS | {"question"},
    "ExamResult": {"score", "total", "pass_score", "passed", "results", "attempts_left", "next_attempt_at", "status"},
    "ExamResult.results[]": {"question", "correct", "your_vote", "answer"},
    "answer": {"outcome", "level", "category_label", "explanation"},
    "PracticeNext": {"question", "left_today", "daily_limit", "stats", "round", "resets_at"},
    "PracticeQuestion": QUESTION_KEYS | {"question_id", "verified", "answered_before"},
    "PracticeAnswer": {"correct", "answer", "left_today", "stats", "resets_at"},
    "PointPage": {"total", "items", "count"},
    "PointPage.items[]": {"kind", "label", "points", "case_id", "created_at"},
    "Achievement": {"key", "label", "description", "earned_at", "current", "target"},
    "Leaderboard": {"period", "items", "me", "generated_at"},
    "Leaderboard.items[]": {"rank", "points", "tier", "votes", "me", "user"},
}
POINT_KINDS = {
    "vote_agreed", "vote_disagreed", "quality_correct", "quality_wrong", "daily_first", "streak_7", "opinion_liked", "exam_passed", "overturn_adjust", "admin_adjust",
    "panel_agreed", "panel_disagreed", "review_vote",
}
ACHIEVEMENTS = {"first_vote", "votes_10", "votes_50", "votes_100", "sharp_eye", "streak_7", "streak_30", "quality_10", "liked_10", "exam_first_try", "tier_regular", "tier_senior"}
CAPABILITIES = [
    "overview", "case_detail", "opinions", "votes", "public_cases", "exam", "practice", "votes_v2", "points", "tiers", "achievements", "leaderboard", "badge",
    "panel", "appeal_review", "machine_cases", "reconsider", "precedents", "hall",
]
# 演示库里打开的开关（生产默认全是关的）
DEMO_SWITCHES = {"auto_appoint": False, "appeals": True, "machine_review": True, "public_group_content": False, "opinions_open_to_jurors": False, "exam_required": False}


def aware(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class Checker:
    def __init__(self, base: str, verbose: bool) -> None:
        self.base = base.rstrip("/")
        self.verbose = verbose
        # trust_env=False：不走系统代理，直接连本机
        self.http = httpx.Client(timeout=60, trust_env=False)
        self.passed = 0
        self.failures: list[str] = []
        self.notes: list[str] = []
        # 字段路径 -> (第一次出现在哪个请求, 示例值)
        self.naive_paths: dict[str, tuple[str, str]] = {}
        self.tokens: dict[str, str] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.review_ids: dict[str, set[int]] = {}

    # ------------------------------------------------------------ 请求

    def proof_headers(self, method: str, path: str, client: str) -> dict[str, str]:
        """X-Kuke-Proof = sha256("METHOD|去掉 /api/v1 前缀的路径（不含查询串）|毫秒时间戳|nonce|KukeChat")。"""
        timestamp = str(int(time.time() * 1000))
        nonce = secrets.token_hex(8)
        digest = hashlib.sha256(f"{method.upper()}|{path}|{timestamp}|{nonce}|{PROOF_SALT}".encode("utf-8")).hexdigest()
        return {"X-Kuke-Client": client, "X-Kuke-Timestamp": timestamp, "X-Kuke-Nonce": nonce, "X-Kuke-Proof": digest}

    def call(self, method: str, path: str, *, user: str | None = None, body: Any = None, query: str = "", proof: bool = True) -> httpx.Response:
        """path 是 /api/v1 之后的部分，例如 /jury/me；query 是原样拼上去的查询串。"""
        headers: dict[str, str] = {}
        if proof:
            headers.update(self.proof_headers(method, path, "kukechat-admin/jury-demo-check" if path.startswith("/admin") else "kukechat-web/jury-demo-check"))
        if user is not None:
            headers["Authorization"] = f"Bearer {self.tokens[user]}"
        url = f"{self.base}/api/v1{path}{('?' + query) if query else ''}"
        return self.http.request(method, url, headers=headers, json=body)

    def get(self, path: str, user: str, query: str = "") -> Any:
        """不单独记结果的辅助读取。"""
        return self.call("GET", path, user=user, query=query).json()

    def login(self, username: str) -> dict[str, Any] | None:
        response = self.call("POST", "/auth/login", body={"username_or_email": username, "password": PASSWORD})
        if response.status_code != 200:
            return None
        data = response.json()
        self.tokens[username] = data["access_token"]
        self.users[username] = data["user"]
        return data

    # ------------------------------------------------------------ 记录

    def ok(self, label: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  PASS  {label}{('  — ' + detail) if detail else ''}")

    def fail(self, label: str, detail: str) -> None:
        self.failures.append(f"{label}: {detail}")
        print(f"  FAIL  {label}  — {detail}")

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)
            print(f"  NOTE  {text}")

    def expect(self, label: str, response: httpx.Response, status: int = 200, *, code: str | None = None, detail: str = "") -> Any:
        """核对状态码（和 detail.code），返回解析后的 JSON；不符合时记 FAIL 并原样带上请求和响应。"""
        try:
            data = response.json()
        except ValueError:
            data = response.text
        got_code = data.get("detail", {}).get("code") if isinstance(data, dict) and isinstance(data.get("detail"), dict) else None
        if response.status_code != status or (code is not None and got_code != code):
            wanted = f"{status}{(' ' + code) if code else ''}"
            request = f"{response.request.method} {response.request.url.raw_path.decode()}"
            self.fail(label, f"{request} 期望 {wanted}，实际 {response.status_code} {json.dumps(data, ensure_ascii=False)[:600]}")
            return None
        if self.verbose and isinstance(data, (dict, list)):
            print("        " + json.dumps(data, ensure_ascii=False)[:400])
        self.ok(label, f"{response.status_code}{(' ' + got_code) if got_code else ''}{('  ' + detail) if detail else ''}")
        self.scan_datetimes(label, data)
        return data

    def verify(self, label: str, condition: bool, detail: str) -> bool:
        if condition:
            self.ok(label, detail)
        else:
            self.fail(label, detail)
        return bool(condition)

    def keys(self, name: str, value: Any, *, optional: set[str] | None = None) -> None:
        """和契约里写的字段比：缺字段算 FAIL，多出来的记 NOTE。"""
        if not isinstance(value, dict):
            self.fail(f"字段 {name}", f"不是对象：{value!r}")
            return
        missing = CONTRACT[name] - set(value)
        extra = set(value) - CONTRACT[name] - (optional or set())
        if missing:
            self.fail(f"字段 {name}", f"契约里有、响应里没有：{sorted(missing)}")
        if extra:
            self.note(f"{name} 比契约多出字段：{sorted(extra)}")

    def scan_datetimes(self, label: str, value: Any, path: str = "") -> None:
        """记下不带时区的时间字符串出现在哪些字段上。"""
        if isinstance(value, dict):
            for key, item in value.items():
                self.scan_datetimes(label, item, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for item in value:
                self.scan_datetimes(label, item, f"{path}[]")
        elif isinstance(value, str) and NAIVE_DATETIME.match(value):
            self.naive_paths.setdefault(path, (label, value))

    def check_content(self, label: str, view: dict[str, Any]) -> None:
        """案件 / 题目的内容：结构、上下文的位置和时间顺序。所有带 messages 的视图都过这里。"""
        for message in view["messages"][:1]:
            self.keys("messages[]", message)
        if view.get("post") is not None:
            self.keys("post", view["post"])
        times = [message["created_at"] for message in view["messages"]]
        offsets = [message["offset"] for message in view["messages"]]
        problems: list[str] = []
        if view["kind"] == "message":
            targets = [message for message in view["messages"] if message["is_target"]]
            if len(targets) != 1 or targets[0]["offset"] != 0 or offsets != list(range(offsets[0], offsets[0] + len(offsets))) or offsets[0] < -10 or offsets[-1] > 10:
                problems.append(f"offset 不对：{offsets}")
            if any(not isinstance(value, str) or not value.endswith("Z") for value in times):
                problems.append(f"时间不带 Z：{[value for value in times if not str(value).endswith('Z')][:2]}")
            elif any(aware(left) > aware(right) for left, right in zip(times, times[1:])):
                problems.append(f"上下文消息的时间不是随顺序递增的：{times}")
            if view.get("post") is not None:
                problems.append("消息案件不应该带 post")
        elif view["post"] is None or not view["post"]["content"] or view["messages"]:
            problems.append("动态案件应该只有 post")
        elif not str(view["post"]["created_at"]).endswith("Z"):
            problems.append(f"post.created_at 不带 Z：{view['post']['created_at']}")
        if problems:
            self.fail(f"{label} 的内容", "；".join(problems))

    @staticmethod
    def target_text(view: dict[str, Any]) -> str:
        if view.get("post"):
            return view["post"]["content"] or ""
        return next((message["content"] or "" for message in view["messages"] if message["is_target"]), "")

    @staticmethod
    def signature(view: dict[str, Any]) -> tuple[str, str, str]:
        """认出「同一道题」用：类型 + 被举报内容 + 时间。"""
        if view.get("post"):
            return ("post", view["post"]["content"] or "", view["post"]["created_at"] or "")
        target = next((message for message in view["messages"] if message["is_target"]), {})
        return ("message", target.get("content") or "", target.get("created_at") or "")

    @staticmethod
    def content_key(view: dict[str, Any]) -> tuple[Any, ...]:
        """认出「同一道题」用，不看时间（考试题和质控题展示时时间是平移过的）：动态看正文和图片数，消息看整段上下文的文字。"""
        if view.get("post"):
            return ("post", view["post"]["content"] or "", len(view["post"]["image_urls"]))
        return ("message", *[(message["offset"], message["content"]) for message in view["messages"]])

    def all_votes(self, username: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            page = self.get("/jury/votes", username, f"limit=50&offset={len(items)}")
            items.extend(page["items"])
            if len(items) >= page["total"] or not page["items"]:
                break
        self.review_ids[username] = {item["case_id"] for item in items}
        return items

    # ------------------------------------------------------------ 各段检查

    def section(self, title: str) -> None:
        print(f"\n== {title}")

    def check_guard(self) -> None:
        self.section("服务与客户端证明头")
        health = self.http.get(f"{self.base}/health")
        self.verify("GET /health", health.status_code == 200 and health.json().get("status") == "ok", f"{health.status_code} {health.text[:80]}")
        realtime = self.http.get(f"{self.base}/health/realtime").json()
        self.verify("没有连 Redis", realtime.get("redis_realtime_configured") is False, f"redis_realtime_configured={realtime.get('redis_realtime_configured')}")
        storage = self.http.get(f"{self.base}/health/storage").json()
        self.verify("存储是本地磁盘", storage.get("storage_backend") == "local", json.dumps(storage))
        bare = self.call("GET", "/jury/config", proof=False)
        self.verify("不带证明头被拒", bare.status_code == 403, f"{bare.status_code} {bare.text[:80]}")

    def check_logins(self) -> None:
        self.section("登录（POST /auth/login）")
        for username, _nickname, role, _use in ACCOUNTS:
            data = self.expect(f"登录 {username}（{role}）", self.call("POST", "/auth/login", body={"username_or_email": username, "password": PASSWORD}))
            if data is None:
                continue
            if not data.get("access_token") or data.get("token_type") != "bearer" or data.get("user", {}).get("username") != username:
                self.fail(f"登录 {username}", f"响应形状不对：{json.dumps(data, ensure_ascii=False)[:300]}")
                continue
            self.tokens[username] = data["access_token"]
            self.users[username] = data["user"]
        self.expect("密码错误", self.call("POST", "/auth/login", body={"username_or_email": "juror", "password": "wrong-password"}), 401)
        me = self.expect("GET /auth/me（Bearer 令牌）", self.call("GET", "/auth/me", user="juror"))
        if me is not None:
            self.verify("令牌对应的用户", me.get("username") == "juror" and "jury_badge" in me, f"username={me.get('username')} jury_badge={me.get('jury_badge')}")

    def check_errors_and_cors(self) -> None:
        self.section("鉴权失败、错误的形状、CORS")
        anonymous = self.call("GET", "/jury/me")
        self.verify("不带 Authorization", anonymous.status_code in {401, 403}, f"{anonymous.status_code} {anonymous.text[:120]}")
        self.tokens["__bad__"] = "not-a-real-token"
        bad = self.call("GET", "/jury/me", user="__bad__")
        self.verify("令牌无效", bad.status_code == 401, f"{bad.status_code} {bad.text[:120]}")
        shapes: dict[str, str] = {}
        samples = [
            ("给不是我的评审编号投票（老接口）", self.call("POST", "/jury/cases/999999/vote", user="juror", body={"vote": "none"}), 404),
            ("vote 取值不对", self.call("POST", "/jury/cases/1/vote", user="juror", body={"vote": "maybe"}), 422),
            ("普通用户访问后台", self.call("GET", "/admin/cases", user="juror"), 403),
            ("读不是我的评审编号的详情（新接口）", self.call("GET", "/jury/cases/999999", user="eligible"), 404),
        ]
        for name, response, status in samples:
            detail = response.json().get("detail")
            shape = "{code, message}" if isinstance(detail, dict) and {"code", "message"} <= set(detail) else ("数组" if isinstance(detail, list) else "字符串")
            shapes[name] = shape
            self.verify(f"错误：{name}", response.status_code == status, f"{response.status_code} detail 是{shape}：{json.dumps(detail, ensure_ascii=False)[:140]}")
        self.verify("错误的三种形状和契约开头说的一致", shapes == {samples[0][0]: "字符串", samples[1][0]: "数组", samples[2][0]: "字符串", samples[3][0]: "{code, message}"}, str(shapes))
        for origin, allowed in (("http://localhost:5180", True), ("http://127.0.0.1:3000", True), ("https://tauri.localhost", True), ("https://www.ccw.site", False)):
            preflight = self.http.options(
                f"{self.base}/api/v1/jury/me",
                headers={"Origin": origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization,x-kuke-client,x-kuke-timestamp,x-kuke-nonce,x-kuke-proof"},
            )
            got = preflight.headers.get("access-control-allow-origin")
            self.verify(f"CORS 预检：{origin} {'放行' if allowed else '不放行'}", (got == origin) is allowed, f"{preflight.status_code} access-control-allow-origin={got}")

    def check_config_me_overview(self) -> None:
        self.section("配置 / 我的状态 / 总览（主账号 juror）")
        config = self.expect("GET /jury/config", self.call("GET", "/jury/config", user="juror"))
        if config is not None:
            self.keys("config", config)
            for part in ("switches", "rules", "opinions", "eligibility", "points", "suspend", "exam", "practice", "recuse", "abstain", "panel", "appeals", "reconsider", "hall"):
                self.keys(f"config.{part}", config.get(part))
            self.verify("config.version / capabilities（含二期、三期追加的六个）", config["version"] == 2 and config["capabilities"] == CAPABILITIES, f"capabilities={config['capabilities']}")
            self.verify("config.switches：演示库里打开了 appeals 和 machine_review（生产默认是关的）", config["switches"] == {name: DEMO_SWITCHES[name] for name in ("auto_appoint", "appeals", "machine_review")}, str(config["switches"]))
            hall = config.get("hall") or {}
            good = config.get("panel") == {"seats": 3, "consensus_votes": 2, "timeout_hours": 48} and config.get("appeals") == {"seats": 3, "consensus_votes": 2, "timeout_hours": 72} and config.get("reconsider") == {"window_days": 7, "text_min": 10}
            self.verify("config 的三期块：panel / appeals / reconsider，points 多三项", good and (config["points"].get("panel_agreed"), config["points"].get("panel_disagreed"), config["points"].get("review_vote")) == (15, 5, 8), f"panel={config.get('panel')} appeals={config.get('appeals')} reconsider={config.get('reconsider')}")
            good = (hall.get("title_max"), hall.get("content_max"), hall.get("comment_max"), hall.get("posts_per_day"), hall.get("comments_per_day"), hall.get("vote_days")) == (60, 5000, 1000, 3, 50, 7)
            good = good and (hall.get("quorum_ratio"), hall.get("quorum_min"), hall.get("pass_num"), hall.get("pass_den"), hall.get("pass_ratio"), hall.get("auto_hide_reports")) == (0.3, 3, 2, 3, 0.6667, 3)
            self.verify("config.hall：数字、守则和举报理由", good and "未结案件" in hall.get("guidelines", "") and hall.get("report_reasons") == ["人身攻击", "讨论未结案件", "泄露身份或隐私", "与风纪无关", "其他"], f"guidelines {len(hall.get('guidelines', ''))} 字，report_reasons={hall.get('report_reasons')}")
            tiers = config["tiers"]
            for tier in tiers:
                self.keys("config.tiers[]", tier)
            good = [tier["key"] for tier in tiers] == ["trainee", "regular", "senior"] and tiers[0]["requirements"] is None and tiers[0]["demote_below"] is None
            if good:
                for tier in tiers[1:]:
                    self.keys("config.tiers[].requirements", tier["requirements"])
                    self.keys("config.tiers[].demote_below", tier["demote_below"])
            self.verify("config.tiers：见习没有条件，正式 / 资深带晋升条件和降级线", good and tiers[1]["requirements"]["votes_settled"] == 15 and tiers[2]["requirements"]["votes_settled"] == 50 and tiers[2]["demote_below"]["to"] == "regular", json.dumps(tiers[1:], ensure_ascii=False)[:300])
            votes = [(vote["value"], vote["kind"]) for vote in config["votes"]]
            self.verify("config.votes（追加了回避和上下文不足）", votes == [("none", "clean"), ("l1", "violation"), ("l2", "violation"), ("l3", "violation"), ("abstain", "abstain"), ("recuse", "recuse"), ("need_context", "abstain")], str(votes))
            for category in config["categories"][:1]:
                self.keys("config.categories[]", category)
            names = {category["key"] for category in config["categories"]}
            self.verify("config.categories 不含色情类", {"harassment", "advertising", "fraud", "flooding", "threat", "other"} <= names and not names & {"sexual", "minor_sexual", "vulgar", "suggestive"}, str(sorted(names)))
            self.verify("config 的二期数字", config["rules"]["severe_min_votes"] == 5 and config["rules"]["need_context_votes"] == 2 and config["exam"]["total"] == 10 and config["exam"]["pass_score"] == 8 and config["practice"]["daily_limit"] == 20 and config["recuse"]["daily_limit"] == 5 and config["points"]["vote_agreed"] == 10, f"points={config['points']}")
            self.config = config
        me = self.expect("GET /jury/me", self.call("GET", "/jury/me", user="juror"))
        if me is not None:
            self.keys("JuryStatus", me)
            self.keys("JuryStatus.exam", me["exam"])
            self.keys("JuryStatus.tier_progress", me["tier_progress"])
            self.verify("/jury/me 主账号在任、可评审 ≥ 6", me["status"] == "active" and me["tier"] == "regular" and (me["available_cases"] or 0) >= 6 and me["restriction"] is None, f"available_cases={me['available_cases']} voted_today={me['voted_today']} stats={me['stats']}")
            requirements = {item["key"]: item for item in me["tier_progress"]["requirements"]}
            self.verify("主账号离资深还差一部分：有的条件满足、有的不满足", me["tier_progress"]["next_tier"] == "senior" and set(requirements) == {"votes_settled", "agreement_rate", "quality_rate", "days_served"} and any(item["ok"] for item in requirements.values()) and not all(item["ok"] for item in requirements.values()), "；".join(f"{item['label']} {item['current']}/{item['target']} {'✓' if item['ok'] else '✗'}" for item in requirements.values()))
            self.verify("主账号的风纪值、回避名额和两个恢复时间", me["points"] > 100 and me["recuse_left_today"] == 4 and bool(me["daily_resets_at"]) and bool(me["recuse_resets_at"]) and me["cooldown_until"] is None and me["warnings"] == [] and me["badge_visible"] is False, f"points={me['points']} recuse_left_today={me['recuse_left_today']} daily_resets_at={me['daily_resets_at']} recuse_resets_at={me['recuse_resets_at']}")
            self.verify("主账号不是资深委员：/jury/me.senior 是 null", me["senior"] is None, f"senior={me['senior']}")
            self.verify("主账号没考过试，exam_required 默认关", me["exam"]["available"] is True and me["exam"]["passed"] is False and me["exam"]["attempts_used"] == 0 and me["exam_required"] is False, json.dumps(me["exam"]))
            self.me_before = me
        overview = self.expect("GET /jury/overview", self.call("GET", "/jury/overview", user="juror"))
        if overview is not None:
            self.keys("overview", overview)
            for part in ("jurors", "backlog", "settled", "counts", "me"):
                self.keys(f"overview.{part}", overview.get(part))
            for day in overview["daily"][:1]:
                self.keys("overview.daily[]", day)
            rates = (overview["consensus_rate"], overview["violation_rate"], overview["overturn_rate"])
            self.verify("总览：样本够、比率都有", overview["sample_enough"] and all(rate is not None for rate in rates) and overview["median_hours"] is not None, f"sample={overview['sample']} 比率={rates} median_hours={overview['median_hours']}")
            self.verify("总览：在任委员", overview["jurors"]["total"] == sum(overview["jurors"][tier] for tier in ("trainee", "regular", "senior")) and overview["jurors"]["senior"] >= 3, str(overview["jurors"]))
            self.verify("总览：积压与定案数", overview["backlog"]["jury"] >= 6 and 0 < overview["settled"]["d7"] <= overview["settled"]["d30"] <= overview["settled"]["total"], f"backlog={overview['backlog']} settled={overview['settled']} counts={overview['counts']}")
            days = overview["daily"]
            today = datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).date().isoformat()
            self.verify("总览：daily 是 30 天、正序、今天在最后", len(days) == 30 and days[-1]["date"] == today and [day["date"] for day in days] == sorted(day["date"] for day in days), f"{days[0]['date']} … {days[-1]['date']}")
            self.verify("总览：daily 有形状", sum(1 for day in days if day["settled"]) >= 10 and sum(day["queued"] for day in days) >= 20, f"有定案的天数={sum(1 for day in days if day['settled'])} 进队列合计={sum(day['queued'] for day in days)}")
            self.verify("总览：me", overview["me"]["status"] == "active" and overview["me"]["tier"] == "regular" and overview["me"]["daily_limit"] == 30, str(overview["me"]))

    # ------------------------------------------------------------ 评审台

    def plan_vote(self, text: str, state: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
        """按被举报的那句话认出有特别用途的案件，决定这一票怎么投。返回 (标记, 请求体, 说明)。"""
        if text == DEMO_TARGETS["settle_violation"]:
            return "settle_violation", {"vote": "l2", "category": "harassment", "note": "联调自测：这是投票时写的备注"}, "已有两张违规票，第三张一致的票当场定案"
        if text == DEMO_TARGETS["settle_clean"]:
            return "settle_clean", {"vote": "none"}, "已有两张不违规票，第三张一致的票当场定案"
        if text == DEMO_TARGETS["no_consensus"]:
            return "no_consensus", {"vote": "none"}, "已有四张票 2:2，第五张有效票投完没有共识，转管理员"
        if text == DEMO_TARGETS["need_context"]:
            return "need_context", {"vote": "need_context", "note": "联调自测：上下文不够"}, "已有一张「上下文不足」，第二张投完转出"
        if text == DEMO_TARGETS["severe"]:
            return "severe", {"vote": "l3", "category": "threat"}, "已有三张严重违规票，第四张一致的票仍不定案（严重违规要五张有效票）"
        if text == DEMO_TARGETS["category"]:
            return "category", {"vote": "l1", "category": "flooding"}, "举报理由是「其他」，投违规票时自己选类别"
        if text in {DEMO_TARGETS["machine_1"], DEMO_TARGETS["machine_2"], DEMO_TARGETS["machine_3"]}:
            return "machine", {"vote": "none"}, "机器来源的案件（没有举报人）"
        state["plain"] = state.get("plain", 0) + 1
        if state["plain"] in {2, 6, 9, 12}:
            # 回避不占每天 30 件的名额：多回避几件，给排在最后的机器来源的案件留出票数
            return "recuse", {"vote": "recuse", "note": "联调自测：主动回避"}, "主动回避"
        if state["plain"] == 4:
            return "abstain", {"vote": "abstain"}, "拿不准"
        return "plain", {"vote": ("l2", "none", "l1")[state["plain"] % 3]}, "普通的一票"

    def check_review(self, max_rounds: int) -> None:
        self.section("评审台：连续取案并投票，直到几个有特别用途的案件都遇到（主账号 juror）")
        wanted = {"settle_violation", "settle_clean", "no_consensus", "need_context", "severe", "category", "recuse", "abstain"}
        before = self.all_votes("juror")
        seen_ids: list[int] = []
        self.voted: list[dict[str, Any]] = []
        state: dict[str, Any] = {}
        for index in range(max_rounds):
            label = f"第 {index + 1} 件"
            response = self.call("GET", "/jury/cases/next", user="juror")
            if response.status_code == 429:
                limited = self.expect(f"{label} GET /jury/cases/next：到了每天的评审上限", response, 429, code="jury_daily_limit")
                if limited is not None:
                    self.verify("429 jury_daily_limit 带 detail.resets_at", bool(limited["detail"].get("resets_at")), str(limited["detail"]))
                self.daily_limit_hit = True
                break
            data = self.expect(f"{label} GET /jury/cases/next", response)
            if data is None:
                return
            view = data.get("case")
            if view is None:
                self.ok(f"{label} 取案", "case 是 null：能分给主账号的案件已经评完了")
                break
            review_id = view["case_id"]
            self.keys("JuryCaseView", view)
            self.check_content(label, view)
            self.verify(f"{label} 评审编号是新的", review_id not in seen_ids and review_id not in self.review_ids["juror"], f"评审编号 {review_id}")
            seen_ids.append(review_id)
            self.inspect_images(label, view)
            if index == 0:
                again = self.get("/jury/cases/next", "juror").get("case") or {}
                self.verify("没投之前再取，还是同一个案件、同一个评审编号", again.get("case_id") == review_id and again.get("assigned_at") == view["assigned_at"], f"评审编号 {again.get('case_id')}")
                self.expect("分到手还没投：读详情", self.call("GET", f"/jury/cases/{review_id}", user="juror"), 403, code="not_voted")
                self.expect("分到手还没投：读观点区", self.call("GET", f"/jury/cases/{review_id}/opinions", user="juror"), 403, code="not_voted")
                self.expect("别人拿我的评审编号来投票", self.call("POST", f"/jury/cases/{review_id}/vote", user="senior1", body={"vote": "none"}), 404)
                self.expect("别人拿我的评审编号读详情", self.call("GET", f"/jury/cases/{review_id}", user="senior1"), 404, code="case_not_found")
            tag, body, why = self.plan_vote(self.target_text(view), state)
            if tag == "category":
                self.expect("类别不在 config.categories 里", self.call("POST", f"/jury/cases/{review_id}/vote", user="juror", body={"vote": "l1", "category": "sexual"}), 422, code="invalid_category")
                self.verify("「其他」理由的案件没有默认类别可用时给 other", view["suggested_category"] == "other", f"suggested_category={view['suggested_category']}")
            started = time.perf_counter()
            result = self.expect(f"{label} POST /jury/cases/{{评审编号}}/vote {json.dumps(body, ensure_ascii=False)}", self.call("POST", f"/jury/cases/{review_id}/vote", user="juror", body=body), detail=why)
            elapsed = time.perf_counter() - started
            if result is None:
                continue
            self.keys("vote result", result)
            self.keys("reward", result["reward"])
            for item in result["reward"]["items"]:
                self.keys("reward.items[]", item)
            detail = result["case"]
            entry = {"tag": tag, "review_id": review_id, "vote": body["vote"], "settled": result["settled"], "detail": detail, "elapsed": elapsed, "kind": view["kind"], "reward": result["reward"], "view": view}
            self.voted.append(entry)
            self.verify(f"{label} 响应里的 case_id 就是评审编号", result["case_id"] == review_id and (detail is None or detail["case_id"] == review_id), f"{result['case_id']}")
            if body["vote"] == "recuse":
                self.verify(f"{label} 回避：case 为 null，不给分", detail is None and result["settled"] is False and result["reward"]["points"] == 0 and result["reward"]["items"] == [], json.dumps(result["reward"]))
                self.expect(f"{label} 回避之后读详情", self.call("GET", f"/jury/cases/{review_id}", user="juror"), 403, code="not_voted")
                self.expect(f"{label} 回避之后读观点区", self.call("GET", f"/jury/cases/{review_id}/opinions", user="juror"), 403, code="not_voted")
                self.expect(f"{label} 回避之后再投", self.call("POST", f"/jury/cases/{review_id}/vote", user="juror", body={"vote": "none"}), 409)
                continue
            self.check_detail(f"{label} 投后视图", detail, body["vote"])
            kind = "质控题" if detail["is_quality_check"] else ("当场定案" if result["settled"] else f"未定案，stage={detail['stage']}")
            self.ok(f"{label} 结果", f"{kind}，votes_cast={detail['votes_cast']}，reward={result['reward']['points']}（{[item['kind'] for item in result['reward']['items']]}），耗时 {elapsed:.2f}s")
            fetched = self.expect(f"{label} GET /jury/cases/{{评审编号}}", self.call("GET", f"/jury/cases/{review_id}", user="juror"))
            if fetched is not None:
                same = all(fetched[key] == detail[key] for key in ("stage", "votes_cast", "tally", "is_quality_check", "opinion_count", "case_id"))
                self.verify(f"{label} 详情和投票响应一致", same, f"stage={fetched['stage']} opinion_count={fetched['opinion_count']}")
            self.expect(f"{label} 重复投票", self.call("POST", f"/jury/cases/{review_id}/vote", user="juror", body={"vote": "none"}), 409)
            # 机器来源的案件排在所有举报案件后面：一直取到遇见它为止
            if wanted <= {item["tag"] for item in self.voted} and index >= 7 and sum(1 for item in self.voted if item["tag"] == "machine") >= 2:
                break
        by_tag = {item["tag"]: item for item in self.voted}
        self.verify("几个有特别用途的案件都遇到了", wanted <= set(by_tag), f"遇到了 {[item['tag'] for item in self.voted]}")
        quality = [item for item in self.voted if item["detail"] and item["detail"]["is_quality_check"]]
        first_six = [item for item in self.voted[:6] if item["detail"] and item["detail"]["is_quality_check"]]
        self.verify("前 6 件里混有质控题", len(first_six) >= 1, f"一共 {len(self.voted)} 件，质控题在第 {[self.voted.index(item) + 1 for item in quality]} 件")
        self.verify("评审编号一路递增：质控题和普通案件的编号连在一起，从大小上分不出来", seen_ids == sorted(seen_ids) and seen_ids[0] > max(self.review_ids["juror"], default=0), f"{seen_ids}")
        self.verify("消息和动态都出现过", {item["kind"] for item in self.voted} == {"message", "post"}, str([item["kind"] for item in self.voted]))
        # 质控题是早就结案的旧案件：投票前内容的时间整体平移到「分到手之前 2–40 小时」，投完（已经揭晓）之后是真实时间
        now = datetime.now(UTC)
        ages: dict[bool, list[float]] = {True: [], False: []}
        problems: list[str] = []
        for position, item in enumerate(self.voted, start=1):
            if item["detail"] is None:
                continue
            shown, assigned = aware(self.signature(item["view"])[2]), aware(item["view"]["assigned_at"])
            real = aware(self.signature(item["detail"])[2])
            age = (assigned - shown).total_seconds() / 3600
            ages[bool(item["detail"]["is_quality_check"])].append(round(age, 1))
            if item["detail"]["is_quality_check"]:
                latest = max([aware(message["created_at"]) for message in item["view"]["messages"]] or [shown])
                if not 2 <= age <= 40.5 or latest > assigned - timedelta(hours=2) + timedelta(seconds=2):
                    problems.append(f"第 {position} 件（质控题）投票前被举报内容的时间在分到手之前 {age:.1f} 小时，最后一条上下文在 {(assigned - latest).total_seconds() / 3600:.1f} 小时前")
                if real >= shown or (now - real).total_seconds() < 72 * 3600:
                    problems.append(f"第 {position} 件（质控题）投完之后应该是真实时间：投票前 {shown.isoformat()}，投完之后 {real.isoformat()}")
            elif real != shown:
                problems.append(f"第 {position} 件（普通案件）投票前后内容的时间不一样：{shown.isoformat()} / {real.isoformat()}")
        self.verify(
            "质控题投票前的内容时间落在近期范围内（分到手之前 2–40 小时），投完之后是真实时间；普通案件前后一致", not problems and bool(ages[True]),
            "；".join(problems) or f"投票前看到的「发于分到手之前多少小时」：质控题 {sorted(ages[True])}，普通案件 {min(ages[False]):.1f}–{max(ages[False]):.1f}",
        )
        machine = [item for item in self.voted if item["tag"] == "machine"]
        if machine:
            first = self.voted.index(machine[0])
            later = [item for item in self.voted[first:] if item["tag"] != "machine" and not (item["detail"] or {}).get("is_quality_check")]
            earlier = [item for item in self.voted[:first] if item["detail"] is None or not item["detail"]["is_quality_check"]]
            self.verify("机器来源的案件排在所有举报案件之后", not later and len(earlier) >= 15, f"前面有 {len(earlier)} 个举报案件，第一个机器案件是第 {first + 1} 件")
            view = machine[0]["view"]
            good = view["case_origin"] == "machine" and view["report_count"] == 0 and len(view["reasons"]) == 1 and view["reasons"][0]["count"] == 1 and view["reasons"][0]["reason"].startswith("机器筛查 · ")
            self.verify("机器来源的案件：case_origin=machine，report_count=0，reasons 是一项「机器筛查 · 类别」", good and all(item["view"]["case_origin"] == "machine" for item in machine) and view["suggested_category"] in {"harassment", "advertising"}, f"reasons={view['reasons']} suggested_category={view['suggested_category']}")
            self.verify("机器来源的案件：投后详情同样带 case_origin", machine[0]["detail"]["case_origin"] == "machine" and machine[0]["detail"]["report_count"] == 0 and machine[0]["detail"]["stage"] == "jury", f"stage={machine[0]['detail']['stage']}")
            self.verify("举报来的案件 case_origin 都是 report", all(item["view"]["case_origin"] == "report" for item in self.voted if item["tag"] != "machine"), "ok")
        elif getattr(self, "daily_limit_hit", False):
            self.note("这一轮质控题出得多，主账号在取到机器来源的案件之前就到了每天 30 件的上限；机器案件的字段这次只在后台和公示栏里核对了")
        else:
            self.fail("机器来源的案件", "举报案件都评完了，也没有取到机器来源的案件")
        for item in quality:
            detail = item["detail"]
            good = detail["stage"] == "closed" and detail["verdict"] is not None and detail["tally"] is None and detail["votes_cast"] == 0 and detail["opinions_enabled"] is False
            good = good and detail["my_vote"]["alias"] is None and detail["public_case_id"] is None and (detail["my_vote"]["agreed"] is not None or item["vote"] in {"abstain", "need_context"})
            kinds = [reward["kind"] for reward in item["reward"]["items"]]
            scored = item["vote"] in {"abstain", "need_context"} or any(kind in {"quality_correct", "quality_wrong"} for kind in kinds)
            self.verify(f"质控题（第 {self.voted.index(item) + 1} 件）当场揭晓，风纪值当场结算", good and scored, f"verdict={detail['verdict']['outcome'] if detail['verdict'] else None} agreed={detail['my_vote']['agreed']} public_case_id={detail['public_case_id']} reward={kinds} explanation={'有' if detail['verdict'] and detail['verdict']['explanation'] else '无'}")
            self.expect("质控题没有观点区", self.call("GET", f"/jury/cases/{item['review_id']}/opinions", user="juror"), 403, code="opinions_closed")
        if "settle_violation" in by_tag:
            item, detail = by_tag["settle_violation"], by_tag["settle_violation"]["detail"]
            good = item["settled"] and detail["stage"] == "closed" and detail["verdict"]["outcome"] == "violation" and detail["verdict"]["category"] == "harassment" and detail["my_vote"]["category"] == "harassment"
            self.verify("三票一致认定违规：当场定案，带类别；我的加分随后到账", good and detail["my_vote"]["agreed"] is True and detail["my_vote"]["points"] >= 10 and detail["public_case_id"] is None, f"verdict={detail['verdict']['category_label']}/L{detail['verdict']['level']} tally={detail['tally']} my_vote.points={detail['my_vote']['points']} 耗时 {item['elapsed']:.2f}s")
            if item["elapsed"] > 1.0:
                self.note(f"定案这一票用了 {item['elapsed']:.2f}s（群成员 320 人）")
        if "settle_clean" in by_tag:
            item, detail = by_tag["settle_clean"], by_tag["settle_clean"]["detail"]
            self.verify("三票一致认定不违规：当场定案", item["settled"] and detail["verdict"]["outcome"] == "clean" and detail["verdict"]["category"] is None and detail["tally"]["none"] == 3, f"tally={detail['tally']}")
        if "no_consensus" in by_tag:
            detail = by_tag["no_consensus"]["detail"]
            self.verify("五张有效票 3:2：没有共识，转管理员", by_tag["no_consensus"]["settled"] is False and detail["stage"] == "admin" and detail["votes_cast"] == 5 and detail["tally"] is None, f"stage={detail['stage']} votes_cast={detail['votes_cast']}")
        if "need_context" in by_tag:
            detail = by_tag["need_context"]["detail"]
            self.verify("第二张「上下文不足」：转出，不计入有效票，不给分", detail["stage"] == "admin" and detail["my_vote"]["vote"] == "need_context" and detail["votes_cast"] == 0 and by_tag["need_context"]["reward"]["points"] == 0, f"stage={detail['stage']} votes_cast={detail['votes_cast']}")
        if "severe" in by_tag:
            detail = by_tag["severe"]["detail"]
            self.verify("四张票都是严重违规：还不定案，继续收票", by_tag["severe"]["settled"] is False and detail["stage"] == "jury" and detail["votes_cast"] == 4 and detail["verdict"] is None and detail["my_vote"]["category"] == "threat", f"stage={detail['stage']} votes_cast={detail['votes_cast']}")
        if "category" in by_tag:
            detail = by_tag["category"]["detail"]
            self.verify("带类别的违规票记下了类别", detail["my_vote"]["category"] == "flooding" and detail["my_vote"]["vote"] == "l1", json.dumps(detail["my_vote"], ensure_ascii=False))
        if "abstain" in by_tag and not by_tag["abstain"]["detail"]["is_quality_check"]:
            self.verify("「拿不准」不计入有效票、不给分", by_tag["abstain"]["reward"]["points"] == 0 and by_tag["abstain"]["detail"]["my_vote"]["agreed"] is None, json.dumps(by_tag["abstain"]["reward"]))
        after = self.get("/jury/me", "juror")
        recuses = sum(1 for item in self.voted if item["vote"] == "recuse")
        counted = len(self.voted) - recuses
        self.verify("投完之后 /jury/me 跟着变：回避不算票、只占回避名额", after["voted_today"] == self.me_before["voted_today"] + counted and after["stats"]["votes_total"] == self.me_before["stats"]["votes_total"] + counted and after["recuse_left_today"] == self.me_before["recuse_left_today"] - recuses and after["points"] > self.me_before["points"], f"voted_today={after['voted_today']} votes_total={after['stats']['votes_total']} recuse_left_today={after['recuse_left_today']} points {self.me_before['points']} → {after['points']}")
        self.verify("这一轮之前的评审记录一条没少", len(self.all_votes("juror")) == len(before) + len(self.voted), f"{len(before)} + {len(self.voted)}")

    def inspect_images(self, label: str, view: dict[str, Any]) -> None:
        urls: list[str] = []
        for message in view["messages"]:
            urls.extend(message["images"])
            if message["type"] == "image" and message["content"]:
                urls.append(message["content"])
                if not message["images"]:
                    self.note("图片消息（type=image）的图片地址在 messages[].content 里，images 是空数组；只有「文字带图」的消息才把图片放在 images 里")
        if view["post"]:
            urls.extend(view["post"]["image_urls"])
        for url in dict.fromkeys(urls):
            response = self.http.get(f"{self.base}{url}")
            self.verify(f"{label} 图片能加载", response.status_code == 200 and response.headers.get("content-type", "").startswith("image/"), f"{url} → {response.status_code} {response.headers.get('content-type')} {len(response.content)} 字节")

    def check_detail(self, label: str, detail: dict[str, Any], vote: str | None) -> None:
        self.keys("JuryCaseDetail", detail, optional={"suggested_category"})
        if detail.get("precedent") is not None:
            self.keys("JuryCaseDetail.precedent", detail["precedent"])
        self.keys("JuryCaseDetail.my_vote", detail.get("my_vote"), optional={"category"})
        if "category" not in detail.get("my_vote", {}):
            self.fail(label, "my_vote 里没有 category")
        closed = detail["stage"] == "closed"
        problems: list[str] = []
        if detail["content_available"]:
            self.check_content(label, detail)
        if vote is not None and detail["my_vote"]["vote"] != vote:
            problems.append(f"my_vote.vote={detail['my_vote']['vote']}")
        if closed:
            self.keys("JuryCaseDetail.verdict", detail.get("verdict"))
            if detail["is_quality_check"]:
                if detail["tally"] is not None:
                    problems.append("质控题不应该给 tally")
            elif detail["tally"] is None:
                problems.append("已定案但没有 tally")
            else:
                # 进过资深合议的案件多一项 panel；原审的分布保持原样
                original = {name: count for name, count in detail["tally"].items() if name != "panel"}
                self.keys("tally", original)
                if "panel" in detail["tally"]:
                    self.keys("tally.panel", detail["tally"]["panel"])
                if sum(count for name, count in original.items() if name not in {"abstain", "need_context"}) != detail["votes_cast"]:
                    problems.append(f"votes_cast={detail['votes_cast']} 和 tally={detail['tally']} 对不上")
        else:
            if detail["tally"] is not None or detail["verdict"] is not None:
                problems.append("未定案却给了 tally / verdict")
            if detail["my_vote"]["agreed"] is not None:
                problems.append("未定案却给了 my_vote.agreed")
        if detail["is_quality_check"] and (detail["my_vote"]["alias"] is not None or detail["public_case_id"] is not None):
            problems.append("质控题不应该有代号和公开编号")
        if not detail["is_quality_check"] and not detail["my_vote"]["alias"]:
            problems.append("没有代号")
        self.verify(label, not problems, "；".join(problems) or f"stage={detail['stage']} alias={detail['my_vote']['alias']} agreed={detail['my_vote']['agreed']} votes_cast={detail['votes_cast']} opinion_count={detail['opinion_count']} public_case_id={detail['public_case_id']}")

    # ------------------------------------------------------------ 两种编号

    def check_ids(self) -> None:
        self.section("两种案件编号（契约 1.0）")
        mine = self.all_votes("juror")
        others = self.all_votes("senior1")
        board = self.get("/jury/public/cases", "juror", "limit=50")["items"]
        public_ids = [item["case_id"] for item in board]
        foreign = next((item["case_id"] for item in others if item["case_id"] not in self.review_ids["juror"]), None)
        real = next((case_id for case_id in public_ids if case_id not in self.review_ids["juror"]), None)
        if foreign is not None:
            self.expect("拿别人的评审编号读详情", self.call("GET", f"/jury/cases/{foreign}", user="juror"), 404, code="case_not_found")
            self.expect("拿别人的评审编号读观点区", self.call("GET", f"/jury/cases/{foreign}/opinions", user="juror"), 404, code="case_not_found")
            self.expect("拿别人的评审编号发观点", self.call("POST", f"/jury/cases/{foreign}/opinions", user="juror", body={"content": "路过"}), 404, code="case_not_found")
        if real is not None:
            self.expect("拿公示栏的公开编号读委员侧详情", self.call("GET", f"/jury/cases/{real}", user="juror"), 404, code="case_not_found")
            self.expect("拿公示栏的公开编号投票", self.call("POST", f"/jury/cases/{real}/vote", user="juror", body={"vote": "none"}), 404)
        published = [item for item in mine if item["public_case_id"] is not None]
        self.verify("我的评审里，已公示的案件带 public_case_id，而且都在公示栏里", len(published) >= 20 and all(item["stage"] == "closed" and not item["is_quality_check"] and item["can_view"] for item in published) and {item["public_case_id"] for item in published[:10]} <= set(self.public_all()), f"{len(published)} 条带公开编号")
        self.verify("评审编号和公开编号是两套数：同一个案件上两者不相等", all(item["public_case_id"] != item["case_id"] for item in published), "ok")
        hidden = [item for item in mine if item["is_quality_check"] or not item["can_view"] or item["stage"] != "closed"]
        self.verify("质控题、回避的记录、未定案的案件 public_case_id 都是 null", all(item["public_case_id"] is None for item in hidden), f"{len(hidden)} 条")
        sample = next((item for item in published), None)
        if sample is not None:
            detail = self.expect("已公示的案件：详情里的 public_case_id", self.call("GET", f"/jury/cases/{sample['case_id']}", user="juror"))
            if detail is not None:
                self.verify("详情和列表给的公开编号一致", detail["public_case_id"] == sample["public_case_id"], f"评审编号 {sample['case_id']} → 公开编号 {detail['public_case_id']}")
            public = self.expect("用公开编号读公示栏详情", self.call("GET", f"/jury/public/cases/{sample['public_case_id']}", user="juror"))
            if public is not None:
                self.verify("公示栏里是同一个案件", public["case_id"] == sample["public_case_id"] and public["outcome"] == sample["outcome"] and public["closed_at"] == sample["closed_at"], f"outcome={public['outcome']}")
            admin = self.expect("后台案件详情（真实编号）", self.call("GET", f"/admin/cases/{sample['public_case_id']}", user="admin"))
            if admin is not None:
                row = next((vote for vote in admin["votes"] if (vote["juror"] or {}).get("username") == "juror"), {})
                self.verify("后台 votes[].review_id 就是委员看到的评审编号", row.get("review_id") == sample["case_id"], f"review_id={row.get('review_id')}")

    def public_all(self) -> list[int]:
        items: list[dict[str, Any]] = []
        while True:
            page = self.get("/jury/public/cases", "juror", f"limit=50&offset={len(items)}")
            items.extend(page["items"])
            if len(items) >= page["total"] or not page["items"]:
                break
        self.public_items = items
        return [item["case_id"] for item in items]

    # ------------------------------------------------------------ 观点区

    def check_opinions(self) -> None:
        self.section("观点区（主账号 juror，路径里是评审编号）")
        votes = [item for item in self.get("/jury/votes", "juror", "state=open&limit=50")["items"] if not item["is_quality_check"] and item["can_view"]]
        candidates = sorted(votes, key=lambda item: -item["opinion_count"])
        if not candidates:
            self.fail("找一个有讨论的未结案件", "主账号没有未结的投票")
            return
        case_id = candidates[0]["case_id"]
        self.discussion_case = case_id
        data = self.expect("GET /jury/cases/{评审编号}/opinions", self.call("GET", f"/jury/cases/{case_id}/opinions", user="juror"))
        if data is None:
            return
        roots = data["items"]
        flat = roots + [reply for root in roots for reply in root["replies"]]
        for item in flat[:1]:
            self.keys("Opinion", item)
        states = {item["state"] for item in flat}
        self.verify("列表：有折叠的、有已删除的占位", {"visible", "hidden", "deleted"} <= states, f"total={data['total']} 根观点 {len(roots)} 条，状态 {sorted(states)}")
        self.verify("列表：折叠 / 已删除的不带内容", all(item["content"] is None for item in flat if item["state"] != "visible") and all(item["content"] for item in flat if item["state"] == "visible"), "content 为 null")
        self.verify("列表：未定案时不透露别人的票", all(item["vote"] is None for item in flat), "vote 全是 null")
        self.verify("列表：有被点赞的、有我赞过的", any(item["like_count"] >= 3 for item in flat) and any(item["liked"] for item in flat), str([(item["alias"], item["like_count"], item["liked"]) for item in roots]))
        self.verify("列表：有回复、有回复别人的回复", any(root["replies"] for root in roots) and any(reply["reply_to_alias"] for root in roots for reply in root["replies"]), str([(reply["alias"], reply["reply_to_alias"]) for root in roots for reply in root["replies"]]))
        self.verify("列表：回复只有一层、按时间正序", all(reply["replies"] == [] and reply["parent_id"] == root["id"] for root in roots for reply in root["replies"]) and [root["created_at"] for root in roots] == sorted(root["created_at"] for root in roots), "ok")
        self.verify("列表：total 不含已删除的；已删除的根观点只在有回复时留占位", data["total"] == sum(1 for item in flat if item["state"] != "deleted") and all(root["replies"] for root in roots if root["state"] == "deleted"), f"total={data['total']}")
        others = [item for item in roots if not item["mine"] and item["state"] == "visible"]
        mine = [item for item in flat if item["mine"] and item["state"] == "visible"]
        if not others or not mine:
            self.fail("找别人的观点和自己的观点", f"别人的 {len(others)} 条，自己的 {len(mine)} 条")
            return
        target = next((item for item in others if not item["liked"]), others[0])

        created = self.expect("发表 POST /jury/cases/{评审编号}/opinions", self.call("POST", f"/jury/cases/{case_id}/opinions", user="juror", body={"content": "联调自测：补充一条观点"}))
        if created is None:
            return
        self.keys("Opinion", created)
        self.verify("发表：返回的观点", created["mine"] and created["parent_id"] is None and created["is_note"] is False and created["state"] == "visible" and bool(created["alias"]), f"id={created['id']} alias={created['alias']} tier={created['tier']}")
        reply = self.expect("回复别人的观点", self.call("POST", f"/jury/cases/{case_id}/opinions", user="juror", body={"content": "联调自测：回复一下", "parent_id": target["id"]}))
        nested_parent = next((reply_item for root in roots for reply_item in root["replies"] if not reply_item["mine"] and reply_item["state"] == "visible"), None)
        if reply is not None:
            self.verify("回复：挂在根观点下面", reply["parent_id"] == target["id"] and reply["reply_to_alias"] is None, f"parent_id={reply['parent_id']}")
        if nested_parent is not None:
            nested = self.expect("回复别人的回复", self.call("POST", f"/jury/cases/{case_id}/opinions", user="juror", body={"content": "联调自测：回复一条回复", "parent_id": nested_parent["id"]}))
            if nested is not None:
                self.verify("回复的回复：挂到同一条根观点，并带 reply_to_alias", nested["parent_id"] == nested_parent["parent_id"] and nested["reply_to_alias"] == nested_parent["alias"], f"reply_to_alias={nested['reply_to_alias']}")
        self.expect("内容为空", self.call("POST", f"/jury/cases/{case_id}/opinions", user="juror", body={"content": "   "}), 422, code="invalid_opinion")
        self.expect("内容超长（501 字）", self.call("POST", f"/jury/cases/{case_id}/opinions", user="juror", body={"content": "字" * 501}), 422, code="invalid_opinion")
        self.expect("请求体里没有 content", self.call("POST", f"/jury/cases/{case_id}/opinions", user="juror", body={}), 422, code="invalid_opinion")

        before = target["like_count"]
        liked = self.expect("点赞 POST /jury/opinions/{id}/like", self.call("POST", f"/jury/opinions/{target['id']}/like", user="juror"))
        if liked is not None:
            self.verify("点赞：返回 {id, like_count, liked}", set(liked) == {"id", "like_count", "liked"} and liked["liked"] is True and liked["like_count"] == before + (0 if target["liked"] else 1), str(liked))
            self.verify("重复点赞是幂等的", self.call("POST", f"/jury/opinions/{target['id']}/like", user="juror").json() == liked, "ok")
        unliked = self.expect("取消点赞 DELETE /jury/opinions/{id}/like", self.call("DELETE", f"/jury/opinions/{target['id']}/like", user="juror"))
        if unliked is not None and liked is not None:
            self.verify("取消点赞：计数减一", unliked["liked"] is False and unliked["like_count"] == liked["like_count"] - 1, str(unliked))
            self.verify("重复取消是幂等的", self.call("DELETE", f"/jury/opinions/{target['id']}/like", user="juror").json() == unliked, "ok")
        self.expect("赞自己的", self.call("POST", f"/jury/opinions/{mine[0]['id']}/like", user="juror"), 400, code="cannot_like_own")
        self.expect("举报：理由不在列表里", self.call("POST", f"/jury/opinions/{target['id']}/report", user="juror", body={"reason": "看他不爽"}), 422, code="invalid_reason")
        self.expect("举报：选「其他」但说明不足 5 个字", self.call("POST", f"/jury/opinions/{target['id']}/report", user="juror", body={"reason": "其他", "details": "不好"}), 422, code="invalid_reason")
        reported = self.expect("举报 POST /jury/opinions/{id}/report", self.call("POST", f"/jury/opinions/{target['id']}/report", user="juror", body={"reason": "与案件无关", "details": "联调自测"}))
        if reported is not None:
            self.verify("举报：返回 {id, state}", set(reported) == {"id", "state"} and reported["state"] in {"visible", "hidden"}, str(reported))
        self.expect("重复举报", self.call("POST", f"/jury/opinions/{target['id']}/report", user="juror", body={"reason": "人身攻击"}), 409, code="already_reported")
        self.expect("举报自己的", self.call("POST", f"/jury/opinions/{mine[0]['id']}/report", user="juror", body={"reason": "人身攻击"}), 400, code="cannot_report_own")
        deleted = self.expect("删除自己的 DELETE /jury/opinions/{id}", self.call("DELETE", f"/jury/opinions/{created['id']}", user="juror"))
        if deleted is not None:
            self.verify("删除：返回 {id, state: deleted}", deleted == {"id": created["id"], "state": "deleted"}, str(deleted))
        self.expect("删除别人的", self.call("DELETE", f"/jury/opinions/{target['id']}", user="juror"), 403, code="not_opinion_author")
        self.expect("不存在的观点", self.call("POST", "/jury/opinions/999999/like", user="juror"), 404)
        self.expect("没投过这个案件的委员点赞这条观点", self.call("POST", f"/jury/opinions/{target['id']}/like", user="senior3"), 404)
        after = self.get(f"/jury/cases/{case_id}/opinions", "juror")
        refreshed = {item["id"]: item for root in after["items"] for item in [root, *root["replies"]]}
        self.verify("操作之后重新拉列表", refreshed.get(target["id"], {}).get("reported") is True and created["id"] not in refreshed and (reply is None or reply["id"] in refreshed), f"total={after['total']}")

    # ------------------------------------------------------------ 我的评审

    def check_votes_and_stats(self) -> None:
        self.section("我的评审 / 个人统计（主账号 juror）")
        page = self.expect("GET /jury/votes", self.call("GET", "/jury/votes", user="juror"))
        if page is None:
            return
        for item in page["items"][:1]:
            self.keys("JuryVoteItem", item)
        times = [item["voted_at"] for item in page["items"]]
        self.verify("默认 20 条、按时间倒序", len(page["items"]) == 20 and times == sorted(times, reverse=True) and page["total"] >= 60, f"total={page['total']}")
        everything = self.all_votes("juror")
        self.verify("翻页能拿全，评审编号不重复", len(everything) == page["total"] and len(self.review_ids["juror"]) == page["total"], f"{len(everything)} / {page['total']}")
        self.votes = everything
        filters: list[tuple[str, Any]] = [
            ("state=open", lambda item: item["stage"] not in {None, "closed"}),
            ("state=closed", lambda item: item["stage"] == "closed"),
            ("outcome=violation", lambda item: item["outcome"] == "violation"),
            ("outcome=clean", lambda item: item["outcome"] == "clean"),
            ("agreed=true", lambda item: item["agreed"] is True),
            ("agreed=false", lambda item: item["agreed"] is False),
            ("vote=abstain", lambda item: item["vote"] == "abstain"),
            ("vote=l1,l2", lambda item: item["vote"] in {"l1", "l2"}),
            ("vote=recuse", lambda item: item["vote"] == "recuse"),
            ("vote=need_context", lambda item: item["vote"] == "need_context"),
            ("vote=recuse,need_context,abstain", lambda item: item["vote"] in {"recuse", "need_context", "abstain"}),
            ("state=closed&outcome=violation&agreed=false", lambda item: item["stage"] == "closed" and item["outcome"] == "violation" and item["agreed"] is False),
        ]
        for query, predicate in filters:
            data = self.expect(f"GET /jury/votes?{query}", self.call("GET", "/jury/votes", user="juror", query=f"{query}&limit=50"))
            if data is None:
                continue
            expected = sum(1 for item in everything if predicate(item))
            # 回避的记录 stage / outcome / agreed 都是 null，带这三种筛选时也不返回：按列表里的字段算出来的数应该正好相等
            self.verify(f"筛选 {query}", data["total"] == expected > 0 and all(predicate(item) for item in data["items"]), f"total={data['total']}（按列表里的字段算是 {expected}）")
        self.expect("契约里的原样查询串（空值 = 不筛选）", self.call("GET", "/jury/votes", user="juror", query="limit=20&offset=0&state=&outcome=&agreed=&vote="))
        paged = self.get("/jury/votes", "juror", "limit=5&offset=5")
        self.verify("limit / offset", [item["case_id"] for item in paged["items"]] == [item["case_id"] for item in everything[5:10]], str([item["case_id"] for item in paged["items"]]))
        self.expect("vote 取值不对", self.call("GET", "/jury/votes", user="juror", query="vote=bogus"), 422, code="invalid_vote_filter")
        self.verify("limit 超过 50", self.call("GET", "/jury/votes", user="juror", query="limit=51").status_code == 422, "422")
        recused = [item for item in everything if item["vote"] == "recuse"]
        good = len(recused) >= 2 and all(not item["can_view"] and item["agreed"] is None and item["outcome"] is None and item["opinion_count"] == 0 and item["voted_at"] for item in recused)
        self.verify("回避的记录：can_view=false，不透露结论", good and all(item["can_view"] for item in everything if item["vote"] != "recuse"), f"{len(recused)} 条")
        hidden = all(item["stage"] is None and item["closed_at"] is None and item["verdict_source"] is None and item["overturned"] is False and item["is_quality_check"] is False and item["public_case_id"] is None for item in recused)
        self.verify("回避的记录：stage 为 null，进展、结论、是不是质控题都不透露", hidden and all(item["stage"] is not None for item in everything if item["vote"] != "recuse"), str([(item["case_id"], item["stage"]) for item in recused]))
        self.verify("vote_stage：主账号的票都是原审投的", {item["vote_stage"] for item in everything} == {"jury"}, str({item["vote_stage"] for item in everything}))
        for item in recused[:1]:
            self.expect("回避的记录点不进详情", self.call("GET", f"/jury/cases/{item['case_id']}", user="juror"), 403, code="not_voted")
        # trainee2 回避过两个已经定案的案件（一个认定违规、一个不违规）：列表不该让他看出结论
        theirs = [item for item in self.all_votes("trainee2") if item["vote"] == "recuse"]
        queries = ("outcome=violation", "outcome=clean", "state=closed", "state=open", "agreed=true", "agreed=false")
        leaks = {query: [item["case_id"] for item in self.get("/jury/votes", "trainee2", f"vote=recuse&{query}")["items"]] for query in queries}
        shown = {(item["stage"], item["outcome"]) for item in theirs}
        self.verify("回避的记录带进展 / 结论筛选时不返回（trainee2 回避过的两个案件都已定案，一个违规一个不违规）", len(theirs) == 2 and shown == {(None, None)} and not any(leaks.values()), f"stage / outcome={shown}；GET /jury/votes?vote=recuse&… 各返回 {leaks}")
        own = {query: self.get("/jury/votes", "juror", f"vote=recuse&{query}")["total"] for query in queries}
        self.verify("主账号自己回避的记录同样筛不出来", set(own.values()) == {0} and self.get("/jury/votes", "juror", "vote=recuse")["total"] == len(recused), str(own))
        stages = {item["stage"] for item in everything if item["stage"]}
        self.verify("历史里各种情况都有", {"jury", "panel", "admin", "closed"} <= stages and any(item["overturned"] for item in everything) and any(item["is_quality_check"] for item in everything) and {"jury", "admin", "panel", "auto"} <= {item["verdict_source"] for item in everything if item["verdict_source"]} and {"none", "l1", "l2", "abstain", "need_context", "recuse"} <= {item["vote"] for item in everything}, f"stage={sorted(stages)} verdict_source={sorted({item['verdict_source'] for item in everything if item['verdict_source']})} vote={sorted({item['vote'] for item in everything})}")
        self.panel_open_review = next((item["case_id"] for item in everything if item["stage"] == "panel"), None)

        stats = self.expect("GET /jury/me/stats", self.call("GET", "/jury/me/stats", user="juror"))
        if stats is not None:
            self.keys("stats", stats)
            for day in stats["trend"][:1]:
                self.keys("stats.trend[]", day)
            counted = sum(1 for item in everything if item["vote"] != "recuse")
            self.verify("统计：数字自洽（回避不算票）", stats["votes_total"] == counted and stats["votes_agreed"] <= stats["votes_settled"] <= stats["votes_total"] and abs(stats["agreement_rate"] - stats["votes_agreed"] / stats["votes_settled"]) < 0.001 and stats["golden_correct"] <= stats["golden_total"], f"votes {stats['votes_total']}/{stats['votes_settled']}/{stats['votes_agreed']} 一致率 {stats['agreement_rate']} 质控 {stats['golden_correct']}/{stats['golden_total']} 弃权 {stats['abstain_total']} 观点 {stats['opinions_total']} 获赞 {stats['likes_received']}")
            self.verify("统计：trend 是 30 天且有形状", len(stats["trend"]) == 30 and sum(1 for day in stats["trend"] if day["votes"]) >= 15, f"有投票的天数={sum(1 for day in stats['trend'] if day['votes'])} 票数合计={sum(day['votes'] for day in stats['trend'])}")
        history = self.expect("GET /jury/history（旧接口保留）", self.call("GET", "/jury/history", user="juror"))
        if history is not None:
            self.verify("旧接口字段不变，case_id 同样是评审编号", set(history[0]) == {"case_id", "kind", "vote", "voted_at", "agreed", "outcome"} and {item["case_id"] for item in history} <= self.review_ids["juror"], str(sorted(history[0])))

        closed = [item for item in everything if item["stage"] == "closed" and not item["is_quality_check"] and item["can_view"]]
        samples = {
            "已定案（最近）": next((item for item in closed), None),
            "已改判": next((item for item in closed if item["overturned"]), None),
            "管理员定案": next((item for item in closed if item["verdict_source"] == "admin"), None),
            "等管理员": next((item for item in everything if item["stage"] == "admin"), None),
            "资深合议中": next((item for item in everything if item["stage"] == "panel"), None),
            "合议定案": next((item for item in closed if item["verdict_source"] == "panel"), None),
            "机器案件未作处理": next((item for item in closed if item["verdict_source"] == "auto"), None),
            "质控题": next((item for item in everything if item["is_quality_check"]), None),
            "定案超过 30 天": next((item for item in reversed(closed)), None),
        }
        for name, item in samples.items():
            if item is None:
                self.fail(f"回看：{name}", "主账号的历史里没有这种案件")
                continue
            detail = self.expect(f"回看：{name} GET /jury/cases/{{评审编号}}", self.call("GET", f"/jury/cases/{item['case_id']}", user="juror"))
            if detail is None:
                continue
            self.check_detail(f"回看：{name} 的结构", detail, item["vote"])
            if name == "已改判":
                self.verify("已改判：结论是新的，source 和 tally 保持原审的样子", detail["verdict"]["overturned"] is True and bool(detail["verdict"]["overturn_note"]) and detail["verdict"]["source"] == "jury", f"outcome={detail['verdict']['outcome']} source={detail['verdict']['source']} agreed={detail['my_vote']['agreed']} tally={detail['tally']} my_vote.points={detail['my_vote']['points']}")
            if name == "资深合议中":
                self.verify("合议中的案件：stage=panel，原审委员看不到票数分布和结论", detail["stage"] == "panel" and detail["tally"] is None and detail["verdict"] is None and detail["votes_cast"] == 5, f"stage={detail['stage']} votes_cast={detail['votes_cast']}")
            if name == "合议定案":
                opinions = self.get(f"/jury/cases/{item['case_id']}/opinions", "juror")
                flat = [entry for root in opinions["items"] for entry in [root, *root["replies"]]]
                good = detail["verdict"]["source"] == "panel" and detail["tally"]["panel"] == {"none": 0, "l1": 1, "l2": 1, "l3": 0} and detail["tally"]["none"] == 2 and detail["verdict"]["level"] == 1 and detail["my_vote"]["agreed"] is True
                self.verify("合议定案：verdict.source=panel，tally 多一项 panel；合议委员在观点区有发言", good and sum(1 for entry in flat if entry["tier"] == "senior") >= 3 and item["vote_stage"] == "jury" and bool(detail["public_case_id"]), f"tally={detail['tally']} 观点 {len(flat)} 条，其中资深委员 {sum(1 for entry in flat if entry['tier'] == 'senior')} 条")
            if name == "机器案件未作处理":
                good = detail["case_origin"] == "machine" and detail["report_count"] == 0 and detail["reasons"][0]["reason"].startswith("机器筛查") and detail["verdict"]["source"] == "auto" and detail["verdict"]["outcome"] == "clean"
                self.verify("「未作处理」的机器案件：verdict.source=auto、outcome=clean，票不计一致率，不进公示栏", good and detail["my_vote"]["agreed"] is None and detail["my_vote"]["points"] == 0 and detail["public_case_id"] is None and item["agreed"] is None and item["outcome"] == "clean", f"verdict={detail['verdict']['outcome']}/{detail['verdict']['source']} agreed={detail['my_vote']['agreed']}")
            if name == "定案超过 30 天":
                self.verify("超过保留期：只剩结论，没有内容", detail["content_available"] is False and detail["messages"] == [] and detail["post"] is None and detail["verdict"] is not None, f"closed_at={detail['verdict']['closed_at']}")
            if name == "已定案（最近）":
                self.verify("保留期内：内容还在", detail["content_available"] is True and (bool(detail["messages"]) or detail["post"] is not None), f"closed_at={detail['verdict']['closed_at']}")
                opinions = self.expect("已定案的观点区", self.call("GET", f"/jury/cases/{item['case_id']}/opinions", user="juror"))
                if opinions is not None and opinions["items"]:
                    self.verify("定案后 Opinion.vote 有值", all(opinion["vote"] for opinion in opinions["items"]), str([(opinion["alias"], opinion["vote"]) for opinion in opinions["items"]]))

    # ------------------------------------------------------------ 公示栏

    def check_public(self) -> None:
        self.section("公示栏（公开编号；主账号 juror + 普通用户 eligible）")
        page = self.expect("GET /jury/public/cases", self.call("GET", "/jury/public/cases", user="juror"))
        if page is None:
            return
        for item in page["items"][:1]:
            self.keys("PublicCase", item)
            self.keys("tally", item["tally"])
        times = [item["closed_at"] for item in page["items"]]
        cutoff = datetime.now(UTC) - timedelta(hours=72)
        self.verify("默认 20 条、按定案时间倒序、都满 72 小时", len(page["items"]) == 20 and times == sorted(times, reverse=True) and page["total"] > 20 and aware(times[0]) <= cutoff, f"total={page['total']} 最新一条定案于 {times[0]}")
        self.public_all()
        everything = self.public_items
        self.verify("翻页能拿全", len(everything) == page["total"], f"{len(everything)} / {page['total']}")
        self.verify("每条的 votes = tally 里的有效票数", all(item["votes"] == sum(count for name, count in item["tally"].items() if name not in {"abstain", "need_context"}) for item in everything), "ok")
        self.verify("票数分布有变化", len({json.dumps(item["tally"], sort_keys=True) for item in everything}) >= 6, f"{len({json.dumps(item['tally'], sort_keys=True) for item in everything})} 种不同的 tally")
        self.verify("不违规的没有类别和等级", all(item["category"] is None and item["level"] is None for item in everything if item["outcome"] == "clean") and all(item["category"] and item["category_label"] and item["level"] is not None for item in everything if item["outcome"] == "violation"), "ok")
        for query, predicate in [
            ("kind=post", lambda item: item["kind"] == "post"),
            ("kind=message", lambda item: item["kind"] == "message"),
            ("outcome=violation", lambda item: item["outcome"] == "violation"),
            ("outcome=clean", lambda item: item["outcome"] == "clean"),
            ("category=privacy", lambda item: item["category"] == "privacy"),
            ("category=advertising", lambda item: item["category"] == "advertising"),
            ("kind=post&outcome=violation", lambda item: item["kind"] == "post" and item["outcome"] == "violation"),
        ]:
            data = self.expect(f"GET /jury/public/cases?{query}", self.call("GET", "/jury/public/cases", user="juror", query=f"{query}&limit=50"))
            if data is not None:
                expected = sum(1 for item in everything if predicate(item))
                self.verify(f"筛选 {query}", data["total"] == expected and expected > 0 and all(predicate(item) for item in data["items"]), f"total={data['total']}（按全量数据算应为 {expected}）")
        self.expect("契约里的原样查询串（空值 = 不筛选）", self.call("GET", "/jury/public/cases", user="juror", query="limit=20&offset=0&kind=&outcome=&category="))

        reasons: dict[str | None, dict[str, Any]] = {}
        for item in everything:
            detail = self.call("GET", f"/jury/public/cases/{item['case_id']}", user="eligible")
            if detail.status_code != 200:
                self.fail(f"GET /jury/public/cases/{item['case_id']}", f"{detail.status_code} {detail.text[:200]}")
                continue
            body = detail.json()
            self.scan_datetimes("GET /jury/public/cases/{id}", body)
            if body["has_content"] != (body["content"] is not None) or (body["content"] is None) != (body["content_hidden_reason"] is not None):
                self.fail(f"公示详情 #{item['case_id']}", f"has_content={body['has_content']} content={body['content']} content_hidden_reason={body['content_hidden_reason']}")
            reasons.setdefault(body["content_hidden_reason"], body)
        self.ok(f"GET /jury/public/cases/{{id}} × {len(everything)}（普通用户 eligible）", f"content_hidden_reason 的取值：{sorted(str(key) for key in reasons)}")
        self.verify("各种公示形态都有", set(reasons) == {None, "group_message", "clean", "sensitive", "overturned"}, str(sorted(str(key) for key in reasons)))
        if None in reasons:
            body = reasons[None]
            self.keys("PublicCaseDetail", body)
            self.keys("PublicCaseDetail.content", body["content"])
            self.verify("带内容的：公开动态、认定违规、只给文字和图片数量，时间带 Z", body["kind"] == "post" and body["outcome"] == "violation" and bool(body["content"]["text"]) and "/uploads/" not in json.dumps(body) and str(body["content"]["created_at"]).endswith("Z"), f"#{body['case_id']} text={body['content']['text'][:24]}… image_count={body['content']['image_count']} created_at={body['content']['created_at']}")
        for reason, label in (("group_message", "群聊消息"), ("clean", "不违规"), ("sensitive", "隐私类"), ("overturned", "已改判")):
            body = reasons.get(reason)
            if body is not None:
                extra = f" overturned={body['overturned']} overturn_note={body['overturn_note']}" if reason == "overturned" else ""
                self.verify(f"不带内容：{label}", body["content"] is None and body["has_content"] is False and (reason != "overturned" or (body["overturned"] and bool(body["overturn_note"]))), f"#{body['case_id']} kind={body['kind']} outcome={body['outcome']} category={body['category']} source={body['source']}{extra}")
        self.verify("公示栏里各种定案来源都有", {"jury", "admin", "panel"} <= {item["source"] for item in everything}, str(sorted({item["source"] for item in everything})))
        by_panel = next((item for item in everything if item["source"] == "panel"), None)
        if by_panel is not None:
            self.verify("合议定案的案件：公示栏的 tally / votes 仍只是原审的票", set(by_panel["tally"]) == CONTRACT["tally"] and by_panel["votes"] == 5, f"#{by_panel['case_id']} tally={by_panel['tally']} votes={by_panel['votes']}")
        machine = [item for item in everything if item["case_origin"] == "machine"]
        self.verify("机器来源的案件进了公示栏：report_count=0，reasons 是一项「机器筛查 · 类别」", len(machine) == 1 and machine[0]["report_count"] == 0 and machine[0]["reasons"] == [{"reason": "机器筛查 · 诈骗", "count": 1}] and machine[0]["outcome"] == "violation" and machine[0]["has_content"] is True and all(item["case_origin"] == "report" for item in everything if item not in machine), str([(item["case_id"], item["reasons"]) for item in machine]))
        if machine:
            body = self.get(f"/jury/public/cases/{machine[0]['case_id']}", "eligible")
            self.verify("机器案件的公示详情：同样带 case_origin，公开动态带内容", body.get("case_origin") == "machine" and bool((body.get("content") or {}).get("text")), f"content={str((body.get('content') or {}).get('text'))[:20]}…")
        linked = [item for item in everything if item["precedent_id"]]
        self.verify("成了判例的案件带 precedent_id", len(linked) >= 4 and len({item["precedent_id"] for item in linked}) == len(linked), f"{len(linked)} 个：{[(item['case_id'], item['precedent_id']) for item in linked]}")
        closed = [item for item in getattr(self, "votes", []) if item["stage"] == "closed" and not item["is_quality_check"] and item["can_view"]]
        recent = [item for item in closed if aware(item["closed_at"]) > cutoff]
        self.verify("定案不足 72 小时的还没进公示栏（public_case_id 是 null）", len(recent) >= 3 and all(item["public_case_id"] is None for item in recent), f"{len(recent)} 个")
        # 「未作处理」的机器案件（verdict_source=auto）不是结论，本来就不进公示栏
        held = [item for item in closed if aware(item["closed_at"]) <= cutoff and item["public_case_id"] is None and item["verdict_source"] != "auto"]
        self.verify("定案满 72 小时却不在公示栏的只有一个（它的处罚有待处理的申诉）", len(held) == 1, f"{len(held)} 个")
        self.held_back = held
        self.expect("不存在 / 没公示的公开编号", self.call("GET", "/jury/public/cases/999999", user="juror"), 404, code="case_not_found")
        self.expect("公示案件的观点区：开关默认关", self.call("GET", f"/jury/public/cases/{everything[0]['case_id']}/opinions", user="juror"), 403, code="opinions_closed")
        self.expect("公示案件的观点区：不是在任委员", self.call("GET", f"/jury/public/cases/{everything[0]['case_id']}/opinions", user="eligible2"), 403, code="not_juror")

    # ------------------------------------------------------------ 其他账号

    def check_other_roles(self) -> None:
        self.section("其他账号的视角")
        overview = self.expect("eligible2 GET /jury/overview", self.call("GET", "/jury/overview", user="eligible2"))
        if overview is not None:
            self.verify("普通用户的总览：me 是非委员", overview["me"] == {"status": "none", "tier": None, "available_cases": None, "voted_today": 0, "daily_limit": 30}, str(overview["me"]))
        self.expect("eligible2 GET /jury/config", self.call("GET", "/jury/config", user="eligible2"))
        empty = self.expect("eligible2 GET /jury/votes", self.call("GET", "/jury/votes", user="eligible2"))
        if empty is not None:
            self.verify("没当过委员：空列表", empty == {"items": [], "total": 0}, str(empty))
        zero = self.expect("eligible2 GET /jury/me/stats", self.call("GET", "/jury/me/stats", user="eligible2"))
        if zero is not None:
            self.verify("没当过委员：全 0", all(value == 0 for key, value in zero.items() if key != "trend") and len(zero["trend"]) == 30, "ok")
        points = self.expect("eligible2 GET /jury/me/points", self.call("GET", "/jury/me/points", user="eligible2"))
        if points is not None:
            self.verify("没当过委员：风纪值 0、明细为空", points == {"total": 0, "items": [], "count": 0}, str(points))
        expectations = {
            "eligible2": ("none", True), "newbie": ("none", False), "applicant": ("applied", True), "suspended": ("suspended", None), "expired": ("expired", None),
            "mutedjuror": ("active", None), "trainee1": ("active", None), "senior3": ("active", None), "hesitant": ("active", None), "cooldown": ("active", None),
        }
        statuses: dict[str, dict[str, Any]] = {}
        for username, (status, eligible) in expectations.items():
            me = self.expect(f"{username} GET /jury/me", self.call("GET", "/jury/me", user=username))
            if me is None:
                continue
            statuses[username] = me
            self.keys("JuryStatus", me)
            failed = [item["text"] for item in me["requirements"] if not item["ok"]]
            self.verify(f"{username} 的状态", me["status"] == status and (eligible is None or me["eligible"] is eligible), f"status={me['status']} eligible={me['eligible']} tier={me['tier']} points={me['points']} available_cases={me['available_cases']} 未达标={failed}")
        if "eligible2" in statuses:
            me = statuses["eligible2"]
            self.verify("非委员：层级进度 / 回避名额 / 恢复时间都是 null，warnings 为空", me["tier_progress"] is None and me["recuse_left_today"] is None and me["daily_resets_at"] is None and me["recuse_resets_at"] is None and me["warnings"] == [] and me["points"] == 0 and me["badge_visible"] is False, "ok")
        if "suspended" in statuses:
            me = statuses["suspended"]
            self.verify("被暂停：tier / available_cases / tier_progress 是 null，带 review_note", me["tier"] is None and me["available_cases"] is None and me["tier_progress"] is None and bool(me["review_note"]), f"review_note={me['review_note']}")
            self.expect("被暂停：不能取案", self.call("GET", "/jury/cases/next", user="suspended"), 403, code="not_juror")
            real = next((item for item in self.all_votes("suspended") if not item["is_quality_check"] and item["can_view"]), None)
            if real is not None:
                self.expect("被暂停：仍可回看投过的案件", self.call("GET", f"/jury/cases/{real['case_id']}", user="suspended"))
                self.expect("被暂停：仍可读观点区", self.call("GET", f"/jury/cases/{real['case_id']}/opinions", user="suspended"))
                self.expect("被暂停：不能再发观点", self.call("POST", f"/jury/cases/{real['case_id']}/opinions", user="suspended", body={"content": "我还能说话吗"}), 403, code="not_juror")
            self.check_notification("suspended", "suspended")
        if "mutedjuror" in statuses:
            me = statuses["mutedjuror"]
            restriction = me["restriction"] or {}
            self.verify("禁言中的委员：restriction", restriction.get("reason") == "muted" and bool(restriction.get("until")) and me["available_cases"] == 0 and me["tier"] == "trainee", json.dumps(restriction, ensure_ascii=False))
            self.expect("禁言中的委员：不能取案", self.call("GET", "/jury/cases/next", user="mutedjuror"), 403, code="juror_restricted")
            real = next((item for item in self.all_votes("mutedjuror") if not item["is_quality_check"] and item["can_view"]), None)
            if real is not None:
                self.expect("禁言中的委员：回看不受影响", self.call("GET", f"/jury/cases/{real['case_id']}", user="mutedjuror"))
                self.expect("禁言中的委员：不能发观点", self.call("POST", f"/jury/cases/{real['case_id']}/opinions", user="mutedjuror", body={"content": "我被禁言了"}), 403, code="juror_restricted")
            practice = self.expect("禁言中的委员：练习场不拦", self.call("GET", "/jury/practice/next", user="mutedjuror"))
            if practice is not None:
                self.verify("禁言中也能拿到练习题", practice["question"] is not None, f"round={practice['round']}")
        if "expired" in statuses:
            self.verify("任期届满：tier 是 null，有违规记录所以没有自动续任", statuses["expired"]["tier"] is None and statuses["expired"]["term_ends_at"] is not None and not statuses["expired"]["eligible"], f"term_ends_at={statuses['expired']['term_ends_at']}")
            self.expect("任期届满：不能取案", self.call("GET", "/jury/cases/next", user="expired"), 403, code="not_juror")
            self.check_notification("expired", "term_expired")
        if "senior3" in statuses:
            warning = next((item for item in statuses["senior3"]["warnings"] if item["code"] == "demote_risk"), None)
            if self.verify("senior3 的 demote_risk 预警", warning is not None and statuses["senior3"]["tier_progress"] == {"next_tier": None, "requirements": []}, json.dumps(statuses["senior3"]["warnings"], ensure_ascii=False)):
                self.keys("JuryStatus.warnings[]", warning)
                self.verify("预警带 current / threshold / sample", 0.7 <= warning["current"] < 0.8 and warning["threshold"] == 0.7 and warning["sample"] >= 15, f"current={warning['current']} threshold={warning['threshold']} sample={warning['sample']}")
        if "hesitant" in statuses:
            warning = next((item for item in statuses["hesitant"]["warnings"] if item["code"] == "high_abstain"), None)
            self.verify("hesitant 的 high_abstain 预警，还没到冷却", warning is not None and 0.3 < warning["current"] <= 0.5 and warning["sample"] >= 20 and statuses["hesitant"]["cooldown_until"] is None, json.dumps(statuses["hesitant"]["warnings"], ensure_ascii=False)[:300])
        if "cooldown" in statuses:
            me = statuses["cooldown"]
            self.verify("cooldown 在弃权冷却中", bool(me["cooldown_until"]) and aware(me["cooldown_until"]) > datetime.now(UTC) and me["available_cases"] == 0, f"cooldown_until={me['cooldown_until']}")
            blocked = self.expect("冷却中取案", self.call("GET", "/jury/cases/next", user="cooldown"), 429, code="jury_cooldown")
            if blocked is not None:
                self.verify("429 jury_cooldown 带 detail.until", blocked["detail"].get("until") == me["cooldown_until"], f"until={blocked['detail'].get('until')}")
        case_id = getattr(self, "discussion_case", None)
        if case_id is not None:
            for username in ("newbie", "senior3"):
                self.expect(f"{username} 拿主账号的评审编号读详情", self.call("GET", f"/jury/cases/{case_id}", user=username), 404, code="case_not_found")
        self.expect("普通用户不能取案", self.call("GET", "/jury/cases/next", user="eligible2"), 403, code="not_juror")
        standing = self.expect("offender GET /moderation/me", self.call("GET", "/moderation/me", user="offender"))
        if standing is not None:
            appealable = [item for item in standing["violations"] if item["can_appeal"]]
            self.verify("offender 有一条可以申诉的处罚", len(appealable) == 1 and appealable[0]["source"] == "jury", f"{[(item['category_label'], item['level'], item['penalty_kind'], item['appeal_status']) for item in standing['violations']]}")
        standing = self.expect("appellant GET /moderation/me", self.call("GET", "/moderation/me", user="appellant"))
        if standing is not None:
            pending = [item for item in standing["violations"] if item["appeal_status"] == "pending"]
            self.verify("appellant 的申诉是开关打开之前提交的：在管理员手里（appeal_stage=admin）", len(pending) == 1 and pending[0]["appeal_stage"] == "admin" and pending[0]["appeal_result"] is None, f"appeal_stage={pending[0]['appeal_stage'] if pending else None}")
            self.verify("appellant 有一条申诉待处理的处罚", len(pending) == 1 and pending[0]["can_appeal"] is False, f"{[(item['category_label'], item['appeal_status']) for item in standing['violations']]}")

    def check_recuse_limit(self, username: str) -> None:
        self.section(f"回避上限（{username} 连续回避）")
        before = self.get("/jury/me", username)
        limit = self.config["recuse"]["daily_limit"]
        self.verify("回避之前：名额是满的，recuse_resets_at 是 null", before["recuse_left_today"] == limit and before["recuse_resets_at"] is None, f"recuse_left_today={before['recuse_left_today']}")
        blocked: dict[str, Any] | None = None
        done = 0
        for _ in range(limit + 1):
            view = self.get("/jury/cases/next", username).get("case")
            if view is None:
                break
            response = self.call("POST", f"/jury/cases/{view['case_id']}/vote", user=username, body={"vote": "recuse"})
            if response.status_code == 429:
                blocked = response.json()
                break
            done += 1
        after = self.get("/jury/me", username)
        good = done == limit and blocked is not None and blocked["detail"].get("code") == "recuse_limit" and after["recuse_left_today"] == 0 and bool(after["recuse_resets_at"])
        self.verify(f"回避 {limit} 次之后第 {limit + 1} 次是 429 recuse_limit，detail.resets_at 和 /jury/me.recuse_resets_at 一致", good and blocked["detail"].get("resets_at") == after["recuse_resets_at"] and after["voted_today"] == before["voted_today"], f"回避了 {done} 次；{json.dumps((blocked or {}).get('detail'), ensure_ascii=False)}")

    def check_notification(self, username: str, kind: str) -> None:
        data = self.get("/notifications", username)
        items = [item for item in data.get("items", []) if item["type"] == "jury"]
        self.verify(f"{username} 的通知中心里有 type=jury 的通知（{kind}）", any((item.get("payload") or {}).get("kind") == kind and item["category"] == "system" for item in items), str([(item["title"], (item.get("payload") or {}).get("kind")) for item in items]))

    # ------------------------------------------------------------ 二期：层级、风纪值、成就、排行榜、身份

    def check_progress(self) -> None:
        self.section("层级与晋升（trainee1：条件已满足，他投过的案件一定案就晋升）")
        me = self.get("/jury/me", "trainee1")
        if getattr(self, "voted", None) and any(item["tag"] == "settle_clean" and item["settled"] for item in self.voted):
            self.verify("主账号刚才定案的那个案件里有 trainee1 的票：他已经自动晋升为正式委员", me["tier"] == "regular" and me["tier_progress"]["next_tier"] == "senior", f"tier={me['tier']}")
            self.check_notification("trainee1", "promoted")
            earned = {item["key"] for item in self.get("/jury/me/achievements", "trainee1")["items"] if item["earned_at"]}
            self.verify("晋升顺带拿到「正式委员」成就", "tier_regular" in earned, str(sorted(earned)))
        else:
            self.verify("trainee1 还是见习，四项条件都已满足", me["tier"] == "trainee" and all(item["ok"] for item in me["tier_progress"]["requirements"]), json.dumps(me["tier_progress"], ensure_ascii=False)[:300])
        self.check_notification("regular2", "promoted")

        self.section("风纪值、成就、排行榜、公开身份（主账号 juror）")
        mine = self.all_votes("juror")
        page = self.expect("GET /jury/me/points", self.call("GET", "/jury/me/points", user="juror"))
        if page is not None:
            self.keys("PointPage", page)
            for item in page["items"][:1]:
                self.keys("PointPage.items[]", item)
            logs: list[dict[str, Any]] = []
            while len(logs) < page["count"]:
                chunk = self.get("/jury/me/points", "juror", f"limit=50&offset={len(logs)}")["items"]
                if not chunk:
                    break
                logs.extend(chunk)
            me = self.get("/jury/me", "juror")
            times = [item["created_at"] for item in logs]
            self.verify("明细：默认 20 条、时间倒序、合计等于总分", len(page["items"]) == 20 and times == sorted(times, reverse=True) and len(logs) == page["count"] and sum(item["points"] for item in logs) == page["total"] == me["points"], f"total={page['total']} count={page['count']}")
            kinds = {item["kind"] for item in logs}
            self.verify("明细里的种类", kinds <= POINT_KINDS and {"vote_agreed", "vote_disagreed", "quality_correct", "quality_wrong", "daily_first", "streak_7", "opinion_liked", "overturn_adjust", "admin_adjust"} <= kinds and all(item["label"] for item in logs), str(sorted(kinds)))
            with_case = [item for item in logs if item["case_id"] is not None]
            self.verify("明细里的 case_id 是我的评审编号；每日首票、连续 7 天、管理员调整没有 case_id", len(with_case) > 20 and {item["case_id"] for item in with_case} <= self.review_ids["juror"] and all(item["case_id"] is None for item in logs if item["kind"] in {"daily_first", "streak_7", "admin_adjust"}), f"{len(with_case)} 条带评审编号")
            self.verify("有负分的流水（质控题答错、改判补差）", any(item["points"] < 0 for item in logs), str(sorted({(item["kind"], item["points"]) for item in logs if item["points"] < 0})))
        achievements = self.expect("GET /jury/me/achievements", self.call("GET", "/jury/me/achievements", user="juror"))
        if achievements is not None:
            items = achievements["items"]
            for item in items[:1]:
                self.keys("Achievement", item)
            earned = {item["key"] for item in items if item["earned_at"]}
            progress = [item for item in items if not item["earned_at"] and 0 < item["current"] < item["target"]]
            self.verify("成就：12 个定义，已获得若干、若干有进度", {item["key"] for item in items} == ACHIEVEMENTS and {"first_vote", "votes_10", "streak_7", "liked_10", "tier_regular"} <= earned and len(progress) >= 2 and all(item["current"] == item["target"] for item in items if item["earned_at"]), f"已获得 {sorted(earned)}；进行中 {[(item['key'], item['current'], item['target']) for item in progress]}")
        boards: dict[str, dict[str, Any]] = {}
        for period in ("week", "month", "all"):
            board = self.expect(f"GET /jury/leaderboard?period={period}", self.call("GET", "/jury/leaderboard", user="juror", query=f"period={period}"))
            if board is None:
                continue
            boards[period] = board
            self.keys("Leaderboard", board)
            rows = board["items"]
            for row in rows[:1]:
                self.keys("Leaderboard.items[]", row)
            mine_rows = [row for row in rows if row["me"]]
            good = board["period"] == period and 8 <= len(rows) <= 20 and [row["rank"] for row in rows] == list(range(1, len(rows) + 1)) and [row["points"] for row in rows] == sorted((row["points"] for row in rows), reverse=True)
            good = good and all(row["points"] > 0 for row in rows) and len(mine_rows) == 1 and board["me"] == mine_rows[0] and any(row["user"] for row in rows) and any(row["user"] is None for row in rows)
            self.verify(f"{period} 榜：十几个人、名次连续、分数递减、有公开的也有匿名的、主账号在榜且 me 和榜单行同形", good, f"{len(rows)} 人；我是第 {board['me']['rank'] if board['me'] else None} 名 {board['me']['points'] if board['me'] else None} 分；公开身份的 {[row['user']['username'] for row in rows if row['user']]}")
            self.verify(f"{period} 榜：公开身份的带 jury_badge，等于他的层级", all(row["user"]["jury_badge"] == row["tier"] for row in rows if row["user"]), "ok")
        if {"week", "all"} <= set(boards):
            self.verify("周榜的分数不超过总榜", boards["week"]["me"]["points"] <= boards["month"]["me"]["points"] <= boards["all"]["me"]["points"], f"周 {boards['week']['me']['points']} / 月 {boards['month']['me']['points']} / 总 {boards['all']['me']['points']}")
        self.expect("period 取值不对", self.call("GET", "/jury/leaderboard", user="juror", query="period=year"), 422)
        board = self.expect("非委员看排行榜", self.call("GET", "/jury/leaderboard", user="eligible2", query="period=all"))
        if board is not None:
            self.verify("非委员：me 是 null", board["me"] is None and all(not row["me"] for row in board["items"]), "ok")

        user_id = self.users["juror"]["id"]
        before = self.get(f"/users/{user_id}", "eligible2")
        self.verify("公开身份之前：别人看到的 UserPublic.jury_badge 是 null", before.get("jury_badge") is None, f"jury_badge={before.get('jury_badge')}")
        status = self.expect("PUT /jury/me/settings {badge_visible: true}", self.call("PUT", "/jury/me/settings", user="juror", body={"badge_visible": True}))
        if status is not None:
            self.keys("JuryStatus", status)
            after = self.get(f"/users/{user_id}", "eligible2")
            board = self.get("/jury/leaderboard", "juror", "period=all")
            self.verify("打开之后：返回的是 JuryStatus，UserPublic.jury_badge 变成层级，排行榜上也公开了", status["badge_visible"] is True and after.get("jury_badge") == "regular" and self.get("/auth/me", "juror").get("jury_badge") == "regular" and (board["me"]["user"] or {}).get("username") == "juror", f"jury_badge={after.get('jury_badge')}")
        status = self.expect("PUT /jury/me/settings {badge_visible: false}", self.call("PUT", "/jury/me/settings", user="juror", body={"badge_visible": False}))
        if status is not None:
            self.verify("关掉之后：徽章撤下，排行榜上重新匿名", status["badge_visible"] is False and self.get(f"/users/{user_id}", "eligible2").get("jury_badge") is None and self.get("/jury/leaderboard", "juror", "period=all")["me"]["user"] is None, "ok")
        self.expect("从没当过委员的人改设置", self.call("PUT", "/jury/me/settings", user="newbie", body={"badge_visible": True}), 403, code="not_juror")
        conversations = self.get("/conversations", "eligible2")
        plaza = next((item for item in conversations if item.get("title") == "创作大厅"), None) if isinstance(conversations, list) else None
        if plaza is None:
            self.fail("在会话列表里找「创作大厅」", str(conversations)[:200])
        else:
            messages = self.call("GET", f"/conversations/{plaza['id']}/messages", user="eligible2", query="limit=30")
            badges = {message["sender"]["username"]: message["sender"].get("jury_badge") for message in messages.json()} if messages.status_code == 200 else {}
            self.verify("大厅最近的消息里，公开了身份的委员带着徽章，路人没有", {name: badge for name, badge in badges.items() if badge} == {"senior1": "senior", "senior2": "senior", "regular2": "regular"} and any(badge is None for badge in badges.values()), f"{messages.status_code} {badges}")
        del mine

    # ------------------------------------------------------------ 二期：考试

    def answer_key(self) -> dict[tuple[Any, ...], dict[str, Any]]:
        """用后台的题库接口建一张「题目内容 -> 标准答案」的表（考试题不带编号，只能按内容认）。"""
        questions: list[dict[str, Any]] = []
        while True:
            page = self.get("/admin/jury/questions", "admin", f"limit=200&offset={len(questions)}")
            questions.extend(page["items"])
            if len(questions) >= page["total"] or not page["items"]:
                break
        self.questions = questions
        key: dict[tuple[Any, ...], dict[str, Any]] = {}
        for question in questions:
            detail = self.get(f"/admin/jury/questions/{question['case_id']}", "admin")
            key[self.content_key(detail)] = detail
        return key

    def check_exam(self) -> None:
        self.section("入门考试")
        key = self.answer_key()
        state = self.expect("eligible GET /jury/exam", self.call("GET", "/jury/exam", user="eligible"))
        if state is not None:
            self.keys("ExamState", state)
            self.verify("没考过：可以直接开考", state["available"] and state["eligible"] and not state["passed"] and state["attempts_used"] == 0 and state["attempts_max"] == 3 and state["next_attempt_at"] is None and state["last_result"] is None and state["active"] is None and state["total"] == 10 and state["pass_score"] == 8, f"server_time={state['server_time']}")
        started = self.expect("eligible POST /jury/exam/start", self.call("POST", "/jury/exam/start", user="eligible"))
        if started is None:
            return
        self.keys("ExamStart", started)
        questions = started["questions"]
        for question in questions:
            self.keys("ExamQuestion", question, optional={"case_origin"})
            self.check_content("考试题", {**question, "case_id": None})
        minutes = (aware(started["expires_at"]) - aware(started["server_time"])).total_seconds() / 60
        opened = aware(started["server_time"])
        shown_ages = sorted(round((opened - aware(self.signature(question)[2])).total_seconds() / 3600, 1) for question in questions)
        self.verify("考试题同样是旧案件：内容的时间平移到开考之前 2–40 小时", 2 <= shown_ages[0] and shown_ages[-1] <= 40.5, f"被举报内容发于开考之前 {shown_ages} 小时")
        self.verify("config.exam.grace_seconds 是 90", self.config["exam"]["grace_seconds"] == 90, f"grace_seconds={self.config['exam']['grace_seconds']}")
        self.verify("10 道题、题号 1–10、不带任何案件编号、限时 60 分钟", [question["question"] for question in questions] == list(range(1, 11)) and all("case_id" not in question and "question_id" not in question for question in questions) and 59 <= minutes <= 60, f"expires_at={started['expires_at']}")
        known = [key.get(self.content_key(question)) for question in questions]
        self.verify("考试题都来自题库里已核对、用于计分的那一份", all(item is not None and item["verified"] and item["pool"] == "scored" for item in known), str([(item or {}).get("pool") for item in known]))
        again = self.get("/jury/exam", "eligible")
        resumed = self.call("POST", "/jury/exam/start", user="eligible").json()
        self.verify("进行中：GET /jury/exam 的 active 和再次开考返回的都是同一场", (again["active"] or {}).get("attempt_id") == started["attempt_id"] == resumed.get("attempt_id") and [q["question"] for q in again["active"]["questions"]] == list(range(1, 11)) and again["attempts_used"] == 1, f"attempt_id={started['attempt_id']}")
        attempt = started["attempt_id"]
        self.expect("别人交我的卷", self.call("POST", f"/jury/exam/{attempt}/submit", user="eligible2", body={"answers": []}), 404, code="exam_not_found")
        self.expect("题号不存在", self.call("POST", f"/jury/exam/{attempt}/submit", user="eligible", body={"answers": [{"question": 11, "vote": "none"}]}), 422, code="invalid_answers")
        self.expect("选项不合法", self.call("POST", f"/jury/exam/{attempt}/submit", user="eligible", body={"answers": [{"question": 1, "vote": "abstain"}]}), 422, code="invalid_answers")
        if not all(known):
            return
        answers = [{"question": question["question"], "vote": "l2" if item["outcome"] == "violation" else "none"} for question, item in zip(questions, known)]
        result = self.expect("eligible 交卷（全部按标准答案）", self.call("POST", f"/jury/exam/{attempt}/submit", user="eligible", body={"answers": answers}))
        if result is not None:
            self.keys("ExamResult", result)
            self.keys("JuryStatus", result["status"])
            for item in result["results"][:1]:
                self.keys("ExamResult.results[]", item)
                self.keys("answer", item["answer"])
            good = result["score"] == 10 and result["total"] == 10 and result["passed"] and all(item["correct"] for item in result["results"]) and result["attempts_left"] == 2 and result["next_attempt_at"] is None
            self.verify("通过：10 分，每题给出标准答案；没开自动任命，状态还是普通用户", good and result["status"]["status"] == "none" and result["status"]["exam"]["passed"] and result["status"]["exam"]["last_score"] == 10, f"score={result['score']} 讲解 {sum(1 for item in result['results'] if item['answer']['explanation'])} 道 status={result['status']['status']}")
        again = self.expect("重复交卷", self.call("POST", f"/jury/exam/{attempt}/submit", user="eligible", body={"answers": answers}), 409, code="exam_closed")
        if again is not None:
            self.verify("409 exam_closed 带 detail.reason（已经交过卷是 submitted）", again["detail"].get("reason") == "submitted", str(again["detail"]))
        self.expect("通过之后再开考", self.call("POST", "/jury/exam/start", user="eligible"), 409, code="exam_passed")
        applied = self.expect("eligible POST /jury/apply（考过了，auto_appoint 关）", self.call("POST", "/jury/apply", user="eligible", body={"statement": "联调自测：考过了来申请"}))
        if applied is not None:
            self.verify("进审批队列", applied["status"] == "applied" and applied["exam"]["passed"] is True, f"status={applied['status']}")
            row = next((item for item in self.get("/admin/jury/members", "admin", "status=applied")["items"] if item["user"]["username"] == "eligible"), {})
            self.verify("后台审批队列里显示考试成绩", row.get("exam_score") == 10 and bool(row.get("exam_passed_at")), f"exam_score={row.get('exam_score')} exam_passed_at={row.get('exam_passed_at')}")

        started = self.expect("eligible2 POST /jury/exam/start", self.call("POST", "/jury/exam/start", user="eligible2"))
        if started is not None:
            known = [key.get(self.content_key(question)) for question in started["questions"]]
            answers = [{"question": question["question"], "vote": "none" if item["outcome"] == "violation" else "l1"} for question, item in list(zip(started["questions"], known))[:4] if item]
            answers += [{"question": question["question"], "vote": "l2" if item["outcome"] == "violation" else "none"} for question, item in list(zip(started["questions"], known))[4:7] if item]
            result = self.expect("eligible2 交卷（4 道故意答错、3 道答对、3 道没答）", self.call("POST", f"/jury/exam/{started['attempt_id']}/submit", user="eligible2", body={"answers": answers}))
            if result is not None:
                unanswered = [item for item in result["results"] if item["your_vote"] is None]
                good = result["score"] == 3 and not result["passed"] and len(unanswered) == 3 and all(not item["correct"] and item["answer"] for item in unanswered) and result["attempts_left"] == 2
                self.verify("没通过：没答的按答错算（your_vote 为 null），24 小时后才能重考", good and bool(result["next_attempt_at"]) and 23 < (aware(result["next_attempt_at"]) - datetime.now(UTC)).total_seconds() / 3600 <= 24, f"score={result['score']} next_attempt_at={result['next_attempt_at']}")
            cooldown = self.expect("没通过之后马上重考", self.call("POST", "/jury/exam/start", user="eligible2"), 429, code="exam_cooldown")
            if cooldown is not None and result is not None:
                self.verify("429 exam_cooldown 带 detail.next_attempt_at", cooldown["detail"].get("next_attempt_at") == result["next_attempt_at"], str(cooldown["detail"].get("next_attempt_at")))
        state = self.expect("examfail GET /jury/exam", self.call("GET", "/jury/exam", user="examfail"))
        if state is not None:
            self.keys("ExamState.last_result", state["last_result"])
            self.verify("考过没通过：last_result、next_attempt_at", state["last_result"]["score"] == 6 and not state["last_result"]["passed"] and state["attempts_used"] == 1 and bool(state["next_attempt_at"]), f"last_result={state['last_result']} next_attempt_at={state['next_attempt_at']}")
        self.expect("examfail 冷却中开考", self.call("POST", "/jury/exam/start", user="examfail"), 429, code="exam_cooldown")
        state = self.get("/jury/exam", "examout")
        exhausted = self.expect("examout 次数用尽后开考", self.call("POST", "/jury/exam/start", user="examout"), 429, code="exam_attempts_exhausted")
        if exhausted is not None:
            self.verify("30 天内 3 次用完：要等最早那一场滑出窗口", state["attempts_used"] == 3 and exhausted["detail"].get("next_attempt_at") == state["next_attempt_at"] and aware(state["next_attempt_at"]) > datetime.now(UTC) + timedelta(days=3), f"attempts_used={state['attempts_used']} next_attempt_at={state['next_attempt_at']}")
        self.expect("newbie（不满足条件）开考", self.call("POST", "/jury/exam/start", user="newbie"), 403, code="jury_not_eligible")
        state = self.get("/jury/exam", "newbie")
        self.verify("不满足条件：eligible=false", state["eligible"] is False and state["available"] is True, f"eligible={state['eligible']}")

        self.expect("PUT /admin/jury/settings {exam_required: true}", self.call("PUT", "/admin/jury/settings", user="admin", body={"exam_required": True}))
        self.verify("/jury/me.exam_required 跟着变", self.get("/jury/me", "eligible2")["exam_required"] is True, "true")
        self.expect("exam_required 打开：没考过的人申请", self.call("POST", "/jury/apply", user="eligible2", body={}), 403, code="exam_required")
        self.expect("PUT /admin/jury/settings {exam_required: false, auto_appoint: true}", self.call("PUT", "/admin/jury/settings", user="admin", body={"exam_required": False, "auto_appoint": True}))
        self.verify("config.switches.auto_appoint 跟着变", self.get("/jury/config", "exampass")["switches"]["auto_appoint"] is True, "true")
        appointed = self.expect("exampass POST /jury/apply（考过了，auto_appoint 开）", self.call("POST", "/jury/apply", user="exampass", body={"statement": "联调自测"}))
        if appointed is not None:
            self.verify("直接成为见习委员，通过考试的风纪值补记上", appointed["status"] == "active" and appointed["tier"] == "trainee" and appointed["points"] == 20 and appointed["available_cases"] is not None and appointed["tier_progress"]["next_tier"] == "regular", f"status={appointed['status']} tier={appointed['tier']} points={appointed['points']}")
            kinds = [item["kind"] for item in self.get("/jury/me/points", "exampass")["items"]]
            self.verify("流水里是 exam_passed", kinds == ["exam_passed"], str(kinds))
        self.expect("PUT /admin/jury/settings {auto_appoint: false}", self.call("PUT", "/admin/jury/settings", user="admin", body={"auto_appoint": False}))
        applied = self.expect("eligible2（没通过考试，exam_required 关）申请", self.call("POST", "/jury/apply", user="eligible2", body={}))
        if applied is not None:
            self.verify("照旧进审批队列", applied["status"] == "applied" and applied["exam"]["passed"] is False, f"status={applied['status']}")

    # ------------------------------------------------------------ 二期：练习场

    def check_practice(self) -> None:
        self.section("练习场")
        scored = next((item["case_id"] for item in getattr(self, "questions", []) if item["pool"] == "scored"), None)
        first = self.expect("juror GET /jury/practice/next", self.call("GET", "/jury/practice/next", user="juror"))
        if first is not None:
            self.keys("PracticeNext", first)
            question = first["question"]
            self.keys("PracticeQuestion", question, optional={"case_origin"})
            self.check_content("练习题", {**question, "case_id": None})
            self.verify("主账号做了一部分：有累计成绩，这一轮没做完，没做过的题优先", first["stats"] == {"answered": 9, "correct": 7} and first["round"]["done"] == 9 < first["round"]["total"] and question["answered_before"] is False and first["daily_limit"] == 20 and first["left_today"] == 18 and bool(first["resets_at"]), f"round={first['round']} left_today={first['left_today']} resets_at={first['resets_at']}")
            item = next((entry for entry in getattr(self, "questions", []) if entry["case_id"] == question["question_id"]), None)
            self.verify("练习题来自练习场那一份（未核对的，或已核对里切出来的）", item is not None and item["pool"] == "practice" and item["verified"] == question["verified"], f"question_id={question['question_id']} pool={(item or {}).get('pool')} verified={question['verified']}")
            wrong = "none" if item and item["outcome"] == "violation" else "l1"
            answered = self.expect("juror POST /jury/practice/{question_id}/answer（故意答错）", self.call("POST", f"/jury/practice/{question['question_id']}/answer", user="juror", body={"vote": wrong}))
            if answered is not None:
                self.keys("PracticeAnswer", answered)
                self.keys("answer", answered["answer"])
                self.verify("作答：公布标准答案，累计和今日余量跟着变", answered["correct"] is False and answered["answer"]["outcome"] == (item or {}).get("outcome") and answered["stats"] == {"answered": 10, "correct": 7} and answered["left_today"] == 17 and answered["resets_at"] == first["resets_at"], f"stats={answered['stats']} left_today={answered['left_today']}")
            second = self.get("/jury/practice/next", "juror")
            self.verify("下一道是另一道没做过的题，round.done 加一", second["question"]["question_id"] != question["question_id"] and second["round"]["done"] == first["round"]["done"] + 1 and second["round"]["total"] == first["round"]["total"], f"round={second['round']}")
            self.expect("练习不给风纪值、不计入一致率", self.call("GET", "/jury/me/stats", user="juror"))
        self.expect("选项不合法（abstain）", self.call("POST", f"/jury/practice/{first['question']['question_id'] if first else 1}/answer", user="juror", body={"vote": "abstain"}), 422)
        self.expect("题目不存在", self.call("POST", "/jury/practice/999999/answer", user="juror", body={"vote": "none"}), 404, code="question_not_found")
        if scored is not None:
            self.expect("拿计分用的题来「练习」", self.call("POST", f"/jury/practice/{scored}/answer", user="juror", body={"vote": "none"}), 404, code="question_not_found")
        done = self.expect("exampass GET /jury/practice/next", self.call("GET", "/jury/practice/next", user="exampass"))
        if done is not None:
            self.verify("做完了一整轮：done == total，之后出的是重做的题", done["round"]["done"] == done["round"]["total"] >= 12 and done["question"]["answered_before"] is True and done["stats"]["answered"] > done["round"]["total"], f"round={done['round']} stats={done['stats']} left_today={done['left_today']}")
        fresh = self.expect("examfail GET /jury/practice/next（满足资格但不是委员）", self.call("GET", "/jury/practice/next", user="examfail"))
        if fresh is not None:
            self.verify("没练过：resets_at 是 null", fresh["resets_at"] is None and fresh["left_today"] == 20 and fresh["stats"] == {"answered": 0, "correct": 0} and fresh["round"]["done"] == 0, f"round={fresh['round']}")
            limit: dict[str, Any] | None = None
            last: dict[str, Any] = {}
            for _ in range(21):
                nxt = self.get("/jury/practice/next", "examfail")
                if nxt["question"] is None:
                    limit = {"exhausted": nxt}
                    break
                response = self.call("POST", f"/jury/practice/{nxt['question']['question_id']}/answer", user="examfail", body={"vote": "none"})
                if response.status_code != 200:
                    limit = {"response": response}
                    break
                last = response.json()
            exhausted = (limit or {}).get("exhausted")
            self.verify("连做 20 道：left_today 归零，再取题 question 是 null", last.get("left_today") == 0 and exhausted is not None and exhausted["left_today"] == 0 and bool(exhausted["resets_at"]), f"left_today={last.get('left_today')} resets_at={(exhausted or {}).get('resets_at')}")
            if exhausted is not None:
                over = self.expect("做满之后再答", self.call("POST", f"/jury/practice/{fresh['question']['question_id']}/answer", user="examfail", body={"vote": "none"}), 429, code="practice_daily_limit")
                if over is not None:
                    self.verify("429 practice_daily_limit 带 detail.resets_at", over["detail"].get("resets_at") == exhausted["resets_at"], str(over["detail"].get("resets_at")))
        self.expect("newbie（不满足条件）练习", self.call("GET", "/jury/practice/next", user="newbie"), 403, code="jury_not_eligible")

    # ------------------------------------------------------------ 后台

    def check_admin(self) -> None:
        self.section("后台（admin；这里的编号都是案件的真实编号）")
        self.expect("普通用户访问后台", self.call("GET", "/admin/jury/settings", user="juror"), 403)
        settings = self.expect("GET /admin/jury/settings", self.call("GET", "/admin/jury/settings", user="admin"))
        if settings is not None:
            switches = {"auto_appoint", "appeals", "machine_review", "public_group_content", "opinions_open_to_jurors", "exam_required"}
            self.verify("开关和阈值（演示库里 appeals / machine_review 是开的）", set(settings["switches"]) == switches and settings["switches"] == DEMO_SWITCHES and settings["thresholds"].get("large_group_members") == 200 and settings["thresholds"].get("practice_every") == 3, f"switches={settings['switches']}，阈值 {len(settings['thresholds'])} 项")
        new_fields = {"jury_outcome", "queued_at", "escalated_at", "published_at", "overturned_at", "overturn_note", "jury_block_reason"}
        cases = self.expect("GET /admin/cases（默认未结）", self.call("GET", "/admin/cases", user="admin"))
        if cases is not None:
            phase3_fields = {"case_origin", "panel_at", "panel_outcome", "reconsider_status"}
            self.verify("未结案件和各队列数量（多了 panel 队列），CaseRead 的三期字段", cases["total"] == sum(cases["open_counts"].values()) and cases["open_counts"].get("admin", 0) >= 4 and cases["open_counts"].get("jury", 0) >= 1 and cases["open_counts"].get("panel") == 4 and not ((new_fields | phase3_fields) - set(cases["items"][0])), f"open_counts={cases['open_counts']}")
        panel = self.expect("GET /admin/cases?queue=panel", self.call("GET", "/admin/cases", user="admin", query="queue=panel&limit=200"))
        if panel is not None:
            reasons = {item["jury_outcome"] for item in panel["items"]}
            self.verify("合议队列：无共识、上下文不足、超时转来的都有，都还没有合议结果", panel["total"] == 4 and reasons == {"no_consensus", "need_context", "timeout"} and all(item["queue"] == "panel" and item["panel_at"] and item["panel_outcome"] is None and item["case_origin"] == "report" for item in panel["items"]), f"jury_outcome={sorted(reasons)}")
        machine = [item for item in self.get("/admin/cases", "admin", "queue=jury&limit=200")["items"] if item["case_origin"] == "machine"]
        self.verify("委员队列里机器来源的案件：没有举报、没有首条举报，优先级排在最后", len(machine) == 3 and all(item["report_count"] == 0 and item["first_report_id"] is None and item["priority"] < 0 and item["machine_decision"] == "review" and item["primary_reason"].startswith("机器筛查 · ") for item in machine), str([(item["id"], item["primary_reason"], item["priority"]) for item in machine]))
        queue = self.expect("GET /admin/cases?queue=admin", self.call("GET", "/admin/cases", user="admin", query="queue=admin&limit=200"))
        if queue is not None:
            kinds = {item["target_type"] for item in queue["items"]}
            outcomes = {item["jury_outcome"] for item in queue["items"]}
            sensitive = [item for item in queue["items"] if item["jury_block_reason"] == "sensitive"]
            direct = [item for item in queue["items"] if item["target_type"] == "message" and not item["jury_eligible"]]
            self.verify("管理员队列：举报用户 / 群聊 / 私聊消息、色情类、无共识、上下文不足都有（超时的那件现在在合议队列里）", {"user", "conversation", "message"} <= kinds and {"no_consensus", "need_context"} <= outcomes and len(sensitive) >= 1 and len(direct) >= 1, f"类型={sorted(kinds)} jury_outcome={sorted(str(item) for item in outcomes)}")
            self.verify("色情低俗的案件在管理员队列、没进过委员队列", all(item["queue"] == "admin" and item["queued_at"] is None and item["primary_reason"] == "色情低俗" for item in sensitive), str([(item["id"], item["primary_reason"]) for item in sensitive]))
            waiting = next((item for item in queue["items"] if item["jury_outcome"] == "need_context"), None)
            if waiting is not None:
                detail = self.get(f"/admin/cases/{waiting['id']}", "admin")
                self.verify("上下文不足转出的案件：后台看得到每张票和评审编号", all(vote["vote"] == "need_context" and vote["review_id"] for vote in detail["votes"]) and len(detail["votes"]) == 2, str([(vote["juror"]["username"], vote["vote"], vote["review_id"]) for vote in detail["votes"]]))
        closed: list[dict[str, Any]] = []
        for status in ("resolved", "dismissed"):
            data = self.expect(f"GET /admin/cases?status={status}", self.call("GET", "/admin/cases", user="admin", query=f"status={status}&limit=200"))
            closed.extend(data["items"] if data else [])
        if closed:
            outcomes = {item["jury_outcome"] for item in closed}
            self.verify("已结案件：四种离开委员队列的方式、公示时间、改判记录", {"consensus", "no_consensus", "timeout", "manual"} <= outcomes and any(item["published_at"] for item in closed) and any(item["overturned_at"] and item["overturn_note"] for item in closed), f"{len(closed)} 件，jury_outcome={sorted(str(item) for item in outcomes)}，在公示的 {sum(1 for item in closed if item['published_at'])} 件")
        self.closed_cases = closed
        discussion = next((item for item in self.get("/admin/cases", "admin", "queue=jury&limit=200")["items"] if item["report_count"] == 2 and item["primary_reason"] == "辱骂骚扰"), None)
        mine = next((item for item in getattr(self, "votes", []) if item["case_id"] == getattr(self, "discussion_case", None)), None)
        for item in self.get("/admin/cases", "admin", "queue=jury&limit=200")["items"]:
            detail = self.get(f"/admin/cases/{item['id']}", "admin")
            if mine is not None and any(vote["review_id"] == mine["case_id"] for vote in detail["votes"]):
                discussion, votes, opinions = item, detail["votes"], detail["opinions"]
                self.ok("GET /admin/cases/{id}：按 votes[].review_id 找到主账号讨论的那个案件", f"真实编号 #{item['id']} ↔ 主账号的评审编号 {mine['case_id']}")
                self.verify("案件详情：votes[] 带代号、层级、评审编号和真实身份", len(votes) >= 4 and all(vote["juror"] and vote["tier"] and vote["review_id"] for vote in votes), str([(vote["alias"], vote["tier"], vote["juror"]["username"], vote["vote"], vote["review_id"]) for vote in votes]))
                self.verify("案件详情：votes[] 带 category（契约 1.10）", all("category" in vote for vote in votes), f"GET /admin/cases/{item['id']} 的 votes[0] 只有这些字段：{sorted(votes[0])}")
                self.verify("案件详情：opinions[] 带原文和举报记录", len(opinions) >= 8 and all(opinion["content"] and opinion["author"] for opinion in opinions) and any(opinion["reports"] for opinion in opinions) and {"visible", "hidden", "deleted"} <= {opinion["state"] for opinion in opinions}, f"{len(opinions)} 条")
                break
        else:
            self.fail("在后台找到主账号讨论的那个案件", "没有哪个案件的 votes[].review_id 等于主账号的评审编号")
        self.discussion_real = discussion["id"] if discussion else None
        members = self.expect("GET /admin/jury/members", self.call("GET", "/admin/jury/members", user="admin"))
        if members is not None:
            by_name = {item["user"]["username"]: item for item in members["items"]}
            phase2 = {"points", "exam_score", "exam_passed_at", "abstain_rate", "recuse_rate", "badge_visible", "days_served", "tier", "quality_recused"}
            good = not (phase2 - set(by_name["juror"])) and by_name["juror"]["tier"] == "regular" and by_name["senior1"]["tier"] == "senior" and by_name["suspended"]["status"] == "suspended" and by_name["expired"]["status"] == "expired"
            self.verify("委员名单：层级、状态（任期届满显示 expired）和二期新增的字段", good, "，".join(f"{name}={item['status']}/{item['tier']}" for name, item in by_name.items()))
            juror, hesitant, trainee2 = by_name["juror"], by_name["hesitant"], by_name["trainee2"]
            self.verify("委员名单：风纪值、在任天数、弃权率、回避率、考试成绩、是否公开", juror["points"] > 100 and 50 <= juror["days_served"] <= 53 and juror["exam_score"] is None and hesitant["abstain_rate"] > 0.3 and hesitant["exam_score"] == 8 and bool(hesitant["exam_passed_at"]) and trainee2["recuse_rate"] > 0 and trainee2["exam_score"] == 8 and by_name["senior1"]["badge_visible"] is True and by_name["suspended"]["days_served"] == 0, f"juror: points={juror['points']} days_served={juror['days_served']}；hesitant: abstain_rate={hesitant['abstain_rate']}；trainee2: recuse_rate={trainee2['recuse_rate']} exam_score={trainee2['exam_score']}")
            self.members = by_name
        for status, names in (("applied", {"applicant"}), ("expired", {"expired"}), ("suspended", {"suspended"})):
            data = self.expect(f"GET /admin/jury/members?status={status}", self.call("GET", "/admin/jury/members", user="admin", query=f"status={status}"))
            if data is not None:
                self.verify(f"按状态筛选 {status}", names <= {item["user"]["username"] for item in data["items"]} and all(item["status"] == status for item in data["items"]), str([item["user"]["username"] for item in data["items"]]))
        applicant = next((item for item in self.get("/admin/jury/members", "admin", "status=applied")["items"] if item["user"]["username"] == "applicant"), {})
        self.verify("审批队列里的 applicant 带着考试成绩", applicant.get("exam_score") == 9 and bool(applicant.get("exam_passed_at")) and bool(applicant.get("statement")), f"exam_score={applicant.get('exam_score')}")
        opinions = self.expect("GET /admin/jury/opinions", self.call("GET", "/admin/jury/opinions", user="admin", query="limit=200"))
        if opinions is not None:
            self.verify("观点列表：带真实作者", opinions["total"] >= 20 and all(item["author"] and item["content"] for item in opinions["items"]), f"total={opinions['total']}")
        for query, minimum in (("reported=true", 1), ("state=hidden", 1), ("state=deleted", 2), ("reported=false&state=&case_id=", 10), (f"case_id={self.discussion_real}" if self.discussion_real else "state=visible", 5)):
            data = self.expect(f"GET /admin/jury/opinions?{query}", self.call("GET", "/admin/jury/opinions", user="admin", query=query))
            if data is not None:
                self.verify(f"观点筛选 {query}", data["total"] >= minimum, f"total={data['total']}")
        self.hidden_opinions = self.get("/admin/jury/opinions", "admin", "state=hidden")["items"]
        violations = self.expect("GET /admin/violations?appeal_status=pending", self.call("GET", "/admin/violations", user="admin", query="appeal_status=pending"))
        if violations is not None:
            names = Counter(item["user"]["username"] for item in violations["items"])
            self.verify("待处理的申诉：appellant 一条，appealing 四条（两条还在资深复审里）", violations["total"] == 5 and dict(names) == {"appellant": 1, "appealing": 4}, f"total={violations['total']} {dict(names)}")

    def check_admin_bank(self) -> None:
        self.section("后台：题库与考试记录")
        page = self.expect("GET /admin/jury/questions", self.call("GET", "/admin/jury/questions", user="admin", query="limit=200"))
        if page is None:
            return
        items = page["items"]
        wanted = {"case_id", "summary", "outcome", "level", "category", "explanation", "verified", "source", "pool", "answered", "correct_rate"}
        self.verify("题库列表的字段", not (wanted - set(items[0])) and page["total"] == len(items), f"total={page['total']}")
        pools = {pool: sum(1 for item in items if item["pool"] == pool) for pool in ("scored", "practice")}
        sources = {source: sum(1 for item in items if item["source"] == source) for source in ("admin", "consensus", "legacy", "precedent")}
        # 仍是判例的案件恒属练习池，无论核对没核对、编号是多少
        precedents = {item["case_id"] for item in self.get("/admin/jury/precedents", "admin", "status=active&limit=200")["items"]}
        good = pools["scored"] >= 15 and pools["practice"] >= 12 and all(count > 0 for count in sources.values()) and all((item["pool"] == "scored") == (item["verified"] and item["case_id"] % 3 != 0 and item["case_id"] not in precedents) for item in items)
        marked = [item for item in items if item["case_id"] in precedents]
        self.verify("成了判例的题都在练习池里", len(marked) >= 5 and all(item["pool"] == "practice" for item in marked), str([(item["case_id"], item["pool"], item["source"], item["verified"]) for item in marked]))
        self.verify("计分池 ≥ 15 道、练习池 ≥ 12 道；四种来源都有；已核对的题里编号能被 3 整除的只给练习场", good, f"pool={pools} source={sources} 已核对 {sum(1 for item in items if item['verified'])} 道，带讲解 {sum(1 for item in items if item['explanation'])} 道")
        self.verify("认定不违规的题没有等级和类别；有被答过的题带正确率", all(item["level"] is None and item["category"] is None for item in items if item["outcome"] == "clean") and any(item["answered"] and item["correct_rate"] is not None for item in items) and any(item["answer_overridden"] for item in items), f"被答过的 {sum(1 for item in items if item['answered'])} 道")
        for query, predicate in [
            ("verified=true", lambda item: item["verified"]),
            ("verified=false", lambda item: not item["verified"]),
            ("source=consensus", lambda item: item["source"] == "consensus"),
            ("source=legacy", lambda item: item["source"] == "legacy"),
            ("source=admin", lambda item: item["source"] == "admin"),
            ("source=precedent", lambda item: item["source"] == "precedent"),
            ("q=垃圾广告", lambda item: "垃圾广告" in (item["primary_reason"] or "") or "垃圾广告" in (item["explanation"] or "")),
            ("verified=&source=&q=", lambda item: True),
        ]:
            data = self.expect(f"GET /admin/jury/questions?{query}", self.call("GET", "/admin/jury/questions", user="admin", query=f"{query}&limit=200"))
            if data is not None:
                self.verify(f"题库筛选 {query}", data["total"] == sum(1 for item in items if predicate(item)) > 0 and all(predicate(item) for item in data["items"]), f"total={data['total']}")
        target = next(item for item in items if item["source"] == "legacy" and not item["verified"] and not item["answer_overridden"])
        detail = self.expect("GET /admin/jury/questions/{case_id}", self.call("GET", f"/admin/jury/questions/{target['case_id']}", user="admin"))
        if detail is not None:
            self.check_content("题库里的题", detail)
            self.verify("题目详情：匿名内容 + 列表里的字段，可以入库", not (wanted - set(detail)) and detail["block_reason"] is None and (bool(detail["messages"]) or detail["post"] is not None) and "sender_id" not in json.dumps(detail), f"#{detail['case_id']} kind={detail['kind']}")
        updated = self.expect("PUT /admin/jury/questions/{case_id} 核对并写讲解", self.call("PUT", f"/admin/jury/questions/{target['case_id']}", user="admin", body={"verified": True, "explanation": "联调自测：讲解"}))
        if updated is not None:
            self.verify("核对之后：verified、pool 跟着变", updated["verified"] is True and updated["explanation"] == "联调自测：讲解" and updated["pool"] == ("practice" if target["case_id"] % 3 == 0 else "scored"), f"pool={updated['pool']}")
        overridden = self.expect("PUT … 改标准答案", self.call("PUT", f"/admin/jury/questions/{target['case_id']}", user="admin", body={"outcome": "clean" if target["outcome"] == "violation" else "violation", "level": None if target["outcome"] == "violation" else 1, "category": None if target["outcome"] == "violation" else "profanity"}))
        if overridden is not None:
            self.verify("改答案之后：answer_overridden", overridden["answer_overridden"] is True and overridden["outcome"] != target["outcome"], f"outcome={overridden['outcome']} level={overridden['level']} category={overridden['category']}")
        self.expect("PUT … 类别不能用于题库", self.call("PUT", f"/admin/jury/questions/{target['case_id']}", user="admin", body={"category": "sexual"}), 422, code="invalid_category")
        removed = self.expect("PUT … 移出题库", self.call("PUT", f"/admin/jury/questions/{target['case_id']}", user="admin", body={"in_bank": False}))
        if removed is not None:
            self.verify("移出之后：in_bank=false，pool 和 source 是 null", removed["in_bank"] is False and removed["pool"] is None and removed["source"] is None and removed["verified"] is False, f"in_bank={removed['in_bank']}")
        self.expect("不在题库里的题直接改", self.call("PUT", f"/admin/jury/questions/{target['case_id']}", user="admin", body={"explanation": "x"}), 409, code="not_in_bank")
        back = self.expect("PUT … 重新加入题库", self.call("PUT", f"/admin/jury/questions/{target['case_id']}", user="admin", body={"in_bank": True}))
        if back is not None:
            self.verify("重新加入：来源是 admin，未核对", back["in_bank"] is True and back["source"] == "admin" and back["verified"] is False and back["pool"] == "practice", f"source={back['source']}")
        post = next((item for item in getattr(self, "closed_cases", []) if item["target_type"] == "post" and item["status"] == "resolved" and item["published_at"] and not item["overturned_at"] and item["verdict_category"] not in {"privacy"}), None)
        if post is not None:
            blocked = self.get(f"/admin/jury/questions/{post['id']}", "admin")
            self.verify("内容会进公示栏的案件（公开动态认定违规）不能当题目", blocked["block_reason"] == "public_content" and blocked["in_bank"] is False, f"#{post['id']} block_reason={blocked['block_reason']}")
            self.expect("把它加入题库", self.call("PUT", f"/admin/jury/questions/{post['id']}", user="admin", body={"in_bank": True}), 422, code="case_not_eligible")
        self.expect("不存在的案件", self.call("GET", "/admin/jury/questions/999999", user="admin"), 404, code="case_not_found")
        exams = self.expect("GET /admin/jury/exams", self.call("GET", "/admin/jury/exams", user="admin"))
        if exams is not None:
            rows = exams["items"]
            times = [row["submitted_at"] for row in rows]
            users = {row["user"]["username"] for row in rows}
            self.verify("考试记录：用户、分数、是否通过、时间，按交卷时间倒序", exams["total"] == len(rows) >= 10 and times == sorted(times, reverse=True) and {"examout", "examfail", "exampass", "applicant", "trainee1", "trainee2", "hesitant"} <= users and all({"score", "total", "passed", "started_at", "timed_out"} <= set(row) for row in rows), f"{exams['total']} 场：{sorted(users)}")
            for query, wanted_value in (("passed=true", True), ("passed=false", False)):
                data = self.expect(f"GET /admin/jury/exams?{query}", self.call("GET", "/admin/jury/exams", user="admin", query=query))
                if data is not None:
                    self.verify(f"考试记录筛选 {query}", data["total"] == sum(1 for row in rows if row["passed"] is wanted_value) > 0 and all(row["passed"] is wanted_value for row in data["items"]), f"total={data['total']}")

    def check_admin_writes(self) -> None:
        self.section("后台的写操作（开关、观点管理、改判、任命、风纪值、申诉）")
        closed = getattr(self, "closed_cases", [])
        group_case = next((item for item in closed if item["published_at"] and item["target_type"] == "message" and item["status"] == "resolved" and not item["overturned_at"] and item["verdict_category"] not in {"privacy", "vulgar", "sexual"}), None)
        updated = self.expect("PUT /admin/jury/settings {public_group_content, opinions_open_to_jurors: true}", self.call("PUT", "/admin/jury/settings", user="admin", body={"public_group_content": True, "opinions_open_to_jurors": True}))
        if updated is not None and group_case is not None:
            body = self.get(f"/jury/public/cases/{group_case['id']}", "eligible2")
            self.verify("打开后群聊消息案件带上了被举报那一句", body.get("has_content") is True and bool((body.get("content") or {}).get("text")), f"#{group_case['id']} content={body.get('content')}")
            page = self.expect("开关打开：在任委员用公开编号读公示案件的观点区", self.call("GET", f"/jury/public/cases/{group_case['id']}/opinions", user="hesitant"))
            if page is not None:
                self.verify("只读的观点区：结构同委员侧，带每个人的票", set(page) == {"items", "total"} and all(item["vote"] for item in page["items"]), f"total={page['total']}")
            self.expect("开关打开：不是在任委员仍然不行", self.call("GET", f"/jury/public/cases/{group_case['id']}/opinions", user="eligible2"), 403, code="not_juror")
        restored = self.expect("PUT /admin/jury/settings 关回去", self.call("PUT", "/admin/jury/settings", user="admin", body={"public_group_content": False, "opinions_open_to_jurors": False}))
        if restored is not None:
            self.verify("开关关回去（回到演示库的初始状态）", restored["switches"] == DEMO_SWITCHES, str(restored["switches"]))
        hidden = getattr(self, "hidden_opinions", [])
        if hidden:
            self.verify("被折叠的观点：两条待处理的举报", hidden[0]["pending_reports"] >= 2 and all(report["reporter"] for report in hidden[0]["reports"]), str([(report["reporter"]["username"], report["reason"]) for report in hidden[0]["reports"]]))
            moderated = self.expect("POST /admin/jury/opinions/{id}/moderate dismiss_reports", self.call("POST", f"/admin/jury/opinions/{hidden[0]['id']}/moderate", user="admin", body={"action": "dismiss_reports", "note": "联调自测"}))
            if moderated is not None:
                self.verify("驳回举报后重新显示", moderated["state"] == "visible" and moderated["pending_reports"] == 0, f"state={moderated['state']}")
            removed = self.expect("POST /admin/jury/opinions/{id}/moderate delete", self.call("POST", f"/admin/jury/opinions/{hidden[0]['id']}/moderate", user="admin", body={"action": "delete", "note": "联调自测"}))
            if removed is not None:
                self.verify("管理员删除", removed["state"] == "deleted", f"state={removed['state']}")
        mine = {item["public_case_id"]: item for item in getattr(self, "votes", []) if item["public_case_id"] is not None}
        # 不挑已经是判例的案件：改判会连带把判例撤下（这一点在后面三期的检查里单独测）
        marked = {item["case_id"] for item in getattr(self, "public_items", []) if item.get("precedent_id")}
        target = next((item for item in closed if item["status"] == "resolved" and item["verdict_source"] == "jury" and not item["overturned_at"] and item["published_at"] and item["id"] not in marked and mine.get(item["id"], {}).get("agreed") is True), None)
        if target is None:
            self.fail("找一个可以改判的案件", "没有主账号投过、委员认定违规、已公示且没改判过的案件")
        else:
            review_id = mine[target["id"]]["case_id"]
            points = self.get("/jury/me", "juror")["points"]
            self.expect("改判缺少说明", self.call("POST", f"/admin/cases/{target['id']}/overturn", user="admin", body={"outcome": "dismiss"}), 422)
            overturned = self.expect("POST /admin/cases/{真实编号}/overturn", self.call("POST", f"/admin/cases/{target['id']}/overturn", user="admin", body={"outcome": "dismiss", "note": "联调自测：改判为不违规"}))
            if overturned is not None:
                self.verify("改判结果", overturned["status"] == "dismissed" and bool(overturned["overturned_at"]) and overturned["overturn_note"] == "联调自测：改判为不违规" and overturned["content_restored"] is not None, f"status={overturned['status']} content_restored={overturned['content_restored']}")
                detail = self.get(f"/jury/cases/{review_id}", "juror")
                after = self.get("/jury/me", "juror")["points"]
                self.verify("委员一侧跟着变：结论改了、my_vote.agreed 翻转、风纪值按 overturn_adjust 扣回", detail["verdict"]["outcome"] == "clean" and detail["verdict"]["overturned"] is True and detail["verdict"]["source"] == "jury" and detail["my_vote"]["agreed"] is False and after == points - self.config["points"]["overturn_adjust"], f"verdict={detail['verdict']['outcome']} agreed={detail['my_vote']['agreed']} points {points} → {after} tally={detail['tally']}")
                public = self.get(f"/jury/public/cases/{target['id']}", "eligible2")
                self.verify("公示栏一侧跟着变：标已改判、撤下内容", public.get("overturned") is True and public.get("content") is None and public.get("content_hidden_reason") == "overturned" and public.get("overturn_note") == "联调自测：改判为不违规", f"outcome={public.get('outcome')} source={public.get('source')}")
            self.expect("重复改判成同样的结论", self.call("POST", f"/admin/cases/{target['id']}/overturn", user="admin", body={"outcome": "dismiss", "note": "重复"}), 409, code="verdict_unchanged")

        founder = self.users["reporter4"]["id"]
        approved = self.expect("POST /admin/jury/members/{id}/review approve + tier（创始任命：没申请过的人直接任命为资深）", self.call("POST", f"/admin/jury/members/{founder}/review", user="admin", body={"action": "approve", "tier": "senior", "note": "联调自测"}))
        if approved is not None:
            me = self.get("/jury/me", "reporter4")
            self.verify("任命为指定层级", approved["status"] == "active" and approved["tier"] == "senior" and approved["days_served"] == 0 and me["status"] == "active" and me["tier"] == "senior" and me["tier_progress"] == {"next_tier": None, "requirements": []}, f"tier={approved['tier']} available_cases={me['available_cases']}")
        changed = self.expect("POST … set_tier", self.call("POST", f"/admin/jury/members/{founder}/review", user="admin", body={"action": "set_tier", "tier": "trainee"}))
        if changed is not None:
            self.verify("调整层级", changed["status"] == "active" and changed["tier"] == "trainee", f"tier={changed['tier']}")
        self.expect("set_tier 不带 tier", self.call("POST", f"/admin/jury/members/{founder}/review", user="admin", body={"action": "set_tier"}), 422)
        adjusted = self.expect("POST … adjust_points +12", self.call("POST", f"/admin/jury/members/{founder}/review", user="admin", body={"action": "adjust_points", "points": 12, "note": "联调自测：加分"}))
        if adjusted is not None:
            logs = self.get("/jury/me/points", "reporter4")
            self.verify("加分：总分和流水", adjusted["points"] == 12 and adjusted["status"] == "active" and logs["total"] == 12 and logs["items"][0]["kind"] == "admin_adjust" and logs["items"][0]["points"] == 12 and logs["items"][0]["case_id"] is None, f"points={adjusted['points']}")
        deducted = self.expect("POST … adjust_points -50（比现有的多）", self.call("POST", f"/admin/jury/members/{founder}/review", user="admin", body={"action": "adjust_points", "points": -50, "note": "联调自测：扣分"}))
        if deducted is not None:
            logs = self.get("/jury/me/points", "reporter4")
            self.verify("扣分：总分不低于 0，流水里记的是实际扣掉的", deducted["points"] == 0 and logs["items"][0]["points"] == -12, f"points={deducted['points']} 流水={logs['items'][0]['points']}")
        self.expect("adjust_points 不带 points", self.call("POST", f"/admin/jury/members/{founder}/review", user="admin", body={"action": "adjust_points"}), 422)
        self.expect("别的操作带 points", self.call("POST", f"/admin/jury/members/{founder}/review", user="admin", body={"action": "suspend", "points": 5}), 422)
        retired = self.expect("POST … retire", self.call("POST", f"/admin/jury/members/{founder}/review", user="admin", body={"action": "retire", "note": "联调自测"}))
        if retired is not None:
            self.verify("卸任之后 days_served 归零", retired["status"] == "retired" and retired["days_served"] == 0, f"status={retired['status']}")

        pending = [item for item in self.get("/admin/violations", "admin", "appeal_status=pending")["items"] if item["user"]["username"] == "appellant"]
        held = getattr(self, "held_back", [])
        if not pending or not held:
            self.fail("找待处理的申诉", f"申诉 {len(pending)} 条，被它挡在公示栏外的案件 {len(held)} 个")
            return
        accepted = self.expect("POST /admin/violations/{id}/appeal accept", self.call("POST", f"/admin/violations/{pending[0]['id']}/appeal", user="admin", body={"accept": True, "note": "联调自测：申诉成立"}))
        if accepted is not None:
            self.verify("申诉通过：处罚撤销", accepted["status"] == "revoked" and accepted["appeal_status"] == "accepted", f"status={accepted['status']}")
            detail = self.get(f"/jury/cases/{held[0]['case_id']}", "juror")
            public = self.call("GET", f"/jury/public/cases/{detail['public_case_id']}", user="eligible2") if detail.get("public_case_id") else None
            body = public.json() if public is not None else {}
            self.verify("申诉通过：案件自动改判并回到公示栏（委员详情里 public_case_id 有值了）", public is not None and public.status_code == 200 and body.get("overturned") is True and body.get("outcome") == "clean" and body.get("overturn_note") == "申诉通过，处罚已撤销" and detail["verdict"]["overturned"] is True, f"public_case_id={detail.get('public_case_id')} outcome={body.get('outcome')}")

    # ------------------------------------------------------------ 三期：初始状态（只读）

    def hall_posts(self, username: str, query: str = "limit=50") -> list[dict[str, Any]]:
        return self.get("/jury/hall/posts", username, query)["items"]

    def post_by_title(self, username: str, prefix: str) -> dict[str, Any]:
        return next((item for item in self.hall_posts(username) if item["title"].startswith(prefix)), {})

    def my_reports(self, username: str) -> list[dict[str, Any]]:
        data = self.get("/reports/me", username)
        return data if isinstance(data, list) else data.get("items", [])

    def check_phase3_seed(self) -> None:
        self.section("三期：资深委员、被处罚的账号、举报人看到的初始状态")
        for username in ("juror", "eligible2"):
            self.expect(f"{username} GET /jury/panel/next", self.call("GET", "/jury/panel/next", user=username), 403, code="not_senior")
            self.expect(f"{username} GET /jury/appeals/next", self.call("GET", "/jury/appeals/next", user=username), 403, code="not_senior")
        empty = self.expect("juror GET /jury/appeals/mine（不是资深委员）", self.call("GET", "/jury/appeals/mine", user="juror"))
        if empty is not None:
            self.verify("非资深委员：空列表", empty == {"items": [], "total": 0}, str(empty))
        counts = {name: self.get("/jury/me", name).get("senior") for name in ("senior1", "senior2", "senior3")}
        self.verify("/jury/me.senior：三位资深委员都有能取的合议和复审", counts == {"senior1": {"panel": 4, "appeals": 3}, "senior2": {"panel": 3, "appeals": 3}, "senior3": {"panel": 4, "appeals": 2}}, str(counts))

        data = self.expect("senior1 GET /jury/panel/next", self.call("GET", "/jury/panel/next", user="senior1"))
        view = (data or {}).get("case")
        if view is not None:
            self.keys("PanelCaseView", view)
            self.keys("PanelCaseView.origin", view["origin"])
            self.keys("PanelCaseView.panel", view["panel"])
            self.keys("tally", view["origin"]["tally"])
            self.check_content("合议案件", view)
            hours = (aware(view["panel"]["expires_at"]) - datetime.now(UTC)).total_seconds() / 3600
            good = self.target_text(view) == DEMO_TARGETS["panel_fresh"] and view["origin"]["reason"] == "no_consensus" and view["origin"]["tally"] == {"none": 2, "l1": 2, "l2": 1, "l3": 0, "abstain": 0, "need_context": 0}
            self.verify("合议里取到的第一件：无共识转来、还没人投，带原审的票数分布和期限", good and view["panel"]["seats"] == 3 and view["panel"]["votes_cast"] == 0 and 40 < hours < 43 and view["report_count"] == 3 and view["case_origin"] == "report", f"origin.reason={view['origin']['reason']} tally={view['origin']['tally']} panel={view['panel']}")
            opinions = view["origin"]["opinions"]
            for item in opinions[:1]:
                self.keys("Opinion", item)
            self.verify("origin.opinions：原审的观点，只读，每条都带作者投的票", len(opinions) == 3 and all(item["vote"] and item["content"] and item["alias"] for item in opinions) and {item["vote"] for item in opinions} == {"l1", "l2", "none"}, str([(item["alias"], item["vote"]) for item in opinions]))
            again = (self.get("/jury/panel/next", "senior1").get("case") or {})
            self.verify("没投之前再取，还是同一件、同一个评审编号", again.get("case_id") == view["case_id"], f"评审编号 {again.get('case_id')}")
            self.expect("合议案件分到手还没投：读详情", self.call("GET", f"/jury/cases/{view['case_id']}", user="senior1"), 403, code="not_voted")
            self.expect("合议的评审编号拿去普通投票接口", self.call("POST", f"/jury/cases/{view['case_id']}/vote", user="senior1", body={"vote": "none"}), 404)
            self.panel_first = view

        data = self.expect("senior1 GET /jury/appeals/next", self.call("GET", "/jury/appeals/next", user="senior1"))
        review = (data or {}).get("review")
        if review is not None:
            self.keys("AppealReviewView", review)
            self.keys("AppealReviewView.penalty", review["penalty"])
            self.keys("AppealReviewView.origin", review["origin"])
            self.keys("tally", review["origin"]["tally"])
            self.check_content("申诉复审的案件内容", {"kind": review["kind"], "messages": review["messages"], "post": review["post"]})
            hours = (aware(review["expires_at"]) - datetime.now(UTC)).total_seconds() / 3600
            good = review["type"] == "appeal" and review["kind"] == "message" and review["source"] == "jury" and review["category"] == "harassment" and review["category_label"] == "辱骂骚扰" and review["level"] == 2
            good = good and review["penalty"] == {"kind": "mute", "hours": 72} and self.target_text(review) == DEMO_TARGETS["appeal_jury"] and review["content"] is None and review["verdict"] is None and review["reconsider_text"] is None and review["requested_at"] is None
            self.verify("复审里取到的第一件：委员定案的处罚，带匿名上下文、处罚、申诉理由和期限", good and bool(review["appeal_text"]) and bool(review["appealed_at"]) and 44 < hours < 47 and review["seats"] == 3 and review["votes_cast"] == 0, f"review_id={review['review_id']} penalty={review['penalty']} appeal_text={review['appeal_text'][:16]}… expires_at={review['expires_at']}")
            self.verify("origin：原审票数分布和观点（每条带票）", review["origin"]["tally"] == {"none": 0, "l1": 1, "l2": 2, "l3": 0, "abstain": 0, "need_context": 0} and len(review["origin"]["opinions"]) == 2 and all(item["vote"] for item in review["origin"]["opinions"]), str(review["origin"]["tally"]))
            self.verify("复审材料里没有身份信息", "sender_id" not in json.dumps(review) and "username" not in json.dumps(review) and "appealing" not in json.dumps(review), "ok")
            self.verify("没投之前再取，还是同一件", (self.get("/jury/appeals/next", "senior1").get("review") or {}).get("review_id") == review["review_id"], f"review_id={review['review_id']}")
            self.appeal_first = review
        mine = self.expect("senior1 GET /jury/appeals/mine", self.call("GET", "/jury/appeals/mine", user="senior1"))
        if mine is not None:
            for item in mine["items"][:1]:
                self.keys("AppealMineItem", item)
            times = [item["voted_at"] for item in mine["items"]]
            seen = {(item["type"], item["my_vote"], item["outcome"]) for item in mine["items"]}
            self.verify("我参与过的复审：申诉和复议都有，带我的票和结果，按投票时间倒序", mine["total"] == len(mine["items"]) == 3 and seen == {("appeal", "uphold", "uphold"), ("appeal", "revoke", "revoke"), ("reconsider", "uphold", "uphold")} and times == sorted(times, reverse=True) and all(item["settled_at"] for item in mine["items"]), str(sorted(seen)))
            reconsider = next((item for item in mine["items"] if item["type"] == "reconsider"), {})
            appeal = next((item for item in mine["items"] if item["type"] == "appeal"), {})
            self.verify("复议的那条没有类别 / 等级 / 处罚；申诉的那条有", reconsider.get("category_label") is None and reconsider.get("level") is None and reconsider.get("penalty") is None and bool(appeal.get("category_label")) and appeal.get("level") == 2 and set(appeal.get("penalty") or {}) == {"kind", "hours"}, f"appeal: {appeal.get('category_label')} L{appeal.get('level')} {appeal.get('penalty')}")
            self.mine_before = mine["total"]

        standing = self.expect("appealing GET /moderation/me", self.call("GET", "/moderation/me", user="appealing"))
        if standing is not None:
            rows = standing["violations"]
            self.verify("ViolationRead 新增的三个字段", all({"appeal_stage", "appeal_result", "appeal_decided_by"} <= set(row) for row in rows), str(sorted(rows[0])))
            stages = sorted((row["source"], row["appeal_status"], row["appeal_stage"]) for row in rows)
            self.verify("appealing：四条申诉都还没结果，两条在资深复审、两条在管理员手里", stages == [("auto", "pending", "admin"), ("auto", "pending", "senior"), ("jury", "pending", "senior"), ("report", "pending", "admin")] and all(row["appeal_result"] is None and row["appeal_decided_by"] is None and not row["can_appeal"] for row in rows), str(stages))
            self.verify("appealing 正在禁言中（委员定案的那条带来的 72 小时）", bool(standing["muted_until"]) and aware(standing["muted_until"]) > datetime.now(UTC) + timedelta(hours=30), f"muted_until={standing['muted_until']}")
        standing = self.expect("punished GET /moderation/me", self.call("GET", "/moderation/me", user="punished"))
        if standing is not None:
            rows = standing["violations"]
            results = sorted((row["appeal_status"], str(row["appeal_result"]), str(row["appeal_decided_by"]), row["status"], row["penalty_kind"]) for row in rows)
            wanted = sorted([("rejected", "upheld", "senior", "active", "warning"), ("accepted", "reduced", "senior", "active", "warning"), ("accepted", "revoked", "senior", "revoked", "mute"), ("none", "None", "None", "active", "notice")])
            self.verify("punished：维持 / 减轻 / 撤销各一条（都是资深复审给的），另有一条还能申诉", results == wanted and sum(1 for row in rows if row["can_appeal"]) == 1 and standing["points"] == 7 and standing["muted_until"] is None, str(results))
            self.punished_open = next((row for row in rows if row["can_appeal"]), None)
        expected = {"reporter1": {"none", "pending"}, "reporter2": {"none", "pending"}, "reporter3": {"none", "upheld"}, "reporter4": {"none", "overturned"}}
        for username, statuses in expected.items():
            items = self.my_reports(username)
            got = {item.get("reconsider_status") for item in items}
            able = [item for item in items if item.get("can_reconsider")]
            self.verify(f"{username} GET /reports/me：can_reconsider / reconsider_status", all("can_reconsider" in item and "reconsider_status" in item for item in items) and got == statuses and (username != "reporter1" or len(able) >= 1), f"reconsider_status={sorted(got)}，可以申请复议的 {len(able)} 条")
            if username == "reporter1":
                self.reconsider_open = next((item for item in able if item["conversation_id"] != 1), able[0] if able else None)
            if username == "reporter4":
                item = next((entry for entry in items if entry["reconsider_status"] == "overturned"), {})
                self.verify("复议改判之后，这条举报变成「已处理」", item.get("status") == "reviewed" and bool(item.get("result_title")), f"status={item.get('status')} result_title={item.get('result_title')}")

    # ------------------------------------------------------------ 三期：资深合议

    def panel_next(self, username: str) -> dict[str, Any] | None:
        return (self.get("/jury/panel/next", username) or {}).get("case")

    def check_panel(self) -> None:
        self.section("三期：资深合议（senior1 / senior2 / senior3 轮流取件、投票）")
        first = getattr(self, "panel_first", None) or self.panel_next("senior1")
        if first is None:
            self.fail("合议取件", "senior1 取不到合议案件")
            return
        review_id = first["case_id"]
        for vote in ("abstain", "need_context"):
            self.expect(f"合议里投 {vote}", self.call("POST", f"/jury/panel/{review_id}/vote", user="senior1", body={"vote": vote}), 422, code="invalid_vote")
        self.expect("主账号（正式委员）投合议票", self.call("POST", f"/jury/panel/{review_id}/vote", user="juror", body={"vote": "none"}), 403, code="not_senior")
        self.expect("别的资深委员拿这个评审编号来投", self.call("POST", f"/jury/panel/{review_id}/vote", user="senior3", body={"vote": "none"}), 404, code="case_not_found")
        self.expect("不存在的评审编号", self.call("POST", "/jury/panel/999999/vote", user="senior1", body={"vote": "none"}), 404, code="case_not_found")
        own = next((item for item in self.all_votes("senior1") if item["vote_stage"] == "jury" and item["stage"] == "jury"), None)
        if own is not None:
            self.expect("原审的评审编号拿去合议投票接口", self.call("POST", f"/jury/panel/{own['case_id']}/vote", user="senior1", body={"vote": "none"}), 404, code="case_not_found")
        before = self.get("/jury/me", "senior1")
        result = self.expect("senior1 POST /jury/panel/{评审编号}/vote {l1}", self.call("POST", f"/jury/panel/{review_id}/vote", user="senior1", body={"vote": "l1", "category": "harassment", "note": "联调自测：合议意见"}))
        if result is not None:
            self.keys("vote result", result)
            detail = result["case"]
            self.check_detail("合议票的投后视图", detail, "l1")
            self.verify("第一张合议票：还没定案，案件在合议中，票数是原审的有效票", result["settled"] is False and result["case_id"] == review_id and detail["stage"] == "panel" and detail["votes_cast"] == 5 and detail["tally"] is None and detail["my_vote"]["category"] == "harassment", f"stage={detail['stage']} votes_cast={detail['votes_cast']}")
            after = self.get("/jury/me", "senior1")
            self.verify("合议票计入总票数和每日投票数", after["stats"]["votes_total"] == before["stats"]["votes_total"] + 1 and after["voted_today"] == before["voted_today"] + 1 and after["senior"]["panel"] == before["senior"]["panel"] - 1, f"votes_total={after['stats']['votes_total']} voted_today={after['voted_today']} senior={after['senior']}")
            self.expect("投完之后读详情", self.call("GET", f"/jury/cases/{review_id}", user="senior1"))
            opinions = self.expect("投完之后读观点区", self.call("GET", f"/jury/cases/{review_id}/opinions", user="senior1"))
            if opinions is not None:
                self.verify("观点区：投票时写的备注成了合议委员的第一条观点", any(item["mine"] and item["is_note"] and item["tier"] == "senior" for item in opinions["items"]), f"total={opinions['total']}")
            row = next((item for item in self.all_votes("senior1") if item["case_id"] == review_id), {})
            self.verify("/jury/votes：这一条 vote_stage=panel，stage 是案件现在的阶段", row.get("vote_stage") == "panel" and row.get("stage") == "panel" and row.get("vote") == "l1", f"vote_stage={row.get('vote_stage')} stage={row.get('stage')}")
            self.expect("重复投合议票", self.call("POST", f"/jury/panel/{review_id}/vote", user="senior1", body={"vote": "none"}), 409, code="already_voted")
        self.panel_fresh_review = review_id

        # 第二件：已有一张违规票，再来一张就定案
        second = self.panel_next("senior1")
        if second is None or self.target_text(second) != DEMO_TARGETS["panel_one_vote"]:
            self.fail("合议第二件", f"应该是已有一票的那件，实际是 {self.target_text(second or {'messages': []})[:30]}")
        else:
            self.verify("第二件：公开动态，已有一张合议票；origin.opinions 里只有原审的发言（先投的合议委员的立场不透露）", second["kind"] == "post" and second["panel"]["votes_cast"] == 1 and len(second["origin"]["opinions"]) == 2, f"votes_cast={second['panel']['votes_cast']} opinions={len(second['origin']['opinions'])}")
            points = self.get("/jury/me", "senior1")["points"]
            result = self.expect("senior1 投第二张违规票 {l2}", self.call("POST", f"/jury/panel/{second['case_id']}/vote", user="senior1", body={"vote": "l2"}))
            if result is not None:
                detail = result["case"]
                self.check_detail("合议定案的投后视图", detail, "l2")
                good = result["settled"] is True and detail["stage"] == "closed" and detail["verdict"]["source"] == "panel" and detail["verdict"]["outcome"] == "violation" and detail["verdict"]["level"] == 1 and detail["verdict"]["category"] == "advertising"
                self.verify("两张违规票：当场定案，verdict.source=panel，等级取较低的那个，tally.panel 有值", good and detail["tally"].get("panel") == {"none": 0, "l1": 1, "l2": 1, "l3": 0} and detail["my_vote"]["agreed"] is True, f"verdict={detail['verdict']['category_label']}/L{detail['verdict']['level']} tally={detail['tally']}")
                logs = self.get("/jury/me/points", "senior1")["items"]
                gained = next((item for item in logs if item["kind"] == "panel_agreed"), {})
                self.verify("合议票与结论一致 +15", gained.get("points") == 15 and gained.get("case_id") == second["case_id"] and self.get("/jury/me", "senior1")["points"] >= points + 15, f"{gained}")
                other = next((item for item in self.all_votes("senior2") if item["vote_stage"] == "panel" and item["stage"] == "closed" and item["verdict_source"] == "panel" and item["agreed"] is True), None)
                self.verify("先投的那位合议委员也按结论结算了", other is not None, str(other)[:200])
                self.panel_settled_review = second["case_id"]

        # 第一件的后两票：senior2 的回避次数今天已经在原审里用完；他投不违规，1 比 1；senior3 的第三票定案
        second_seat = self.panel_next("senior2")
        if second_seat is None or self.target_text(second_seat) != DEMO_TARGETS["panel_fresh"]:
            self.fail("senior2 取合议案件", f"应该是优先级最高、席位还没满的那件，实际是 {self.target_text(second_seat or {'messages': []})[:30]}")
        else:
            self.verify("senior2 取到的是同一件：已有一张合议票", second_seat["panel"]["votes_cast"] == 1 and second_seat["case_id"] != review_id, f"votes_cast={second_seat['panel']['votes_cast']}")
            blocked = self.expect("senior2（今天的回避次数已在原审里用完）在合议里回避", self.call("POST", f"/jury/panel/{second_seat['case_id']}/vote", user="senior2", body={"vote": "recuse"}), 429, code="recuse_limit")
            if blocked is not None:
                self.verify("合议里的回避和原审共用每天的上限：429 recuse_limit 带 detail.resets_at", bool(blocked["detail"].get("resets_at")), str(blocked["detail"].get("resets_at")))
            result = self.expect("senior2 投不违规 {none}", self.call("POST", f"/jury/panel/{second_seat['case_id']}/vote", user="senior2", body={"vote": "none", "note": "联调自测：我认为不违规"}))
            if result is not None:
                self.verify("1 比 1：还不定案", result["settled"] is False and result["case"]["stage"] == "panel", f"stage={result['case']['stage']}")
            third_seat = self.panel_next("senior3")
            if third_seat is None or self.target_text(third_seat) != DEMO_TARGETS["panel_fresh"]:
                self.fail("senior3 取合议案件", f"应该还是第一件，实际是 {self.target_text(third_seat or {'messages': []})[:30]}")
            else:
                self.verify("senior3 看到的 origin.opinions 仍然只有原审的发言（前两位合议委员写的备注不在里面）", third_seat["panel"]["votes_cast"] == 2 and len(third_seat["origin"]["opinions"]) == 3, f"votes_cast={third_seat['panel']['votes_cast']} opinions={len(third_seat['origin']['opinions'])}")
                result = self.expect("senior3 投违规 {l2}：第三票定案", self.call("POST", f"/jury/panel/{third_seat['case_id']}/vote", user="senior3", body={"vote": "l2"}))
                if result is not None:
                    detail = result["case"]
                    self.verify("2 比 1 定案：违规，等级取违规票里较低的那个", result["settled"] is True and detail["verdict"]["source"] == "panel" and detail["verdict"]["outcome"] == "violation" and detail["verdict"]["level"] == 1 and detail["tally"]["panel"] == {"none": 1, "l1": 1, "l2": 1, "l3": 0}, f"L{detail['verdict']['level']} tally.panel={detail['tally']['panel']}")
                    minority = next((item for item in self.get("/jury/me/points", "senior2")["items"] if item["kind"] == "panel_disagreed"), {})
                    self.verify("合议票与结论不一致 +5", minority.get("points") == 5 and minority.get("case_id") == second_seat["case_id"], str(minority))
                    opinions = self.get(f"/jury/cases/{third_seat['case_id']}/opinions", "senior3")["items"]
                    self.verify("定案之后的观点区：原审委员和合议委员的发言都在，每条带票", sum(1 for item in opinions if item["tier"] == "senior") == 2 and len(opinions) == 5 and all(item["vote"] for item in opinions), str([(item["alias"], item["tier"], item["vote"]) for item in opinions]))
                    mine = getattr(self, "panel_open_review", None)
                    if mine is not None:
                        detail = self.get(f"/jury/cases/{mine}", "juror")
                        self.verify("原审委员（主账号）回看：stage=closed，verdict.source=panel，tally 多一项 panel，原审的票按合议结论结算", detail["stage"] == "closed" and detail["verdict"]["source"] == "panel" and detail["tally"]["panel"] == {"none": 1, "l1": 1, "l2": 1, "l3": 0} and detail["tally"]["l1"] == 2 and detail["my_vote"]["agreed"] is True and detail["my_vote"]["points"] == 10, f"tally={detail['tally']} agreed={detail['my_vote']['agreed']} points={detail['my_vote']['points']}")

        # 回避：只有三位资深委员，有人回避就凑不齐合议庭了，案件转管理员
        unclear = self.panel_next("senior3")
        if unclear is None or self.target_text(unclear) != DEMO_TARGETS["panel_need_context"]:
            self.fail("senior3 取上下文不足转来的合议案件", f"取到的是 {self.target_text(unclear or {'messages': []})[:30]}")
        else:
            self.verify("因「上下文不足」进合议的案件：origin.reason / tally / 原审委员写的备注", unclear["origin"]["reason"] == "need_context" and unclear["origin"]["tally"]["need_context"] == 2 and len(unclear["origin"]["opinions"]) == 1 and unclear["origin"]["opinions"][0]["vote"] == "need_context", f"tally={unclear['origin']['tally']}")
            recused = self.expect("senior3 在合议里回避", self.call("POST", f"/jury/panel/{unclear['case_id']}/vote", user="senior3", body={"vote": "recuse", "note": "联调自测：合议回避"}))
            if recused is not None:
                self.verify("合议回避：不算票、不给分", recused["settled"] is False and recused["case"] is None and recused["reward"]["points"] == 0, json.dumps(recused["reward"]))
                self.expect("回避之后再投", self.call("POST", f"/jury/panel/{unclear['case_id']}/vote", user="senior3", body={"vote": "none"}), 409, code="already_recused")
                self.expect("回避之后读详情", self.call("GET", f"/jury/cases/{unclear['case_id']}", user="senior3"), 403, code="not_voted")
                row = next((item for item in self.all_votes("senior3") if item["case_id"] == unclear["case_id"]), {})
                self.verify("合议里回避的记录：stage 为 null，vote_stage=panel，点不进详情", row.get("stage") is None and row.get("vote_stage") == "panel" and row.get("can_view") is False and row.get("vote") == "recuse", str({key: row.get(key) for key in ("vote", "stage", "vote_stage", "can_view")}))
                moved = next((item for item in self.get("/admin/cases", "admin", "queue=admin&limit=200")["items"] if item["summary"] == DEMO_TARGETS["panel_need_context"]), {})
                self.verify("没有别的资深委员可以顶上：案件转管理员，panel_outcome=unavailable", moved.get("queue") == "admin" and moved.get("panel_outcome") == "unavailable" and moved.get("jury_outcome") == "need_context", f"queue={moved.get('queue')} panel_outcome={moved.get('panel_outcome')}")
        last = self.panel_next("senior1")
        if last is not None:
            self.verify("因超时进合议的案件：origin.reason=timeout", last["origin"]["reason"] == "timeout" and sum(last["origin"]["tally"].values()) == 1, f"tally={last['origin']['tally']}")
            counts = {name: self.get("/jury/me", name)["senior"]["panel"] for name in ("senior1", "senior2", "senior3")}
            self.verify("合议队列里只剩这一件：分到手没投的那位也算着它", counts == {"senior1": 1, "senior2": 1, "senior3": 1}, str(counts))

    # ------------------------------------------------------------ 三期：申诉复审与举报人复议

    def review_plan(self, review: dict[str, Any]) -> dict[str, Any]:
        text = self.target_text(review) if review["messages"] or review["post"] else (review["content"] or {}).get("text") or ""
        if review["type"] == "reconsider":
            if text == DEMO_TARGETS["reconsider_pending"]:
                return {"vote": "overturn", "level": 2, "category": "harassment", "note": "联调自测：改判"}
            return {"vote": "uphold", "note": "联调自测：维持原结论"}
        if review["source"] == "machine":
            return {"vote": "uphold"}
        if text == DEMO_TARGETS["appeal_jury"]:
            return {"vote": "reduce", "note": "联调自测：减轻"}
        return {"vote": "revoke", "note": "联调自测：撤销"}

    def check_appeals(self) -> None:
        self.section("三期：申诉进不进资深复审、重复举报、举报人申请复议")
        open_violation = getattr(self, "punished_open", None)
        if open_violation is not None:
            appealed = self.expect("punished POST /moderation/violations/{id}/appeal（委员定案的处罚，原审里没有资深委员）", self.call("POST", f"/moderation/violations/{open_violation['id']}/appeal", user="punished", body={"text": "联调自测：只是手滑多发了几遍，没有恶意"}))
            if appealed is not None:
                self.verify("申诉进了资深复审", appealed["appeal_status"] == "pending" and appealed["appeal_stage"] == "senior" and appealed["can_appeal"] is False, f"appeal_stage={appealed['appeal_stage']}")
        standing = self.get("/moderation/me", "offender")
        target = next((row for row in standing["violations"] if row["can_appeal"]), None)
        if target is not None:
            appealed = self.expect("offender POST …/appeal（原审里有资深委员，凑不齐三位）", self.call("POST", f"/moderation/violations/{target['id']}/appeal", user="offender", body={"text": "联调自测：我只是说话直了一点"}))
            if appealed is not None:
                self.verify("条件不满足：申诉直接到管理员", appealed["appeal_status"] == "pending" and appealed["appeal_stage"] == "admin", f"appeal_stage={appealed['appeal_stage']}")
            self.verify("每条处罚只能申诉一次", self.call("POST", f"/moderation/violations/{target['id']}/appeal", user="offender", body={"text": "再申诉一次试试看"}).status_code in {400, 409}, "4xx")
        report = getattr(self, "reconsider_open", None)
        if report is None:
            self.fail("找一条可以申请复议的举报", "reporter1 的举报里没有 can_reconsider=true 的")
            return
        duplicate = self.expect("newbie 再举报同一句话（30 天内已认定不违规）", self.call("POST", "/reports", user="newbie", body={"target_type": "message", "target_id": report["target_id"], "reason": "辱骂骚扰"}), 409, code="already_reviewed")
        if duplicate is not None:
            self.verify("409 already_reviewed 带 detail.case_id", duplicate["detail"].get("case_id") == report["case_id"], str(duplicate["detail"]))
        cleared = next((item for item in getattr(self, "closed_cases", []) if item["status"] == "dismissed" and item["verdict_source"] == "admin" and item["target_type"] == "message" and item["conversation_id"] == 1 and not item["overturned_at"] and aware(item["closed_at"]) > datetime.now(UTC) - timedelta(days=28)), None)
        if cleared is not None:
            self.expect("管理员认定不违规的内容同样不再受理", self.call("POST", "/reports", user="eligible2", body={"target_type": "message", "target_id": cleared["message_id"], "reason": "辱骂骚扰"}), 409, code="already_reviewed")
        path = f"/reports/{report['id']}/reconsider"
        self.expect("复议理由太短", self.call("POST", path, user="reporter1", body={"text": "不服"}), 422, code="text_required")
        self.expect("拿别人的举报申请复议", self.call("POST", path, user="reporter2", body={"text": "这不是我的举报，但我也想申请复议"}), 404, code="report_not_found")
        handled = next((item for item in self.my_reports("reporter1") if item["status"] == "reviewed"), None)
        if handled is not None:
            self.expect("已经认定违规的举报申请复议", self.call("POST", f"/reports/{handled['id']}/reconsider", user="reporter1", body={"text": "联调自测：这条已经处理过了，不能复议"}), 409, code="reconsider_unavailable")
        requested = self.expect("reporter1 POST /reports/{id}/reconsider", self.call("POST", path, user="reporter1", body={"text": "联调自测：他说话的语气明显是在嘲讽，希望再看一次"}))
        if requested is not None:
            self.verify("申请之后：返回 ReportRead，reconsider_status=pending，不能再申请", requested["id"] == report["id"] and requested["reconsider_status"] == "pending" and requested["can_reconsider"] is False, f"reconsider_status={requested['reconsider_status']}")
        self.expect("一个案件只能复议一次", self.call("POST", path, user="reporter1", body={"text": "联调自测：再申请一次试试看行不行"}), 409, code="reconsider_unavailable")

        self.section("三期：资深委员投复审票（申诉的三种结果、复议的两种结果）")
        first = getattr(self, "appeal_first", None)
        if first is not None:
            self.expect("申诉复审投 overturn（那是复议的选项）", self.call("POST", f"/jury/appeals/{first['review_id']}/vote", user="senior1", body={"vote": "overturn"}), 422, code="invalid_vote")
            self.expect("没分给我的复审", self.call("POST", f"/jury/appeals/{first['review_id']}/vote", user="senior3", body={"vote": "uphold"}), 404, code="review_not_found")
            self.expect("主账号（不是资深）投复审票", self.call("POST", f"/jury/appeals/{first['review_id']}/vote", user="juror", body={"vote": "uphold"}), 403, code="not_senior")
        seniors = ("senior1", "senior2", "senior3")
        settled: dict[int, str | None] = {}
        seen: dict[int, dict[str, Any]] = {}
        closed_tested = voted_again = reconsider_tested = False
        for round_index in range(8):
            views = {name: (self.get("/jury/appeals/next", name) or {}).get("review") for name in seniors}
            if not any(views.values()):
                break
            for name in seniors:
                review = views[name]
                if review is None:
                    continue
                review_id = review["review_id"]
                if review_id not in seen:
                    seen[review_id] = review
                    self.keys("AppealReviewView", review)
                    if review["type"] == "reconsider":
                        self.keys("AppealReviewView.verdict", review["verdict"])
                        good = review["verdict"]["outcome"] == "clean" and review["verdict"]["source"] == "jury" and bool(review["reconsider_text"]) and bool(review["requested_at"]) and review["appeal_text"] is None and review["appealed_at"] is None
                        self.verify(f"复议（review {review_id}）：带原结论和复议理由，没有类别 / 等级 / 处罚", good and review["category"] is None and review["level"] is None and review["penalty"] is None and review["content"] is None and review["origin"] is not None and bool(review["messages"]), f"verdict={review['verdict']} reconsider_text={review['reconsider_text'][:14]}…")
                    elif review["source"] == "machine":
                        self.keys("AppealReviewView.content", review["content"])
                        self.verify(f"机器处罚的申诉（review {review_id}）：没有案件快照，只有被处罚的那条内容；已有一票", review["content"]["text"] == DEMO_TARGETS["appeal_machine"] and review["messages"] == [] and review["post"] is None and review["origin"] is None and review["votes_cast"] == 1 and review["penalty"]["kind"] == "mute", f"content={review['content']} votes_cast={review['votes_cast']}")
                body = self.review_plan(review)
                label = f"第 {round_index + 1} 轮 {name} POST /jury/appeals/{review_id}/vote {json.dumps({key: body[key] for key in body if key != 'note'}, ensure_ascii=False)}"
                if review_id in settled:
                    self.expect(f"{label}（别人先投出了结果）", self.call("POST", f"/jury/appeals/{review_id}/vote", user=name, body=body), 409, code="review_closed")
                    closed_tested = True
                    continue
                if review["type"] == "reconsider" and not reconsider_tested:
                    reconsider_tested = True
                    self.expect("复议投 reduce（那是申诉的选项）", self.call("POST", f"/jury/appeals/{review_id}/vote", user=name, body={"vote": "reduce"}), 422, code="invalid_vote")
                    self.expect("复议改判时等级超出 1–3", self.call("POST", f"/jury/appeals/{review_id}/vote", user=name, body={"vote": "overturn", "level": 4}), 422)
                result = self.expect(label, self.call("POST", f"/jury/appeals/{review_id}/vote", user=name, body=body))
                if result is None:
                    continue
                self.keys("AppealVoteResult", result)
                kinds = [(item["kind"], item["points"]) for item in result["reward"]["items"]]
                if result["review_id"] != review_id or kinds != [("review_vote", 8)] or result["reward"]["points"] != 8 or (result["outcome"] is not None) != result["settled"]:
                    self.fail(label, f"响应不对：{json.dumps(result, ensure_ascii=False)}")
                if not voted_again:
                    self.expect("重复投复审票", self.call("POST", f"/jury/appeals/{review_id}/vote", user=name, body=body), 409, code="already_voted")
                    voted_again = True
                if result["settled"]:
                    settled[review_id] = result["outcome"]
        outcomes = sorted((seen[review_id]["type"], seen[review_id]["source"], str(outcome)) for review_id, outcome in settled.items())
        self.verify("五件复审都出了结果：申诉 减轻 / 维持 / 撤销，复议 改判 / 维持", outcomes == sorted([("appeal", "jury", "reduce"), ("appeal", "machine", "uphold"), ("appeal", "jury", "revoke"), ("reconsider", "jury", "overturn"), ("reconsider", "jury", "uphold")]), str(outcomes))
        self.verify("别人先投出结果之后再投：409 review_closed", closed_tested, "遇到过")
        counts = {name: self.get("/jury/me", name)["senior"]["appeals"] for name in seniors}
        self.verify("复审都投完了：/jury/me.senior.appeals 归零", set(counts.values()) == {0}, str(counts))
        mine = self.get("/jury/appeals/mine", "senior1")
        self.verify("/jury/appeals/mine 跟着增加", mine["total"] == getattr(self, "mine_before", 0) + 5 and all(item["outcome"] and item["settled_at"] for item in mine["items"]), f"total={mine['total']}")
        log = next((item for item in self.get("/jury/me/points", "senior1")["items"] if item["kind"] == "review_vote"), {})
        self.verify("复审票的明细：+8，case_id 为 null", log.get("points") == 8 and log.get("case_id") is None, str(log))

        standing = self.get("/moderation/me", "appealing")
        rows = {row["source"] + ":" + row["summary"][:6]: row for row in standing["violations"]}
        reduced = next((row for row in standing["violations"] if row["source"] == "jury"), {})
        upheld = next((row for row in standing["violations"] if row["summary"] == DEMO_TARGETS["appeal_machine"]), {})
        self.verify("减轻：记录和记分保留，禁言解除（appeal_status=accepted，appeal_result=reduced）", reduced.get("appeal_status") == "accepted" and reduced.get("appeal_result") == "reduced" and reduced.get("appeal_decided_by") == "senior" and reduced.get("appeal_stage") is None and reduced.get("status") == "active" and reduced.get("penalty_kind") == "warning" and standing["points"] == 10 and standing["muted_until"] is None, f"penalty_kind={reduced.get('penalty_kind')} points={standing['points']} muted_until={standing['muted_until']}（{len(rows)} 条处罚）")
        self.verify("维持：申诉驳回（appeal_status=rejected，appeal_result=upheld）", upheld.get("appeal_status") == "rejected" and upheld.get("appeal_result") == "upheld" and upheld.get("appeal_decided_by") == "senior" and upheld.get("status") == "active", str({key: upheld.get(key) for key in ("appeal_status", "appeal_result", "appeal_decided_by", "status")}))
        standing = self.get("/moderation/me", "punished")
        revoked = next((row for row in standing["violations"] if open_violation is not None and row["id"] == open_violation["id"]), {})
        self.verify("撤销：处罚和记分一起撤销", revoked.get("status") == "revoked" and revoked.get("appeal_result") == "revoked" and revoked.get("appeal_decided_by") == "senior" and standing["points"] == 6, f"status={revoked.get('status')} points={standing['points']}")
        self.check_notification("appealing", "appeal_result")
        self.check_notification("reporter2", "reconsider_result")
        mine_reports = {username: self.my_reports(username) for username in ("reporter1", "reporter2")}
        overturned = next((item for item in mine_reports["reporter2"] if item["reconsider_status"] == "overturned"), {})
        kept = next((item for item in mine_reports["reporter1"] if item["id"] == report["id"]), {})
        self.verify("复议改判：举报变成已处理；复议维持：仍是未采纳，不能再申请", overturned.get("status") == "reviewed" and kept.get("reconsider_status") == "upheld" and kept.get("status") == "rejected" and kept.get("can_reconsider") is False, f"reporter2: {overturned.get('status')}/{overturned.get('reconsider_status')}；reporter1: {kept.get('status')}/{kept.get('reconsider_status')}")
        self.appeal_outcomes = {"reduced": reduced.get("id"), "upheld": upheld.get("id"), "revoked": revoked.get("id"), "overturned_case": overturned.get("case_id"), "kept_case": kept.get("case_id")}

    # ------------------------------------------------------------ 三期：判例

    def check_precedents(self) -> None:
        self.section("三期：判例（普通用户 eligible2、正式委员 juror、资深委员 senior1 / senior2）")
        page = self.expect("eligible2 GET /jury/precedents", self.call("GET", "/jury/precedents", user="eligible2"))
        if page is None:
            return
        items = page["items"]
        for item in items[:1]:
            self.keys("Precedent", item)
        times = [item["created_at"] for item in items]
        categories = {item["category"] for item in items}
        self.verify("判例列表：8 条有效，资深委员和管理员标的都有，类别分散，按创建时间倒序", page["total"] == len(items) == 8 and {item["by"] for item in items} == {"senior", "admin"} and len(categories) >= 4 and None in categories and times == sorted(times, reverse=True) and not any(item["mine"] for item in items) and all(item["title"] and len(item["summary"]) > 60 for item in items), f"by={[item['by'] for item in items]} category={sorted(str(item) for item in categories)}")
        self.verify("认定不违规的判例没有类别和等级", all(item["category"] is None and item["level"] is None for item in items if item["outcome"] == "clean") and {item["outcome"] for item in items} == {"violation", "clean"}, "ok")
        filtered = self.expect("GET /jury/precedents?category=advertising", self.call("GET", "/jury/precedents", user="eligible2", query="category=advertising"))
        if filtered is not None:
            self.verify("按类别筛选", filtered["total"] == sum(1 for item in items if item["category"] == "advertising") > 0 and all(item["category"] == "advertising" for item in filtered["items"]), f"total={filtered['total']}")
        paged = self.get("/jury/precedents", "eligible2", "limit=3&offset=2&category=")
        self.verify("limit / offset（空的 category = 不筛选）", [item["id"] for item in paged["items"]] == [item["id"] for item in items[2:5]] and paged["total"] == 8, str([item["id"] for item in paged["items"]]))
        mine = [item for item in self.get("/jury/precedents", "senior1")["items"] if item["mine"]]
        self.verify("senior1 看列表：自己标的那几条 mine=true", len(mine) >= 1 and all(item["by"] == "senior" for item in mine), str([item["id"] for item in mine]))
        reasons: dict[str | None, dict[str, Any]] = {}
        for item in items:
            detail = self.call("GET", f"/jury/precedents/{item['id']}", user="eligible2")
            if detail.status_code != 200:
                self.fail(f"GET /jury/precedents/{item['id']}", f"{detail.status_code} {detail.text[:200]}")
                continue
            body = detail.json()
            self.scan_datetimes("GET /jury/precedents/{id}", body)
            if body["case"] is not None or (body["content"] is None) == (body["content_hidden_reason"] is None):
                self.fail(f"判例 {item['id']} 的详情（普通用户）", f"case={body['case']} content={body['content']} content_hidden_reason={body['content_hidden_reason']}")
            reasons.setdefault(body["content_hidden_reason"], body)
        self.ok(f"GET /jury/precedents/{{id}} × {len(items)}（普通用户）", f"content_hidden_reason 的取值：{sorted(str(key) for key in reasons)}；case 都是 null")
        if None in reasons:
            body = reasons[None]
            self.keys("PrecedentDetail", body)
            self.keys("PublicCaseDetail.content", body["content"])
            self.verify("公开动态的判例：全站能看到被处理的内容", body["kind"] == "post" and body["outcome"] == "violation" and bool(body["content"]["text"]) and str(body["content"]["created_at"]).endswith("Z"), f"#{body['id']} {body['content']['text'][:20]}…")
        self.verify("群聊消息 / 认定不违规的判例：普通用户看不到内容，要旨照常可读", {"group_message", "clean"} <= set(reasons) and all(body["summary"] for body in reasons.values()), str(sorted(str(key) for key in reasons)))
        group = reasons.get("group_message")
        if group is not None:
            detail = self.expect("juror（在任委员）GET /jury/precedents/{id}", self.call("GET", f"/jury/precedents/{group['id']}", user="juror"))
            if detail is not None:
                self.keys("PrecedentDetail", detail)
                self.keys("PrecedentDetail.case", detail["case"])
                self.check_content("判例的匿名上下文", {"kind": detail["kind"], **detail["case"]})
                self.verify("在任委员：case 是匿名化的完整上下文；全站可见的 content 仍然按公示栏的规则", len(detail["case"]["messages"]) >= 3 and detail["content"] is None and detail["content_hidden_reason"] == "group_message" and "sender_id" not in json.dumps(detail), f"{len(detail['case']['messages'])} 条消息")
            self.expect("suspended（不在任）读同一条", self.call("GET", f"/jury/precedents/{group['id']}", user="suspended"))
            self.verify("不在任的委员：case 是 null", self.get(f"/jury/precedents/{group['id']}", "suspended")["case"] is None, "null")
        self.expect("不存在的判例", self.call("GET", "/jury/precedents/999999", user="juror"), 404, code="precedent_not_found")
        linked = [item for item in getattr(self, "public_items", []) if item.get("precedent_id")]
        by_id = {item["id"]: item for item in items}
        self.verify("公示栏里对应的案件带 precedent_id，指回同一个案件", len(linked) >= 4 and all(by_id.get(item["precedent_id"], {}).get("case_id") == item["case_id"] for item in linked if item["precedent_id"] in by_id), f"{len(linked)} 个")

        votes = [item for item in self.all_votes("senior1") if item["stage"] == "closed" and not item["is_quality_check"] and item["can_view"]]
        precedent_cases = {item["case_id"] for item in items}
        candidate = marked = None
        for item in votes[:45]:
            if item["overturned"]:
                continue
            detail = self.get(f"/jury/cases/{item['case_id']}", "senior1")
            if detail["precedent"] is not None and detail["precedent"]["mine"] and marked is None:
                marked = detail
            elif detail["can_mark_precedent"] and detail["precedent"] is None and item["public_case_id"] is not None and candidate is None:
                candidate = item
            if marked is not None and candidate is not None:
                break
        if marked is not None:
            self.keys("JuryCaseDetail.precedent", marked["precedent"])
            own = next((item for item in mine if item["id"] == marked["precedent"]["id"]), {})
            self.verify("案件详情：已经是判例的带 precedent {id, title, mine}，不能再标", marked["precedent"] == {"id": own.get("id"), "title": own.get("title"), "mine": True} and marked["can_mark_precedent"] is False, str(marked["precedent"]))
        else:
            self.fail("找一个 senior1 自己标过判例的案件", "他最近的已定案件里没有 precedent.mine=true 的")
        if candidate is None:
            self.fail("找一个 senior1 可以标为判例的案件", "他的已定案件里没有 can_mark_precedent=true 的")
            return
        self.ok("案件详情：can_mark_precedent=true", f"senior1 的评审编号 {candidate['case_id']}（公开编号 {candidate['public_case_id']}）")
        juror_item = next((item for item in getattr(self, "votes", []) if item["public_case_id"] in precedent_cases), None)
        if juror_item is not None:
            detail = self.get(f"/jury/cases/{juror_item['case_id']}", "juror")
            self.verify("正式委员看是判例的案件：precedent.mine=false，can_mark_precedent=false", (detail["precedent"] or {}).get("mine") is False and detail["can_mark_precedent"] is False, str(detail["precedent"]))
        quality = next((item for item in self.all_votes("senior1") if item["is_quality_check"]), None)
        if quality is not None:
            detail = self.get(f"/jury/cases/{quality['case_id']}", "senior1")
            self.verify("质控题的详情：precedent 是 null，can_mark_precedent=false", detail["precedent"] is None and detail["can_mark_precedent"] is False, "ok")

        body = {"case_id": candidate["case_id"], "title": "联调自测：判例标题", "summary": "联调自测：裁判要旨。"}
        self.expect("正式委员标判例", self.call("POST", "/jury/precedents", user="juror", body={**body, "case_id": (juror_item or candidate)["case_id"]}), 403, code="not_senior")
        self.expect("普通用户标判例", self.call("POST", "/jury/precedents", user="eligible2", body=body), 403, code="not_juror")
        self.expect("拿公开编号来标（不是自己的评审编号）", self.call("POST", "/jury/precedents", user="senior1", body={**body, "case_id": 999999}), 404, code="case_not_found")
        pending = self.panel_next("senior1")
        if pending is not None:
            self.expect("分到手还没投的案件", self.call("POST", "/jury/precedents", user="senior1", body={**body, "case_id": pending["case_id"]}), 403, code="not_voted")
        unsettled = next((item for item in self.all_votes("senior1") if item["stage"] in {"jury", "admin"} and item["can_view"]), None)
        if unsettled is not None:
            self.expect("还没定案的案件", self.call("POST", "/jury/precedents", user="senior1", body={**body, "case_id": unsettled["case_id"]}), 422, code="case_not_eligible")
        overturned = next((item for item in votes if item["overturned"]), None)
        if overturned is not None:
            self.expect("被改判的案件", self.call("POST", "/jury/precedents", user="senior1", body={**body, "case_id": overturned["case_id"]}), 422, code="case_not_eligible")
        if quality is not None:
            self.expect("质控题", self.call("POST", "/jury/precedents", user="senior1", body={**body, "case_id": quality["case_id"]}), 422, code="case_not_eligible")
        self.expect("标题为空", self.call("POST", "/jury/precedents", user="senior1", body={**body, "title": "  "}), 422, code="invalid_precedent")
        self.expect("标题超过 60 字", self.call("POST", "/jury/precedents", user="senior1", body={**body, "title": "字" * 61}), 422, code="invalid_precedent")
        self.expect("要旨超过 1000 字", self.call("POST", "/jury/precedents", user="senior1", body={**body, "summary": "字" * 1001}), 422, code="invalid_precedent")
        created = self.expect("senior1 POST /jury/precedents", self.call("POST", "/jury/precedents", user="senior1", body=body))
        if created is None:
            return
        self.keys("Precedent", created)
        self.verify("标成功：返回的 case_id 是公开编号（提交的是评审编号），mine=true，by=senior", created["case_id"] == candidate["public_case_id"] != candidate["case_id"] and created["mine"] is True and created["by"] == "senior" and created["title"] == body["title"], f"id={created['id']} case_id={created['case_id']}")
        self.expect("同一个案件再标一次", self.call("POST", "/jury/precedents", user="senior1", body=body), 409, code="precedent_exists")
        self.expect("别的资深委员改它", self.call("PUT", f"/jury/precedents/{created['id']}", user="senior2", body={"title": "不是我的"}), 403, code="not_precedent_author")
        self.expect("别的资深委员撤它", self.call("DELETE", f"/jury/precedents/{created['id']}", user="senior2"), 403, code="not_precedent_author")
        updated = self.expect("senior1 PUT /jury/precedents/{id}（只传 title）", self.call("PUT", f"/jury/precedents/{created['id']}", user="senior1", body={"title": "联调自测：改过的标题"}))
        if updated is not None:
            self.verify("只改传进来的字段", updated["title"] == "联调自测：改过的标题" and updated["summary"] == body["summary"] and updated["id"] == created["id"], f"title={updated['title']}")
        self.expect("改成空的要旨", self.call("PUT", f"/jury/precedents/{created['id']}", user="senior1", body={"summary": " "}), 422, code="invalid_precedent")
        self.verify("案件详情和公示栏跟着变", (self.get(f"/jury/cases/{candidate['case_id']}", "senior1")["precedent"] or {}).get("id") == created["id"] and self.get(f"/jury/public/cases/{candidate['public_case_id']}", "eligible2").get("precedent_id") == created["id"], f"precedent_id={created['id']}")
        question = self.get(f"/admin/jury/questions/{candidate['public_case_id']}", "admin")
        self.verify("成了判例的案件在题库里恒属练习池（内容进公示栏的除外）", question.get("pool") == "practice" or question.get("block_reason") == "public_content", f"in_bank={question.get('in_bank')} pool={question.get('pool')} source={question.get('source')} block_reason={question.get('block_reason')}")
        removed = self.expect("senior1 DELETE /jury/precedents/{id}", self.call("DELETE", f"/jury/precedents/{created['id']}", user="senior1"))
        if removed is not None:
            self.verify("撤下：返回 {id, status}", removed == {"id": created["id"], "status": "withdrawn"}, str(removed))
        self.expect("撤下之后读详情", self.call("GET", f"/jury/precedents/{created['id']}", user="senior1"), 404, code="precedent_not_found")
        self.expect("撤下之后再改", self.call("PUT", f"/jury/precedents/{created['id']}", user="senior1", body={"title": "还能改吗"}), 404, code="precedent_not_found")
        again = self.expect("撤下之后重新标同一个案件", self.call("POST", "/jury/precedents", user="senior1", body=body))
        if again is not None:
            self.verify("一个案件只有一条判例：复用同一个 id", again["id"] == created["id"], f"id={again['id']}")
        self.precedent_live = {"id": created["id"], "case": candidate["public_case_id"]}

    # ------------------------------------------------------------ 三期：议事厅

    def check_hall(self) -> None:
        self.section("三期：议事厅 —— 读（普通用户 eligible2、主账号 juror）")
        page = self.expect("eligible2 GET /jury/hall/posts", self.call("GET", "/jury/hall/posts", user="eligible2", query="limit=50"))
        if page is None:
            return
        posts = page["items"]
        for item in posts[:1]:
            self.keys("HallPost", item)
        vote_sample = next((item for item in posts if item["vote"]), None)
        if vote_sample is not None:
            self.keys("HallPost.vote", vote_sample["vote"])
        seen = Counter((item["kind"], item["status"] if item["state"] == "visible" else item["state"]) for item in posts)
        wanted = {("announcement", "open"): 2, ("proposal", "open"): 2, ("proposal", "passed"): 1, ("proposal", "adopted"): 1, ("proposal", "declined"): 1, ("proposal", "rejected"): 1, ("discussion", "open"): 3, ("discussion", "closed"): 1, ("discussion", "hidden"): 1}
        self.verify("帖子列表：公告、六种状态的提案、讨论（含被关闭的、被折叠的）", dict(seen) == wanted and page["total"] == 13, str(dict(seen)))
        self.verify("置顶的在最前面；公告的 author_tier 是 null，其余是作者现在的层级", posts[0]["pinned"] and posts[0]["kind"] == "announcement" and not any(item["pinned"] for item in posts[1:]) and all(item["author_tier"] is None for item in posts if item["kind"] == "announcement") and {item["author_tier"] for item in posts if item["kind"] != "announcement"} == {"regular", "senior"}, f"作者 {sorted({item['author']['username'] for item in posts})}")
        self.verify("普通用户：都不能评论、不能表决", not any(item["can_comment"] or item["mine"] or item["can_edit"] or item["can_delete"] or (item["vote"] or {}).get("can_vote") for item in posts), "ok")
        long = next((item for item in posts if item["content_truncated"]), None)
        if long is not None:
            full = self.expect("GET /jury/hall/posts/{id}", self.call("GET", f"/jury/hall/posts/{long['id']}", user="eligible2"))
            if full is not None:
                self.keys("HallPost", full)
                self.verify("列表里的正文是前 200 字（content_truncated=true），详情里是全文", len(long["content"]) == 200 and full["content"].startswith(long["content"]) and len(full["content"]) > 200 and full["content_truncated"] is False, f"{len(long['content'])} → {len(full['content'])} 字")
        hidden = next((item for item in posts if item["state"] == "hidden"), {})
        self.verify("被折叠的帖子：content 是 null，标题是占位文字", hidden.get("content") is None and "折叠" in hidden.get("title", "") and hidden.get("can_comment") is False, f"title={hidden.get('title')}")
        for query, predicate in [
            ("kind=proposal", lambda item: item["kind"] == "proposal"), ("kind=announcement", lambda item: item["kind"] == "announcement"), ("status=passed", lambda item: item["status"] == "passed"),
            ("kind=discussion&status=closed", lambda item: item["kind"] == "discussion" and item["status"] == "closed"), ("kind=&status=&sort=", lambda item: True),
        ]:
            data = self.expect(f"GET /jury/hall/posts?{query}", self.call("GET", "/jury/hall/posts", user="eligible2", query=f"{query}&limit=50"))
            if data is not None:
                self.verify(f"筛选 {query}", data["total"] == sum(1 for item in posts if predicate(item)) > 0 and all(predicate(item) for item in data["items"]), f"total={data['total']}")
        newest = self.get("/jury/hall/posts", "eligible2", "sort=new&limit=50")["items"]
        times = [item["created_at"] for item in newest if not item["pinned"]]
        self.verify("sort=new：置顶之后按发帖时间倒序", newest[0]["pinned"] and times == sorted(times, reverse=True) and [item["id"] for item in newest] != [item["id"] for item in posts], "ok")
        self.expect("不存在的帖子", self.call("GET", "/jury/hall/posts/999999", user="eligible2"), 404, code="post_not_found")

        mine = {item["id"]: item for item in self.hall_posts("juror")}
        close = next((item for item in mine.values() if item["title"].startswith("建议把「刷屏」")), {})
        voted = next((item for item in mine.values() if item["title"].startswith("建议质控题答错")), {})
        self.verify("表决中、主账号没投过的提案：3 赞成 1 反对，法定人数 5 人 —— 再有一张赞成票就满足通过条件", close.get("status") == "open" and close["vote"]["yes"] == 3 and close["vote"]["no"] == 1 and close["vote"]["quorum"] == 5 and close["vote"]["my_vote"] is None and close["vote"]["can_vote"] is True and (close["vote"]["pass_num"], close["vote"]["pass_den"]) == (2, 3) and aware(close["vote"]["ends_at"]) > datetime.now(UTC), str(close.get("vote")))
        self.verify("表决中、主账号投过赞成的提案", voted["vote"]["my_vote"] == "yes" and voted["vote"]["yes"] == 2 and voted["vote"]["no"] == 2 and voted["vote"]["can_vote"] is True and voted["can_edit"] is False, str(voted.get("vote")))
        states = {item["status"]: item for item in mine.values() if item["kind"] == "proposal" and item["status"] != "open"}
        self.verify("出了结果的提案：不能再表决；采纳 / 不采纳的带管理员说明", all(not item["vote"]["can_vote"] and aware(item["vote"]["ends_at"]) < datetime.now(UTC) for item in states.values()) and bool(states["adopted"]["admin_note"]) and bool(states["declined"]["admin_note"]) and states["passed"]["admin_note"] is None and states["rejected"]["admin_note"] is None and states["rejected"]["vote"]["yes"] < states["rejected"]["vote"]["no"], f"adopted: {states['adopted']['admin_note'][:16]}…")
        own = next((item for item in mine.values() if item["mine"]), {})
        self.verify("主账号自己发的讨论帖：mine / can_edit / can_delete 都是 true", own.get("kind") == "discussion" and own["can_edit"] and own["can_delete"] and own["can_comment"] and own["comment_count"] == 4, f"#{own.get('id')} {own.get('title')}")
        closed = next((item for item in mine.values() if item["status"] == "closed"), {})
        self.verify("被管理员关闭的帖子：不能再评论，带关闭说明", closed.get("can_comment") is False and bool(closed.get("admin_note")), f"admin_note={str(closed.get('admin_note'))[:20]}…")
        discussion = next((item for item in mine.values() if item["title"].startswith("「开玩笑」和「辱骂」")), {})
        data = self.expect("juror GET /jury/hall/posts/{id}/comments", self.call("GET", f"/jury/hall/posts/{discussion.get('id')}/comments", user="juror"))
        if data is not None:
            comments = data["items"]
            for item in comments[:1]:
                self.keys("HallComment", item)
            by_id = {item["id"]: item for item in comments}
            roots = [item for item in comments if item["parent_id"] is None]
            replies = [item for item in comments if item["parent_id"] is not None]
            times = [item["created_at"] for item in comments]
            self.verify("评论：平铺、时间正序，回复的 parent_id 都指向根评论（只有一层）", data["total"] == len(comments) == 12 and times == sorted(times) and all(item["parent_id"] in by_id and by_id[item["parent_id"]]["parent_id"] is None for item in replies) and len(roots) == 6 and discussion["comment_count"] == 11, f"total={data['total']} 根评论 {len(roots)} 条，回复 {len(replies)} 条，帖子的 comment_count={discussion['comment_count']}")
            nested = [item for item in replies if item["reply_to"]]
            self.verify("回复别人的回复：压成一层，带 reply_to（被回复的人）；直接回复根评论的 reply_to 是 null", len(nested) == 3 and {item["reply_to"]["username"] for item in nested} == {"trainee1", "senior1", "hesitant"} and any(item["reply_to"] is None for item in replies), str([(item["author"]["username"], item["reply_to"]["username"]) for item in nested]))
            deleted = [item for item in comments if item["state"] == "deleted"]
            folded = [item for item in comments if item["state"] == "hidden"]
            self.verify("已删除的根评论下面还有回复：留一个占位；已删除的回复和没有回复的根评论不出现", len(deleted) == 1 and deleted[0]["content"] is None and deleted[0]["parent_id"] is None and any(item["parent_id"] == deleted[0]["id"] for item in replies), f"占位 {len(deleted)} 条")
            self.verify("被在任委员举报到折叠的评论：content 是 null", len(folded) == 1 and folded[0]["content"] is None and folded[0]["reported"] is False and all(item["content"] for item in comments if item["state"] == "visible"), f"折叠 {len(folded)} 条")
            self.verify("评论的作者层级：见习委员也在评论", {"trainee", "regular", "senior"} <= {item["author_tier"] for item in comments} and sum(1 for item in comments if item["mine"]) == 2, str(sorted({str(item['author_tier']) for item in comments})))
            theirs = {item["id"]: item for item in self.get(f"/jury/hall/posts/{discussion['id']}/comments", "senior1")["items"]}
            self.verify("举报过的人看：reported=true", bool(folded) and theirs[folded[0]["id"]]["reported"] is True, "ok")
            paged = self.get(f"/jury/hall/posts/{discussion['id']}/comments", "juror", "limit=4&offset=3")
            self.verify("评论的 limit / offset", [item["id"] for item in paged["items"]] == [item["id"] for item in comments[3:7]] and paged["total"] == 12, str([item["id"] for item in paged["items"]]))
            self.hall_comments = comments

        self.section("三期：议事厅 —— 发帖、表决、评论、举报")
        for username, code in (("trainee2", "tier_required"), ("eligible2", "not_juror"), ("mutedjuror", "juror_restricted")):
            self.expect(f"{username} 发帖", self.call("POST", "/jury/hall/posts", user=username, body={"kind": "discussion", "title": "联调自测", "content": "联调自测"}), 403, code=code)
        self.expect("委员发公告", self.call("POST", "/jury/hall/posts", user="juror", body={"kind": "announcement", "title": "联调自测", "content": "联调自测"}), 422, code="invalid_post")
        self.expect("标题超过 60 字", self.call("POST", "/jury/hall/posts", user="juror", body={"kind": "discussion", "title": "字" * 61, "content": "联调自测"}), 422, code="invalid_post")
        self.expect("正文为空", self.call("POST", "/jury/hall/posts", user="juror", body={"kind": "discussion", "title": "联调自测", "content": "  "}), 422, code="invalid_post")
        post = self.expect("juror POST /jury/hall/posts（讨论）", self.call("POST", "/jury/hall/posts", user="juror", body={"kind": "discussion", "title": "联调自测：讨论帖", "content": "联调自测：这是一个讨论帖。"}))
        if post is None:
            return
        self.keys("HallPost", post)
        self.verify("发帖：返回 HallPost", post["mine"] and post["can_edit"] and post["can_delete"] and post["vote"] is None and post["author_tier"] == "regular" and post["status"] == "open" and post["state"] == "visible" and post["comment_count"] == 0, f"id={post['id']}")
        edited = self.expect("PUT /jury/hall/posts/{id}（只传 title）", self.call("PUT", f"/jury/hall/posts/{post['id']}", user="juror", body={"title": "联调自测：改过的标题"}))
        if edited is not None:
            self.verify("只改传进来的字段", edited["title"] == "联调自测：改过的标题" and edited["content"] == post["content"], edited["title"])
        self.expect("别人改我的帖子", self.call("PUT", f"/jury/hall/posts/{post['id']}", user="senior1", body={"title": "不是我的"}), 403, code="not_post_author")
        self.expect("别人删我的帖子", self.call("DELETE", f"/jury/hall/posts/{post['id']}", user="senior1"), 403, code="not_post_author")
        proposal = self.expect("juror POST /jury/hall/posts（提案）", self.call("POST", "/jury/hall/posts", user="juror", body={"kind": "proposal", "title": "联调自测：提案", "content": "现在的规则是……建议改成……理由是……"}))
        if proposal is not None:
            days = (aware(proposal["vote"]["ends_at"]) - datetime.now(UTC)).total_seconds() / 86400
            self.verify("提案：表决 7 天，法定人数和通过线在发帖时固定", proposal["vote"]["yes"] == 0 and proposal["vote"]["no"] == 0 and proposal["vote"]["my_vote"] is None and proposal["vote"]["can_vote"] and 6.9 < days <= 7 and proposal["vote"]["quorum"] >= 5 and (proposal["vote"]["pass_num"], proposal["vote"]["pass_den"]) == (2, 3), str(proposal["vote"]))
            path = f"/jury/hall/posts/{proposal['id']}"
            self.expect("还没人表决：提案可以改", self.call("PUT", path, user="juror", body={"content": "现在的规则是……建议改成……理由是……（补充）"}))
            self.expect("表决的取值不对", self.call("POST", f"{path}/vote", user="juror", body={"vote": "maybe"}), 422, code="invalid_vote")
            self.expect("普通用户表决", self.call("POST", f"{path}/vote", user="eligible2", body={"vote": "yes"}), 403, code="not_juror")
            cast = self.expect("juror POST /jury/hall/posts/{id}/vote {yes}", self.call("POST", f"{path}/vote", user="juror", body={"vote": "yes"}))
            if cast is not None:
                self.verify("表决：返回 HallPost", cast["vote"]["yes"] == 1 and cast["vote"]["no"] == 0 and cast["vote"]["my_vote"] == "yes" and cast["can_edit"] is False, str(cast["vote"]))
            changed = self.expect("改票 {no}", self.call("POST", f"{path}/vote", user="juror", body={"vote": "no"}))
            if changed is not None:
                self.verify("结束前可以改票：计数跟着换", changed["vote"]["yes"] == 0 and changed["vote"]["no"] == 1 and changed["vote"]["my_vote"] == "no", str(changed["vote"]))
            trainee = self.expect("见习委员表决 {yes}", self.call("POST", f"{path}/vote", user="trainee2", body={"vote": "yes"}))
            if trainee is not None:
                self.verify("见习委员可以表决", trainee["vote"]["yes"] == 1 and trainee["vote"]["no"] == 1 and trainee["vote"]["my_vote"] == "yes", str(trainee["vote"]))
            self.expect("有人表决之后改提案", self.call("PUT", path, user="juror", body={"title": "还能改吗"}), 409, code="post_locked")
        self.expect("对讨论帖表决", self.call("POST", f"/jury/hall/posts/{post['id']}/vote", user="juror", body={"vote": "yes"}), 409, code="vote_closed")
        ended = next((item for item in mine.values() if item["status"] == "passed"), None)
        if ended is not None:
            self.expect("对已经截止的提案表决", self.call("POST", f"/jury/hall/posts/{ended['id']}/vote", user="juror", body={"vote": "yes"}), 409, code="vote_closed")
            self.expect("出了结果的提案：作者删不掉", self.call("DELETE", f"/jury/hall/posts/{ended['id']}", user="senior1"), 409, code="post_locked")
            self.expect("出了结果的提案：作者也不能改", self.call("PUT", f"/jury/hall/posts/{ended['id']}", user="senior1", body={"title": "还能改吗"}), 409, code="post_locked")
        if close:
            cast = self.expect("主账号给「只差一票」的提案投赞成", self.call("POST", f"/jury/hall/posts/{close['id']}/vote", user="juror", body={"vote": "yes"}))
            if cast is not None:
                self.verify("4 赞成 1 反对，达到法定人数：到期时就会是通过（现在仍是表决中）", cast["vote"]["yes"] == 4 and cast["vote"]["no"] == 1 and cast["vote"]["yes"] + cast["vote"]["no"] >= cast["vote"]["quorum"] and cast["vote"]["yes"] * 3 >= 5 * 2 and cast["status"] == "open", str(cast["vote"]))

        path = f"/jury/hall/posts/{post['id']}/comments"
        self.expect("普通用户评论", self.call("POST", path, user="eligible2", body={"content": "路过"}), 403, code="not_juror")
        self.expect("评论为空", self.call("POST", path, user="juror", body={"content": "  "}), 422, code="invalid_comment")
        self.expect("评论超过 1000 字", self.call("POST", path, user="juror", body={"content": "字" * 1001}), 422, code="invalid_comment")
        root = self.expect("见习委员评论 POST /jury/hall/posts/{id}/comments", self.call("POST", path, user="trainee2", body={"content": "联调自测：见习委员的评论"}))
        if root is None:
            return
        self.keys("HallComment", root)
        self.verify("评论：返回 HallComment", root["parent_id"] is None and root["author_tier"] == "trainee" and root["mine"] and root["state"] == "visible" and root["reply_to"] is None, f"id={root['id']}")
        reply = self.expect("回复这条评论", self.call("POST", path, user="juror", body={"content": "联调自测：回复", "parent_id": root["id"]}))
        nested = self.expect("回复那条回复", self.call("POST", path, user="trainee2", body={"content": "联调自测：回复的回复", "parent_id": (reply or root)["id"]}))
        if reply is not None and nested is not None:
            self.verify("回复只有一层：回复的回复挂到同一条根评论下，reply_to 是被回复的人", reply["parent_id"] == root["id"] and reply["reply_to"] is None and nested["parent_id"] == root["id"] and nested["reply_to"]["username"] == "juror", f"parent_id={nested['parent_id']} reply_to={nested['reply_to']['username']}")
        other = getattr(self, "hall_comments", [{}])[0].get("id")
        if other is not None:
            self.expect("parent_id 是别的帖子里的评论", self.call("POST", path, user="juror", body={"content": "串台了", "parent_id": other}), 404, code="comment_not_found")
        if closed:
            self.expect("在已关闭的帖子下评论", self.call("POST", f"/jury/hall/posts/{closed['id']}/comments", user="juror", body={"content": "还能评论吗"}), 409, code="post_closed")
        if hidden:
            self.expect("在被折叠的帖子下评论", self.call("POST", f"/jury/hall/posts/{hidden['id']}/comments", user="juror", body={"content": "还能评论吗"}), 409, code="post_closed")
        self.verify("帖子的 comment_count 跟着变", self.get(f"/jury/hall/posts/{post['id']}", "juror")["comment_count"] == 3, "3")
        self.expect("删别人的评论", self.call("DELETE", f"/jury/hall/comments/{root['id']}", user="juror"), 403, code="not_comment_author")

        payload = {"target_type": "comment", "target_id": root["id"], "reason": "与风纪无关"}
        self.expect("举报自己的评论", self.call("POST", "/jury/hall/reports", user="trainee2", body=payload), 400, code="cannot_report_own")
        self.expect("举报理由不在列表里", self.call("POST", "/jury/hall/reports", user="juror", body={**payload, "reason": "看他不爽"}), 422, code="invalid_reason")
        self.expect("选「其他」但说明不足 5 个字", self.call("POST", "/jury/hall/reports", user="juror", body={**payload, "reason": "其他", "details": "不好"}), 422, code="invalid_reason")
        self.expect("举报不存在的评论", self.call("POST", "/jury/hall/reports", user="juror", body={**payload, "target_id": 999999}), 404, code="comment_not_found")
        outsider = self.expect("普通用户举报这条评论", self.call("POST", "/jury/hall/reports", user="eligible2", body=payload))
        if outsider is not None:
            self.verify("举报：返回 {target_type, target_id, state}；不是委员的举报不计入折叠", outsider == {"target_type": "comment", "target_id": root["id"], "state": "visible"}, str(outsider))
        states = []
        for username in ("juror", "senior3", "hesitant"):
            result = self.call("POST", "/jury/hall/reports", user=username, body=payload)
            states.append(result.json().get("state") if result.status_code == 200 else f"{result.status_code} {result.text[:80]}")
        self.verify("三名在任委员举报：第三个举报之后自动折叠", states == ["visible", "visible", "hidden"], str(states))
        self.expect("重复举报", self.call("POST", "/jury/hall/reports", user="juror", body=payload), 409, code="already_reported")
        listing = {item["id"]: item for item in self.get(path, "juror")["items"]}
        self.verify("折叠之后：content 是 null，reported=true，回复还在", listing[root["id"]]["state"] == "hidden" and listing[root["id"]]["content"] is None and listing[root["id"]]["reported"] is True and (reply is None or reply["id"] in listing), "ok")
        announcement = next((item for item in posts if item["kind"] == "announcement" and not item["pinned"]), None)
        if announcement is not None:
            states = [self.call("POST", "/jury/hall/reports", user=username, body={"target_type": "post", "target_id": announcement["id"], "reason": "与风纪无关"}).json().get("state") for username in ("juror", "senior3", "hesitant")]
            self.verify("公告不会被自动折叠（举报照样记下来）", states == ["visible", "visible", "visible"], str(states))
        if reply is not None:
            removed = self.expect("DELETE /jury/hall/comments/{id}（自己的回复）", self.call("DELETE", f"/jury/hall/comments/{reply['id']}", user="juror"))
            if removed is not None:
                self.verify("删评论：返回 {id, state}，列表里不再出现", removed == {"id": reply["id"], "state": "deleted"} and reply["id"] not in {item["id"] for item in self.get(path, "juror")["items"]}, str(removed))
        self.hall_live = {"post": post["id"], "proposal": (proposal or {}).get("id"), "comment": root["id"], "nested": (nested or {}).get("id")}

        third = self.expect("juror 今天的第三帖", self.call("POST", "/jury/hall/posts", user="juror", body={"kind": "discussion", "title": "联调自测：第三帖", "content": "联调自测"}))
        limited = self.expect("juror 今天的第四帖", self.call("POST", "/jury/hall/posts", user="juror", body={"kind": "discussion", "title": "联调自测：第四帖", "content": "联调自测"}), 429, code="hall_rate_limited")
        if limited is not None:
            self.verify("429 hall_rate_limited 带 detail.resets_at（最近 24 小时滚动）", bool(limited["detail"].get("resets_at")) and 23 < (aware(limited["detail"]["resets_at"]) - datetime.now(UTC)).total_seconds() / 3600 <= 24, str(limited["detail"].get("resets_at")))
        if third is not None:
            removed = self.expect("DELETE /jury/hall/posts/{id}（自己的讨论帖）", self.call("DELETE", f"/jury/hall/posts/{third['id']}", user="juror"))
            if removed is not None:
                self.verify("删帖：返回 {id, state}", removed == {"id": third["id"], "state": "deleted"}, str(removed))
            self.expect("删掉之后再读", self.call("GET", f"/jury/hall/posts/{third['id']}", user="juror"), 404, code="post_not_found")
            self.expect("删了也占当天的额度", self.call("POST", "/jury/hall/posts", user="juror", body={"kind": "discussion", "title": "联调自测：删了再发", "content": "联调自测"}), 429, code="hall_rate_limited")

    # ------------------------------------------------------------ 三期：后台

    def check_admin_phase3(self) -> None:
        self.section("三期：后台 —— 合议队列、复审监控、申诉")
        queue = self.expect("GET /admin/cases?queue=admin（合议超时 / 凑不齐人转来的）", self.call("GET", "/admin/cases", user="admin", query="queue=admin&limit=200"))
        if queue is not None:
            outcomes = {item["panel_outcome"] for item in queue["items"]}
            self.verify("管理员队列里有合议超时转来的和凑不齐合议庭的", {"timeout", "unavailable"} <= outcomes, str(sorted(str(item) for item in outcomes)))
            stale = next((item for item in queue["items"] if item["panel_outcome"] == "timeout"), None)
            if stale is not None:
                detail = self.get(f"/admin/cases/{stale['id']}", "admin")
                stages = [(vote["juror"]["username"], vote["stage"], vote["vote"]) for vote in detail["votes"]]
                self.verify("合议超时的案件：后台看得到原审和合议的每一张票（votes[].stage）", sum(1 for _name, stage, _vote in stages if stage == "jury") == 5 and [name for name, stage, _vote in stages if stage == "panel"] == ["senior3"] and bool(detail["panel_at"]), str(stages))
        closed = [item for status in ("resolved", "dismissed") for item in self.get("/admin/cases", "admin", f"status={status}&limit=200")["items"]]
        by_panel = [item for item in closed if item["verdict_source"] == "panel" and item["panel_at"]]
        self.verify("合议定案的案件：verdict_source=panel，panel_outcome=consensus", len(by_panel) >= 3 and all(item["panel_outcome"] == "consensus" and item["panel_at"] for item in by_panel), f"{len(by_panel)} 件")
        machine = [item for item in closed if item["case_origin"] == "machine"]
        self.verify("机器来源的已结案件：一个委员定案，一个「未作处理」（verdict_source=auto）", {"jury", "auto"} <= {item["verdict_source"] for item in machine} and all(item["report_count"] == 0 and item["first_report_id"] is None and item["primary_reason"].startswith("机器筛查") for item in machine), str([(item["id"], item["verdict_source"], item["status"]) for item in machine]))
        left = self.get("/admin/cases", "admin", "queue=panel&limit=200")["items"]
        if left:
            target = left[0]
            resolved = self.expect("POST /admin/cases/{id}/resolve（管理员直接接手合议中的案件）", self.call("POST", f"/admin/cases/{target['id']}/resolve", user="admin", body={"outcome": "violation", "category": "harassment", "level": 1, "note": "联调自测：管理员接手合议中的案件"}))
            if resolved is not None:
                self.verify("接手之后：结案，panel_outcome=manual", resolved["status"] == "resolved" and resolved["verdict_source"] == "admin" and resolved["panel_outcome"] == "manual", f"status={resolved['status']} panel_outcome={resolved['panel_outcome']}")
                self.verify("被接手的合议案件不再分给资深委员", self.panel_next("senior1") is None and self.get("/jury/me", "senior1")["senior"]["panel"] == 0, str(self.get("/jury/me", "senior1")["senior"]))

        page = self.expect("GET /admin/jury/reviews", self.call("GET", "/admin/jury/reviews", user="admin", query="limit=200"))
        if page is None:
            return
        reviews = page["items"]
        wanted = {"id", "kind", "stage", "status", "outcome", "decided_by", "escalation", "violation_id", "case_id", "report_id", "requester", "text", "target_type", "source", "category", "category_label", "level", "penalty", "created_at", "expires_at", "escalated_at", "closed_at", "votes"}
        self.verify("复审监控：申诉理由 / 复议理由、申请人、每位复审委员的票（实名）", page["total"] == len(reviews) == 12 and not (wanted - set(reviews[0])) and all(item["requester"] and item["text"] for item in reviews) and any(vote["juror"]["username"].startswith("senior") and vote["vote"] and vote["tier"] == "senior" for item in reviews for vote in item["votes"]), f"total={page['total']}")
        for query, predicate in [("kind=appeal", lambda item: item["kind"] == "appeal"), ("kind=reconsider", lambda item: item["kind"] == "reconsider"), ("status=open", lambda item: item["status"] == "open"), ("status=closed", lambda item: item["status"] == "closed"), ("kind=appeal&status=open", lambda item: item["kind"] == "appeal" and item["status"] == "open"), ("kind=&status=", lambda item: True)]:
            data = self.expect(f"GET /admin/jury/reviews?{query}", self.call("GET", "/admin/jury/reviews", user="admin", query=f"{query}&limit=200"))
            if data is not None:
                self.verify(f"复审筛选 {query}", data["total"] == sum(1 for item in reviews if predicate(item)) > 0 and all(predicate(item) for item in data["items"]), f"total={data['total']}")
        waiting = [item for item in reviews if item["status"] == "open"]
        shape = sorted((item["kind"], item["stage"], str(item["escalation"])) for item in waiting)
        self.verify("还开着的只剩转给管理员的两件：超时的申诉复审、凑不齐人的复议", shape == [("appeal", "admin", "timeout"), ("reconsider", "admin", "unavailable")], str(shape))
        overturned = next((item for item in reviews if item["kind"] == "reconsider" and item["outcome"] == "overturn" and item["decided_by"] == "senior" and len(item["votes"]) >= 2 and any(vote["level"] == 2 for vote in item["votes"])), None)
        self.verify("复议改判的那件：票上带等级和类别", overturned is not None and all(vote["category"] for vote in overturned["votes"] if vote["vote"] == "overturn"), str([(vote["juror"]["username"], vote["vote"], vote["level"], vote["category"]) for vote in (overturned or {}).get("votes", [])]))
        appeal = next((item for item in waiting if item["kind"] == "appeal"), None)
        reconsider = next((item for item in waiting if item["kind"] == "reconsider"), None)
        if appeal is not None:
            refused = self.expect("POST /admin/jury/reviews/{id}/decide 用在申诉复审上", self.call("POST", f"/admin/jury/reviews/{appeal['id']}/decide", user="admin", body={"action": "uphold"}), 409, code="appeal_review")
            if refused is not None:
                self.verify("409 appeal_review 带 detail.violation_id", refused["detail"].get("violation_id") == appeal["violation_id"], str(refused["detail"]))
            decided = self.expect("POST /admin/violations/{id}/appeal 驳回（复审超时转来的申诉）", self.call("POST", f"/admin/violations/{appeal['violation_id']}/appeal", user="admin", body={"accept": False, "note": "联调自测：维持"}))
            if decided is not None:
                row = next((item for item in self.get("/moderation/me", "appealing")["violations"] if item["id"] == appeal["violation_id"]), {})
                self.verify("管理员驳回：appeal_result=upheld，appeal_decided_by=admin，复审随之关闭", decided["appeal_status"] == "rejected" and row.get("appeal_result") == "upheld" and row.get("appeal_decided_by") == "admin" and row.get("appeal_stage") is None, str({key: row.get(key) for key in ("appeal_status", "appeal_result", "appeal_decided_by")}))
                after = next((item for item in self.get("/admin/jury/reviews", "admin", "kind=appeal&limit=200")["items"] if item["id"] == appeal["id"]), {})
                self.verify("复审监控里这件变成 closed / uphold / admin", (after.get("status"), after.get("outcome"), after.get("decided_by")) == ("closed", "uphold", "admin"), str((after.get("status"), after.get("outcome"), after.get("decided_by"))))
        if reconsider is not None:
            path = f"/admin/jury/reviews/{reconsider['id']}/decide"
            self.verify("decide 的 action 取值不对", self.call("POST", path, user="admin", body={"action": "reduce"}).status_code == 422, "422")
            self.expect("decide 不存在的复审", self.call("POST", "/admin/jury/reviews/999999/decide", user="admin", body={"action": "uphold"}), 404, code="review_not_found")
            decided = self.expect("POST /admin/jury/reviews/{id}/decide {uphold}（转给管理员的复议）", self.call("POST", path, user="admin", body={"action": "uphold", "note": "联调自测：维持原结论"}))
            if decided is not None:
                self.verify("管理员维持：closed / uphold / admin", (decided["status"], decided["outcome"], decided["decided_by"]) == ("closed", "uphold", "admin") and bool(decided["closed_at"]), str((decided["status"], decided["outcome"], decided["decided_by"])))
                status = next((item["reconsider_status"] for item in self.my_reports("reporter1") if item["id"] == reconsider["report_id"]), None)
                self.verify("举报人一侧：reconsider_status 从 pending 变成 upheld", status == "upheld", f"reconsider_status={status}")
            self.expect("已经有结果的复议再 decide", self.call("POST", path, user="admin", body={"action": "overturn"}), 409, code="review_closed")
        outcomes = getattr(self, "appeal_outcomes", {})
        if outcomes.get("reduced"):
            restored = self.expect("POST /admin/violations/{id}/appeal 驳回资深复审已经「减轻」的申诉", self.call("POST", f"/admin/violations/{outcomes['reduced']}/appeal", user="admin", body={"accept": False, "note": "联调自测：推翻复审的结果"}))
            if restored is not None:
                standing = self.get("/moderation/me", "appealing")
                row = next((item for item in standing["violations"] if item["id"] == outcomes["reduced"]), {})
                self.verify("管理员的结论覆盖复审结果：原来的禁言恢复", row.get("appeal_result") == "upheld" and row.get("appeal_decided_by") == "admin" and row.get("appeal_status") == "rejected" and row.get("penalty_kind") == "mute" and bool(standing["muted_until"]), f"penalty_kind={row.get('penalty_kind')} muted_until={standing['muted_until']}")
        if outcomes.get("upheld"):
            accepted = self.expect("POST /admin/violations/{id}/appeal 通过资深复审已经「维持」的申诉", self.call("POST", f"/admin/violations/{outcomes['upheld']}/appeal", user="admin", body={"accept": True, "note": "联调自测：改为撤销"}))
            if accepted is not None:
                row = next((item for item in self.get("/moderation/me", "appealing")["violations"] if item["id"] == outcomes["upheld"]), {})
                self.verify("覆盖为撤销：处罚撤销，appeal_result=revoked，appeal_decided_by=admin", accepted["status"] == "revoked" and row.get("appeal_result") == "revoked" and row.get("appeal_decided_by") == "admin", str({key: row.get(key) for key in ("status", "appeal_result", "appeal_decided_by")}))
        if outcomes.get("revoked"):
            self.expect("驳回资深复审已经撤销的处罚", self.call("POST", f"/admin/violations/{outcomes['revoked']}/appeal", user="admin", body={"accept": False}), 409, code="appeal_already_revoked")
        if outcomes.get("overturned_case"):
            item = next((entry for entry in self.get("/admin/cases", "admin", "status=resolved&limit=200")["items"] if entry["id"] == outcomes["overturned_case"]), {})
            self.verify("复议改判的案件：CaseRead.reconsider_status=overturned，记了处罚", item.get("reconsider_status") == "overturned" and bool(item.get("overturned_at")) and item.get("violation_id") is not None and item.get("verdict_category") == "harassment" and item.get("verdict_level") == 2, str({key: item.get(key) for key in ("status", "reconsider_status", "verdict_category", "verdict_level", "violation_id")}))
            question = self.get(f"/admin/jury/questions/{outcomes['overturned_case']}", "admin")
            if question.get("in_bank") and question.get("pool") == "scored":
                self.note(
                    f"复议改判的案件（#{outcomes['overturned_case']}）在改判之前就已经被定时任务自动收进题库了（委员三票一致、定案满 72 小时；复议的窗口是 7 天）。改判之后 "
                    f"GET /admin/jury/questions/{outcomes['overturned_case']} 仍是 in_bank={question.get('in_bank')} verified={question.get('verified')} pool={question.get('pool')} source={question.get('source')}，"
                    f"标准答案跟着变成了 outcome={question.get('outcome')} level={question.get('level')}：它还在计分池里，来源写的是「委员一致通过」，而委员当时一致的结论恰好相反"
                )
        settings = self.get("/admin/jury/settings", "admin")["thresholds"]
        wanted_thresholds = {"panel_timeout_hours": 48, "appeal_timeout_hours": 72, "points_panel_agreed": 15, "points_panel_disagreed": 5, "points_review_vote": 8, "machine_daily_min": 5, "cleared_block_days": 30, "reconsider_window_days": 7, "reconsider_text_min": 10, "precedent_title_max": 60, "hall_posts_per_day": 3, "hall_quorum_min": 3, "hall_pass_num": 2, "hall_pass_den": 3, "hall_auto_hide_reports": 3, "quality_recuse_retry_days": 7, "quality_shift_min_hours": 2, "quality_shift_max_hours": 40}
        self.verify("后台设置里的三期阈值", {key: settings.get(key) for key in wanted_thresholds} == wanted_thresholds, str({key: settings.get(key) for key in wanted_thresholds if settings.get(key) != wanted_thresholds[key]}) or "ok")

        self.section("三期：后台 —— 判例管理")
        page = self.expect("GET /admin/jury/precedents", self.call("GET", "/admin/jury/precedents", user="admin", query="limit=200"))
        if page is not None:
            rows = page["items"]
            extra = {"status", "created_by_id", "created_by", "updated_at", "withdrawn_at", "withdrawn_by_id", "withdraw_note", "overturned", "in_bank", "bank_added"}
            withdrawn = [item for item in rows if item["status"] == "withdrawn"]
            self.verify("后台的判例列表：含已撤下的，带创建人、撤下说明、是否在题库", page["total"] == len(rows) == 10 and not ((CONTRACT["Precedent"] | extra) - set(rows[0])) and len(withdrawn) == 1 and bool(withdrawn[0]["withdraw_note"]) and withdrawn[0]["withdrawn_at"] and all(item["created_by"] for item in rows) and any(item["bank_added"] for item in rows), f"total={page['total']} 已撤下 {len(withdrawn)} 条，因判例进题库的 {sum(1 for item in rows if item['bank_added'])} 条")
            for query, status in (("status=active", "active"), ("status=withdrawn", "withdrawn")):
                data = self.expect(f"GET /admin/jury/precedents?{query}", self.call("GET", "/admin/jury/precedents", user="admin", query=f"{query}&limit=200"))
                if data is not None:
                    self.verify(f"判例筛选 {query}", data["total"] == sum(1 for item in rows if item["status"] == status) > 0 and all(item["status"] == status for item in data["items"]), f"total={data['total']}")
            live = getattr(self, "precedent_live", None)
            if live is not None:
                edited = self.expect("PUT /admin/jury/precedents/{id}（改资深委员标的）", self.call("PUT", f"/admin/jury/precedents/{live['id']}", user="admin", body={"summary": "联调自测：管理员改过的要旨。"}))
                if edited is not None:
                    self.verify("后台可以改任何人标的，创建人不变", edited["summary"] == "联调自测：管理员改过的要旨。" and edited["created_by"]["username"] == "senior1" and edited["by"] == "senior", f"created_by={edited['created_by']['username']}")
                removed = self.expect("DELETE /admin/jury/precedents/{id}（带说明）", self.call("DELETE", f"/admin/jury/precedents/{live['id']}", user="admin", body={"note": "联调自测：后台撤下"}))
                if removed is not None:
                    self.verify("后台撤下：status=withdrawn，记下谁撤的和说明", removed["status"] == "withdrawn" and removed["withdraw_note"] == "联调自测：后台撤下" and removed["withdrawn_by_id"] == self.users["admin"]["id"], f"status={removed['status']}")
                self.expect("重复撤下是幂等的", self.call("DELETE", f"/admin/jury/precedents/{live['id']}", user="admin"))
                self.expect("已撤下的不能改", self.call("PUT", f"/admin/jury/precedents/{live['id']}", user="admin", body={"title": "还能改吗"}), 404, code="precedent_not_found")
        body = {"title": "联调自测：管理员标的判例", "summary": "联调自测：裁判要旨。"}
        self.expect("后台标判例：案件不存在", self.call("POST", "/admin/jury/precedents", user="admin", body={**body, "case_id": 999999}), 404, code="case_not_found")
        open_case = self.get("/admin/cases", "admin", "queue=jury&limit=1")["items"]
        if open_case:
            self.expect("后台标判例：还没定案", self.call("POST", "/admin/jury/precedents", user="admin", body={**body, "case_id": open_case[0]["id"]}), 422, code="case_not_eligible")
        closed_now = [item for status in ("resolved", "dismissed") for item in self.get("/admin/cases", "admin", f"status={status}&limit=200")["items"]]
        privacy = next((item for item in closed_now if item["verdict_category"] == "privacy"), None)
        if privacy is not None:
            self.expect("后台标判例：隐私类", self.call("POST", "/admin/jury/precedents", user="admin", body={**body, "case_id": privacy["id"]}), 422, code="case_not_eligible")
        private = next((item for item in closed_now if not item["jury_eligible"]), None)
        if private is not None:
            self.expect("后台标判例：涉及私聊", self.call("POST", "/admin/jury/precedents", user="admin", body={**body, "case_id": private["id"]}), 422, code="case_not_eligible")
        taken = {item["case_id"] for item in (page or {}).get("items", [])}
        fresh = next((item for item in closed_now if item["verdict_source"] == "jury" and item["status"] == "resolved" and item["published_at"] and not item["overturned_at"] and item["id"] not in taken and item["target_type"] == "message" and item["verdict_category"] not in {"privacy"}), None)
        if fresh is not None:
            created = self.expect("POST /admin/jury/precedents（公开编号）", self.call("POST", "/admin/jury/precedents", user="admin", body={**body, "case_id": fresh["id"]}))
            if created is not None:
                self.verify("后台标成功：by=admin，带是否进了题库", created["by"] == "admin" and created["case_id"] == fresh["id"] and created["status"] == "active" and created["in_bank"] is True, f"id={created['id']} in_bank={created['in_bank']} bank_added={created['bank_added']}")
                self.expect("同一个案件再标", self.call("POST", "/admin/jury/precedents", user="admin", body={**body, "case_id": fresh["id"]}), 409, code="precedent_exists")
                self.expect("资深委员改管理员标的", self.call("PUT", f"/jury/precedents/{created['id']}", user="senior1", body={"title": "不是我的"}), 403, code="not_precedent_author")
                overturned = self.expect("改判这个案件", self.call("POST", f"/admin/cases/{fresh['id']}/overturn", user="admin", body={"outcome": "dismiss", "note": "联调自测：改判，判例应该自动撤下"}))
                if overturned is not None:
                    row = next((item for item in self.get("/admin/jury/precedents", "admin", "limit=200")["items"] if item["id"] == created["id"]), {})
                    self.verify("案件被改判：判例自动撤下（withdrawn_by_id 为空），对外读不到", row.get("status") == "withdrawn" and row.get("withdrawn_by_id") is None and row.get("overturned") is True and self.call("GET", f"/jury/precedents/{created['id']}", user="juror").status_code == 404, f"status={row.get('status')} withdraw_note={row.get('withdraw_note')}")

        self.section("三期：后台 —— 议事厅")
        page = self.expect("GET /admin/jury/hall/posts", self.call("GET", "/admin/jury/hall/posts", user="admin", query="limit=200"))
        if page is None:
            return
        rows = page["items"]
        wanted = {"id", "kind", "title", "content", "author_id", "author", "author_tier", "status", "state", "pinned", "vote", "settled_at", "admin_note", "decided_by_id", "decided_at", "comment_count", "pending_reports", "created_at", "updated_at", "last_activity_at", "deleted_at", "deleted_by_id", "moderation_note"}
        hidden = next((item for item in rows if item["state"] == "hidden"), {})
        deleted = [item for item in rows if item["state"] == "deleted"]
        self.verify("后台的帖子列表：含已删除的；被折叠的也是原文，带待处理的举报数", not (wanted - set(rows[0])) and len(deleted) >= 1 and bool(hidden.get("content")) and "折叠" not in hidden.get("title", "折叠") and hidden.get("pending_reports") == 3, f"total={page['total']} 已删除 {len(deleted)} 个；折叠的那个标题是「{hidden.get('title')}」")
        for query, predicate in [("kind=proposal", lambda item: item["kind"] == "proposal"), ("status=adopted", lambda item: item["status"] == "adopted"), ("state=deleted", lambda item: item["state"] == "deleted"), ("state=hidden", lambda item: item["state"] == "hidden"), ("kind=&status=&state=", lambda item: True)]:
            data = self.expect(f"GET /admin/jury/hall/posts?{query}", self.call("GET", "/admin/jury/hall/posts", user="admin", query=f"{query}&limit=200"))
            if data is not None:
                self.verify(f"帖子筛选 {query}", data["total"] == sum(1 for item in rows if predicate(item)) > 0 and all(predicate(item) for item in data["items"]), f"total={data['total']}")
        discussion = next((item for item in rows if item["title"].startswith("「开玩笑」和「辱骂」")), {})
        detail = self.expect("GET /admin/jury/hall/posts/{id}", self.call("GET", f"/admin/jury/hall/posts/{discussion.get('id')}", user="admin"))
        seeded: dict[str, dict[str, Any]] = {}
        if detail is not None:
            comments = detail["comments"]
            states = Counter(item["state"] for item in comments)
            self.verify("后台的帖子详情：全部评论的原文，含已删除和被折叠的", len(comments) == 15 and dict(states) == {"visible": 10, "deleted": 4, "hidden": 1} and all(item["content"] for item in comments) and any(item["reply_to_id"] for item in comments) and any(item["moderation_note"] for item in comments), str(dict(states)))
            seeded["folded"] = next(item for item in comments if item["state"] == "hidden")
            seeded["self_deleted"] = next(item for item in comments if item["state"] == "deleted" and item["deleted_by_id"] == item["author_id"])
            seeded["admin_deleted"] = next(item for item in comments if item["state"] == "deleted" and item["deleted_by_id"] != item["author_id"])
            seeded["visible"] = next(item for item in comments if item["state"] == "visible" and item["parent_id"] is None)

        reports = self.expect("GET /admin/jury/hall/reports", self.call("GET", "/admin/jury/hall/reports", user="admin", query="limit=200"))
        if reports is not None:
            items = reports["items"]
            wanted = {"id", "target_type", "target_id", "target", "reporter_id", "reporter", "reason", "details", "status", "created_at", "handled_by_id", "handled_at", "handle_note"}
            statuses = Counter(item["status"] for item in items)
            self.verify("举报列表：待处理的不为空，已处理和已驳回的也在；带被举报内容此刻的样子", not (wanted - set(items[0])) and statuses["pending"] >= 7 and statuses["handled"] >= 1 and statuses["dismissed"] >= 1 and all(item["reporter"] and item["target"] and item["target"]["content"] for item in items) and {"post", "comment"} == {item["target_type"] for item in items}, str(dict(statuses)))
            for status in ("pending", "handled", "dismissed"):
                data = self.expect(f"GET /admin/jury/hall/reports?status={status}", self.call("GET", "/admin/jury/hall/reports", user="admin", query=f"status={status}&limit=200"))
                if data is not None:
                    self.verify(f"举报筛选 status={status}", data["total"] == statuses[status] and all(item["status"] == status for item in data["items"]), f"total={data['total']}")
            outsider = next((item for item in items if item["status"] == "pending" and item["reporter"]["username"] == "eligible" and item["target"]["state"] == "visible"), None)
            self.verify("不是委员的人举报的那条：记下来了，目标没有被折叠", outsider is not None and outsider["target"]["pending_reports"] == 1, str((outsider or {}).get("reason")))
            if seeded.get("folded"):
                target = next((item for item in items if item["status"] == "pending" and item["target_type"] == "comment" and item["target_id"] == seeded["folded"]["id"]), None)
                if target is not None:
                    self.verify("举报处理的 action 取值不对", self.call("POST", f"/admin/jury/hall/reports/{target['id']}/handle", user="admin", body={"action": "ignore"}).status_code == 422, "422")
                    handled = self.expect("POST /admin/jury/hall/reports/{id}/handle {dismiss}（被折叠的评论）", self.call("POST", f"/admin/jury/hall/reports/{target['id']}/handle", user="admin", body={"action": "dismiss", "note": "联调自测：举报不成立"}))
                    if handled is not None:
                        left = [item for item in self.get("/admin/jury/hall/reports", "admin", "status=pending&limit=200")["items"] if item["target_type"] == "comment" and item["target_id"] == seeded["folded"]["id"]]
                        self.verify("驳回：评论恢复显示，同一个目标上其余待处理的举报一起了结", handled["status"] == "dismissed" and handled["target"]["state"] == "visible" and handled["handle_note"] == "联调自测：举报不成立" and left == [], f"state={handled['target']['state']}，剩余 {len(left)} 条")
                    self.expect("处理过的举报再处理", self.call("POST", f"/admin/jury/hall/reports/{target['id']}/handle", user="admin", body={"action": "delete"}), 409, code="report_handled")
            target = next((item for item in items if item["status"] == "pending" and item["target_type"] == "post" and item["target"]["state"] == "hidden"), None)
            if target is not None:
                handled = self.expect("POST …/handle {delete}（被折叠的帖子）", self.call("POST", f"/admin/jury/hall/reports/{target['id']}/handle", user="admin", body={"action": "delete", "note": "联调自测：删除"}))
                if handled is not None:
                    self.verify("删除：帖子删掉，举报记成已处理，对外读不到", handled["status"] == "handled" and handled["target"]["state"] == "deleted" and self.call("GET", f"/jury/hall/posts/{target['target_id']}", user="juror").status_code == 404, f"state={handled['target']['state']}")
            self.expect("不存在的举报", self.call("POST", "/admin/jury/hall/reports/999999/handle", user="admin", body={"action": "dismiss"}), 404, code="report_not_found")
        if seeded:
            path = "/admin/jury/hall/comments/{}/moderate"
            self.expect("恢复作者自己删掉的评论", self.call("POST", path.format(seeded["self_deleted"]["id"]), user="admin", body={"action": "restore"}), 409, code="comment_not_restorable")
            restored = self.expect("POST /admin/jury/hall/comments/{id}/moderate {restore}（管理员删掉的）", self.call("POST", path.format(seeded["admin_deleted"]["id"]), user="admin", body={"action": "restore", "note": "联调自测：恢复"}))
            if restored is not None:
                self.verify("恢复：state=visible", restored["state"] == "visible" and restored["deleted_at"] is None, f"state={restored['state']}")
            removed = self.expect("POST …/moderate {delete}（不经过举报，直接删）", self.call("POST", path.format(seeded["visible"]["id"]), user="admin", body={"action": "delete", "note": "联调自测：直接删除"}))
            if removed is not None:
                self.verify("删除：state=deleted，记下说明", removed["state"] == "deleted" and removed["moderation_note"] == "联调自测：直接删除" and removed["deleted_by_id"] == self.users["admin"]["id"], f"state={removed['state']}")
            self.expect("不存在的评论", self.call("POST", path.format(999999), user="admin", body={"action": "delete"}), 404, code="comment_not_found")

        announcement = self.expect("POST /admin/jury/hall/posts（发公告）", self.call("POST", "/admin/jury/hall/posts", user="admin", body={"title": "联调自测：公告", "content": "联调自测：这是一条公告。", "pinned": False}))
        if announcement is not None:
            self.verify("公告：kind=announcement，作者是管理员，没有层级", announcement["kind"] == "announcement" and announcement["author"]["username"] == "admin" and announcement["author_tier"] is None and announcement["pinned"] is False, f"id={announcement['id']}")
            path = f"/admin/jury/hall/posts/{announcement['id']}/decision"
            pinned = self.expect("POST …/decision {pin}", self.call("POST", path, user="admin", body={"action": "pin"}))
            if pinned is not None:
                top = self.hall_posts("eligible2")[:2]
                self.verify("置顶：排到前面", pinned["pinned"] is True and {item["id"] for item in top if item["pinned"]} >= {announcement["id"]}, str([item["id"] for item in top]))
            self.expect("POST …/decision {unpin}", self.call("POST", path, user="admin", body={"action": "unpin"}))
            self.expect("公告不能采纳", self.call("POST", path, user="admin", body={"action": "adopt"}), 409, code="invalid_post_status")
            removed = self.expect("POST …/decision {delete}", self.call("POST", path, user="admin", body={"action": "delete", "note": "联调自测"}))
            if removed is not None:
                self.verify("删除：state=deleted，说明只留在后台", removed["state"] == "deleted" and removed["moderation_note"] == "联调自测", f"state={removed['state']}")
            self.expect("已删除的帖子再置顶", self.call("POST", path, user="admin", body={"action": "pin"}), 404, code="post_not_found")
        passed = next((item for item in rows if item["status"] == "passed"), None)
        rejected = next((item for item in rows if item["status"] == "rejected"), None)
        if passed is not None:
            path = f"/admin/jury/hall/posts/{passed['id']}/decision"
            adopted = self.expect("POST …/decision {adopt}（通过了的提案）", self.call("POST", path, user="admin", body={"action": "adopt", "note": "联调自测：采纳"}))
            if adopted is not None:
                public = self.get(f"/jury/hall/posts/{passed['id']}", "eligible2")
                self.verify("采纳：status=adopted，说明对所有人展示", adopted["status"] == "adopted" and adopted["decided_by_id"] == self.users["admin"]["id"] and public["status"] == "adopted" and public["admin_note"] == "联调自测：采纳", f"status={adopted['status']}")
            declined = self.expect("POST …/decision {decline}（改成不采纳）", self.call("POST", path, user="admin", body={"action": "decline", "note": "联调自测：不采纳"}))
            if declined is not None:
                self.verify("可以在采纳和不采纳之间改", declined["status"] == "declined" and declined["admin_note"] == "联调自测：不采纳", f"status={declined['status']}")
            self.expect("出了结果的提案不能关闭", self.call("POST", path, user="admin", body={"action": "close"}), 409, code="invalid_post_status")
        if rejected is not None:
            self.expect("没通过的提案不能采纳", self.call("POST", f"/admin/jury/hall/posts/{rejected['id']}/decision", user="admin", body={"action": "adopt"}), 409, code="invalid_post_status")
        live = getattr(self, "hall_live", None)
        if live is not None:
            closed = self.expect("POST …/decision {close}（主账号刚发的讨论帖）", self.call("POST", f"/admin/jury/hall/posts/{live['post']}/decision", user="admin", body={"action": "close", "note": "联调自测：关闭"}))
            if closed is not None:
                public = self.get(f"/jury/hall/posts/{live['post']}", "juror")
                self.verify("关闭：status=closed，不能再评论，作者也不能再改", closed["status"] == "closed" and public["can_comment"] is False and public["can_edit"] is False and public["admin_note"] == "联调自测：关闭", f"status={closed['status']}")
                self.expect("关闭之后评论", self.call("POST", f"/jury/hall/posts/{live['post']}/comments", user="juror", body={"content": "还能评论吗"}), 409, code="post_closed")
            self.verify("decision 的 action 取值不对", self.call("POST", f"/admin/jury/hall/posts/{live['post']}/decision", user="admin", body={"action": "archive"}).status_code == 422, "422")
        self.expect("普通用户访问后台的议事厅接口", self.call("GET", "/admin/jury/hall/posts", user="juror"), 403)

    # ------------------------------------------------------------ 每日首票 / 连续 7 天

    def check_daily_rewards(self) -> None:
        self.section("每日首票和连续 7 天（dayfirst / streak6：今天还没投过票的在任委员）")
        today = datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).date()
        for username, wanted in (("dayfirst", {"daily_first": 2}), ("streak6", {"daily_first": 2, "streak_7": 10})):
            me = self.get("/jury/me", username)
            logs = self.get("/jury/me/points", username, "limit=50")["items"]
            earlier = [item for item in logs if item["kind"] in {"daily_first", "streak_7"} and aware(item["created_at"]).astimezone(timezone(timedelta(hours=8))).date() == today]
            self.verify(f"{username}：在任、有案件可评，今天还没有 daily_first / streak_7 的流水", me["status"] == "active" and (me["available_cases"] or 0) >= 1 and not earlier, f"available_cases={me['available_cases']} voted_today={me['voted_today']} points={me['points']}")
            view = (self.get("/jury/cases/next", username) or {}).get("case")
            if view is None:
                self.fail(f"{username} 取案", "没有可评的案件")
                continue
            result = self.expect(f"{username} 今天的第一张有效票", self.call("POST", f"/jury/cases/{view['case_id']}/vote", user=username, body={"vote": "none"}))
            if result is None:
                continue
            got = {item["kind"]: item["points"] for item in result["reward"]["items"] if item["kind"] in {"daily_first", "streak_7"}}
            self.verify(f"{username}：reward 里有 {' + '.join(wanted)}", got == wanted and result["reward"]["points"] >= sum(wanted.values()) - 5 and all(item["label"] for item in result["reward"]["items"]), json.dumps(result["reward"], ensure_ascii=False))
            second = (self.get("/jury/cases/next", username) or {}).get("case")
            if second is not None:
                again = self.call("POST", f"/jury/cases/{second['case_id']}/vote", user=username, body={"vote": "none"}).json()
                kinds = [item["kind"] for item in (again.get("reward") or {}).get("items", [])]
                self.verify(f"{username}：同一天的第二张票不再给", "daily_first" not in kinds and "streak_7" not in kinds, str(kinds))
        earned = {item["key"] for item in self.get("/jury/me/achievements", "streak6")["items"] if item["earned_at"]}
        self.verify("streak6 拿到「连续 7 天」的成就", "streak_7" in earned, str(sorted(earned)))

    # ------------------------------------------------------------ 实时事件

    def start_listener(self, username: str) -> None:
        """用另一位委员的身份连上 /ws，在后台收事件。没装 websockets 就跳过。"""
        self.events: list[dict[str, Any]] = []
        self.listener_error: str | None = None
        self.listener_stop = threading.Event()
        try:
            from websockets.sync.client import connect
        except ImportError:
            self.listener_error = "没有安装 websockets，跳过实时事件"
            return
        url = self.base.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?token={self.tokens[username]}"
        ready = threading.Event()

        def run() -> None:
            try:
                with connect(url, open_timeout=10) as socket:
                    while not self.listener_stop.is_set():
                        try:
                            raw = socket.recv(timeout=0.5)
                        except TimeoutError:
                            continue
                        try:
                            event = json.loads(raw)
                        except ValueError:
                            continue
                        self.events.append(event)
                        if event.get("type") == "connection.ready":
                            ready.set()
            except Exception as error:  # noqa: BLE001
                self.listener_error = f"{type(error).__name__}: {error}"
                ready.set()

        self.listener = threading.Thread(target=run, daemon=True)
        self.listener.start()
        ready.wait(timeout=10)

    def check_events(self, username: str) -> None:
        self.section(f"实时事件（{username} 的 /ws 通道）")
        if not hasattr(self, "listener_stop"):
            return
        time.sleep(1.0)
        self.listener_stop.set()
        if self.listener_error:
            self.fail("连接 /ws", self.listener_error)
            return
        kinds: dict[str, int] = {}
        for event in self.events:
            kinds[event.get("type")] = kinds.get(event.get("type"), 0) + 1
        own = {item["case_id"] for item in self.all_votes(username)}
        self.verify("连上 /ws?token=…，收到 connection.ready", "connection.ready" in kinds, f"事件信封是 {{type, data}}；本次收到 {kinds}")
        for kind in ("jury.opinion.created", "jury.opinion.changed", "jury.case.settled", "jury.points.changed"):
            samples = [event["data"] for event in self.events if event.get("type") == kind]
            self.verify(f"收到 {kind}，其中的 case_id 是 {username} 自己的评审编号", bool(samples) and all(item.get("case_id") in own for item in samples if item.get("case_id") is not None), json.dumps(samples[0], ensure_ascii=False) if samples else "没收到")
        changed = next((event["data"] for event in self.events if event.get("type") == "jury.points.changed"), None)
        if changed is not None:
            self.verify("jury.points.changed 的字段", set(changed) == {"delta", "total", "kind", "label", "case_id"} and bool(changed["label"]), json.dumps(changed, ensure_ascii=False))
        earned = next((event["data"] for event in self.events if event.get("type") == "jury.achievement.earned"), None)
        if earned is not None:
            self.verify("jury.achievement.earned 的字段", set(earned) == {"key", "label"} and earned["key"] in ACHIEVEMENTS, json.dumps(earned, ensure_ascii=False))
        hall = [event["data"] for event in self.events if event.get("type") == "jury.hall.changed"]
        self.verify("收到 jury.hall.changed：{post_id, kind, action}", bool(hall) and all(set(item) == {"post_id", "kind", "action"} for item in hall) and {"created", "updated", "closed"} <= {item["action"] for item in hall}, f"{len(hall)} 次，action={sorted({item['action'] for item in hall})}")
        if "jury.queue.changed" in kinds:
            self.ok("收到 jury.queue.changed", f"{kinds['jury.queue.changed']} 次（同一进程 30 秒内最多一次）")
        else:
            self.note("这次没有收到 jury.queue.changed（它 30 秒内最多发一次，没收到不一定是问题）")

    # ------------------------------------------------------------ 汇总

    def summary(self) -> int:
        print("\n" + "=" * 72)
        # 用户对象（UserPublic）里的 created_at 是老接口的格式，一直不带时区，不算在这里
        jury_paths = {path: found for path, found in self.naive_paths.items() if "/jury/" in found[0] and "/admin/" not in found[0] and not any(part in path for part in ("user.", "author.", "reply_to."))}
        if jury_paths:
            listing = "；".join(f"{path} = {value}（{label}）" for path, (label, value) in jury_paths.items())
            self.notes.append(f"风纪接口里这些时间是不带时区的字符串：{listing}")
        if self.notes:
            print("和契约对不上 / 客户端要留意的地方：")
            for index, text in enumerate(self.notes, 1):
                print(f"  {index}. {text}")
        print(f"\n结果：PASS {self.passed}，FAIL {len(self.failures)}，NOTE {len(self.notes)}")
        for text in self.failures:
            print(f"  FAIL  {text}")
        print("\n这次自测改动了数据。留给客户端之前请重建：python scripts/jury_demo.py --reset")
        return 1 if self.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="风纪系统 v2 联调实例的 HTTP 自测")
    parser.add_argument("--base", default=f"http://127.0.0.1:{DEFAULT_PORT}", help="服务地址（不含 /api/v1）")
    parser.add_argument("--rounds", type=int, default=26, help="评审台最多连续取几个案件（默认 26：要把举报案件取完，才轮得到机器来源的案件）")
    parser.add_argument("--verbose", action="store_true", help="打印每个响应的开头")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    checker = Checker(args.base, args.verbose)
    try:
        checker.check_guard()
    except httpx.HTTPError as error:
        print(f"连不上 {args.base}：{error}\n先运行 python scripts/jury_demo.py 把服务起起来。")
        return 2
    checker.check_logins()
    if "juror" not in checker.tokens or "admin" not in checker.tokens:
        return checker.summary()
    checker.check_errors_and_cors()
    checker.start_listener("senior1")
    checker.check_config_me_overview()
    checker.check_phase3_seed()
    checker.check_review(args.rounds)
    checker.check_ids()
    checker.check_opinions()
    checker.check_votes_and_stats()
    checker.check_public()
    checker.check_other_roles()
    checker.check_progress()
    checker.check_recuse_limit("senior2")
    checker.check_admin()
    checker.check_exam()
    checker.check_practice()
    checker.check_admin_bank()
    checker.check_admin_writes()
    checker.check_panel()
    checker.check_appeals()
    checker.check_precedents()
    checker.check_hall()
    checker.check_admin_phase3()
    checker.check_daily_rewards()
    checker.check_events("senior1")
    return checker.summary()


if __name__ == "__main__":
    raise SystemExit(main())
