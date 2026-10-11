"""Run directly: E:/kukechat-venv/Scripts/python.exe tests/bench_message_send_latency.py

Entirely synthetic, serial, in-memory SQLite and fake Redis; never imports the app
until after changing to an empty temporary directory and clearing environment.
No ASGI network server, sockets, production database, .env, or existing data.
Measures the real service + API endpoint callable, not HTTP transport/auth.
Use --label before/after with identical flags when comparing code revisions.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def percentile(values: list[float], percent: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percent / 100
    lower = int(position)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (position - lower)


def rss_bytes() -> int:
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except ImportError:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                           ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                           ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                           ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                           ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            get_current = ctypes.windll.kernel32.GetCurrentProcess
            get_current.restype = wintypes.HANDLE
            read_memory = ctypes.windll.psapi.GetProcessMemoryInfo
            read_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            if not read_memory(get_current(), ctypes.byref(counters), counters.cb):
                raise OSError("GetProcessMemoryInfo failed")
            return counters.WorkingSetSize
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak if sys.platform == "darwin" else peak * 1024


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ids = []

    def exists(self, key):
        self.ids.append(key)
        return self

    async def execute(self):
        self.redis.commands["EXISTS"] += len(self.ids)
        self.redis.round_trips += 1
        return [int(int(key.rsplit(":", 1)[-1]) in self.redis.online) for key in self.ids]


class FakeRedis:
    def __init__(self, online, publish_delay=0.0):
        self.online = set(online)
        self.publish_delay = publish_delay
        self.commands = defaultdict(int)
        self.round_trips = 0
        self.last_publish = None

    def pipeline(self):
        return FakePipeline(self)

    async def smembers(self, key):
        self.commands["SMEMBERS"] += 1
        self.round_trips += 1
        return {str(user_id) for user_id in self.online}

    async def publish(self, channel, payload):
        self.commands["PUBLISH"] += 1
        self.round_trips += 1
        self.last_publish = payload
        if self.publish_delay:
            await asyncio.sleep(self.publish_delay)
        return 1


async def run(args):
    # Deliberately no app import at module scope; the caller has already isolated cwd/env.
    from sqlalchemy import event, insert, text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.core.database import Base
    from app.models.user import User
    from app.models.conversation import Conversation, ConversationMember
    from app.models.enums import ConversationType, MemberRole, MessageType
    from app.models.message import Message
    from app.models.group_activity import GroupMemberDailyActivity, GroupMemberExpLog
    from app.models.bot import Bot, BotInstallation
    from app.services import messages as service
    from app.api.v1 import conversations as api
    from app.websocket.manager import ConnectionManager
    from app.schemas.message import MessageCreate, MessageRead

    # On Windows asyncio needs an internal socket during event-loop creation/teardown;
    # block outbound connects only while the benchmark coroutine actually runs.
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection
    def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("network prohibited in local benchmark")
    socket.socket.connect = forbidden_connect
    socket.create_connection = forbidden_connect
    logging.getLogger("app.services.ccw").setLevel(logging.ERROR)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tables = [User.__table__, Conversation.__table__, ConversationMember.__table__,
              Message.__table__, GroupMemberDailyActivity.__table__, GroupMemberExpLog.__table__,
              Bot.__table__, BotInstallation.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=tables)
        # Recreate production's covering index without running migrations or opening a db file.
        await connection.execute(text("CREATE INDEX ix_messages_conversation_id_id ON messages (conversation_id, id)"))
    samples = defaultdict(list)
    peak_rss = rss_bytes()
    total_commands = defaultdict(int)
    total_round_trips = 0

    async def wrapped(name, original, *pos, **kw):
        started = time.perf_counter_ns()
        try:
            return await original(*pos, **kw)
        finally:
            samples[name].append((time.perf_counter_ns() - started) / 1e6)

    # Instrument actual production functions rather than reimplementing business logic.
    def track(name, original):
        async def wrapper(*pos, **kw):
            return await wrapped(name, original, *pos, **kw)
        return wrapper

    original_flush, original_commit = AsyncSession.flush, AsyncSession.commit

    async def timed_flush(db, *pos, **kw):
        segment = "insert_flush" if any(isinstance(obj, Message) for obj in db.new) else "activity_flush"
        return await wrapped(segment, original_flush, db, *pos, **kw)
    original_rate = service._enforce_message_rate_limit
    original_membership = service.require_membership
    original_prune = service._prune_conversation_messages
    original_activity = service.group_activity_service.record_message_activity
    original_lookup = api.online_conversation_member_ids
    original_fanout = ConnectionManager.send_to_users
    original_api = api.send_message
    original_service = service.send_message
    original_enrich = service.enrich_metadata_with_previews

    async def fake_preview(oid, access_key=""):
        await asyncio.sleep(args.preview_delay)
        raise TimeoutError("synthetic CCW timeout; no socket")

    async def preview_timer(*pos, **kw):
        return await wrapped("preview_enrichment", original_enrich, *pos, **kw)

    with ExitStack() as patches:
        patches.enter_context(patch.object(AsyncSession, "flush", timed_flush))
        patches.enter_context(patch.object(AsyncSession, "commit", track("commit", original_commit)))
        patches.enter_context(patch.object(service, "_enforce_message_rate_limit", track("rate_limit", original_rate)))
        patches.enter_context(patch.object(service, "require_membership", track("require_membership", original_membership)))
        patches.enter_context(patch.object(service, "_prune_conversation_messages", track("prune", original_prune)))
        patches.enter_context(patch.object(service.group_activity_service, "record_message_activity", track("activity", original_activity)))
        patches.enter_context(patch.object(api, "online_conversation_member_ids", track("online_recipient_lookup", original_lookup)))
        patches.enter_context(patch.object(ConnectionManager, "send_to_users", track("redis_broadcast_and_local_enqueue", original_fanout)))
        patches.enter_context(patch.object(service, "send_message", track("service_total", original_service)))
        patches.enter_context(patch.object(service, "enrich_metadata_with_previews", preview_timer))
        if args.mode == "slow-preview":
            from app.services import ccw
            patches.enter_context(patch.object(ccw, "fetch_creation_preview", fake_preview))
        patches.enter_context(patch.object(api, "send_message", track("api_callable_total", original_api)))
        # This endpoint always schedules an admin signal; never permit a real network connection.
        # Stub only the optional admin channel; user Redis presence and broadcast are real methods.
        async def silent_admin(*_args, **_kwargs):
            return None
        patches.enter_context(patch.object(api, "send_admin_message_signal", silent_admin))
        patches.enter_context(patch.object(api.moderation_service, "schedule_message_moderation", lambda *_: None))
        patches.enter_context(patch.object(api.response_cache, "invalidate_user_conversations", lambda *_: None))

        for group_size in (5, 200):
            for online_size in (10, 1000):
                case = f"group={group_size},global_online={online_size}"
                async with sessions() as db:
                    # Other online users are purposefully NOT group members (authorization check).
                    db.add_all([User(username=f"synthetic-{case}-{i}", hashed_password="synthetic") for i in range(group_size)])
                    conversation = Conversation(type=ConversationType.GROUP, title="Synthetic group")
                    db.add(conversation)
                    await db.flush()
                    group_user_ids = [i for (i,) in (await db.execute(text("SELECT id FROM users ORDER BY id DESC LIMIT :n"), {"n": group_size})).all()]
                    group_user_ids.reverse()
                    sender_id = group_user_ids[0]
                    db.add_all(ConversationMember(conversation_id=conversation.id, user_id=i,
                                                  role=MemberRole.OWNER if i == sender_id else MemberRole.MEMBER,
                                                  nickname="Synthetic sender" if i == sender_id else None)
                               for i in group_user_ids)
                    await db.commit()
                    conversation_id = conversation.id
                    # Old timestamps avoid the real 20-per-minute sender limit; new sends are
                    # bounded to <= 20 and use a fresh sender for each case.
                    old = datetime.now(timezone.utc) - timedelta(days=1)
                    await db.execute(insert(Message), [dict(conversation_id=conversation_id, sender_id=sender_id,
                                                             type=MessageType.TEXT, content="synthetic history", created_at=old)
                                                       for _ in range(1000)])
                    await db.commit()
                group_online = min(group_size, online_size)
                fake = FakeRedis(set(group_user_ids[:group_online]) |
                                 set(range(1_000_000, 1_000_000 + online_size - group_online)),
                                 1.0 if args.mode == "slow-publish" else 0.0)
                assert len(fake.online) == online_size
                manager = ConnectionManager()
                manager._redis = fake
                manager._pubsub = object()
                # Avoid creating a subscriber/presence heartbeat; enable only fake-Redis mode.
                with ExitStack() as case_patches:
                    case_patches.enter_context(patch.object(manager, "redis_enabled", return_value=True))
                    case_patches.enter_context(patch.object(api, "manager", manager))
                    async with sessions() as db:
                        sender = await db.get(User, sender_id)
                        payload = MessageCreate(content=(" ".join(f"https://www.ccw.site/detail/{i:024x}" for i in (1, 2, 3))
                                                         if args.mode == "slow-preview" else "synthetic benchmark text"),
                                                type=MessageType.TEXT)
                        for number in range(args.iterations + args.warmup):
                            measured = number >= args.warmup
                            if measured:
                                before = {key: len(values) for key, values in samples.items()}
                                redis_before = dict(fake.commands)
                                trips_before = fake.round_trips
                                started = time.perf_counter_ns()
                            result = await api.send_message(conversation_id, payload, sender, db)
                            # No ASGI/auth/HTTP transport; explicit Pydantic response encode+decode.
                            decode_started = time.perf_counter_ns()
                            decoded = MessageRead.model_validate_json(result.model_dump_json())
                            if decoded.id != result.id:
                                raise AssertionError("response decode mismatch")
                            decode_ms = (time.perf_counter_ns() - decode_started) / 1e6
                            await asyncio.sleep(0)  # let bounded admin signal tasks settle
                            if measured:
                                samples[case + "/response_encode_decode"].append(decode_ms)
                                samples[case + "/total_with_response_decode"].append((time.perf_counter_ns() - started) / 1e6)
                                for key, values in list(samples.items()):
                                    if "/" not in key:
                                        samples[case + "/" + key].extend(values[before.get(key, 0):])
                                for command, count in fake.commands.items():
                                    total_commands[case + "/" + command] += count - redis_before.get(command, 0)
                                total_round_trips += fake.round_trips - trips_before
                                peak_rss = max(peak_rss, rss_bytes())
                        if fake.last_publish is None:
                            raise AssertionError("Redis user fanout was not exercised")
                        event_payload = json.loads(fake.last_publish)
                        if event_payload["namespace"] != "user" or len(event_payload["owner_ids"]) != group_online:
                            raise AssertionError("online recipient authorization/fanout mismatch")
    await engine.dispose()
    socket.socket.connect = original_connect
    socket.create_connection = original_create_connection
    return samples, peak_rss, total_commands, total_round_trips


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=12, help="Measured serial sends/case (1..20)")
    parser.add_argument("--warmup", type=int, default=2, help="Unmeasured serial sends/case (0..5)")
    parser.add_argument("--label", default="current", help="Name for comparing reruns or code revisions")
    parser.add_argument("--mode", choices=("plain", "slow-preview", "slow-publish"), default="plain",
                        help="Plain text, three mocked slow CCW links, or 1-second fake Redis PUBLISH")
    parser.add_argument("--preview-delay", type=float, default=0.05, help="Mock CCW fetch sleep/link (0..0.25s)")
    args = parser.parse_args()
    if not 1 <= args.iterations <= 20 or not 0 <= args.warmup <= 5 or args.iterations + args.warmup > 20:
        parser.error("bounded serial workload requires 1..20 iterations, 0..5 warmup, sum <= 20")
    if not 0 <= args.preview_delay <= 0.25:
        parser.error("--preview-delay must be within 0..0.25 seconds")
    with tempfile.TemporaryDirectory(prefix="kuke-message-send-bench-") as directory:
        os.chdir(directory)
        # The settings module has env_file='.env'; empty cwd prevents reading it.
        # Erase inherited application secrets, DATABASE_URL, REDIS_URL, proxy vars, etc.
        os.environ.clear()
        os.environ["JWT_SECRET_KEY"] = "synthetic-benchmark-only-key-000000000000"
        try:
            samples, peak, commands, round_trips = asyncio.run(run(args))
        finally:
            os.chdir(ROOT)  # Windows cannot remove the current working directory.
    print(f"label={args.label} mode={args.mode} real_service+API_callable+mock_Redis; HTTP/auth/ASGI/network/admin_signal=excluded")
    print(f"serial measured sends per case={args.iterations}, warmup={args.warmup}, history/case=1000; peak RSS MiB={peak / 1048576:.2f}")
    for case in (f"group={group},global_online={online}" for group in (5, 200) for online in (10, 1000)):
        print("\n" + case)
        print(f"  online group recipients={min(int(case.split(',')[0].split('=')[1]), int(case.split('=')[-1]))}")
        for segment in ("rate_limit", "require_membership", "insert_flush", "activity_flush", "activity", "preview_enrichment", "prune", "commit", "service_total",
                        "online_recipient_lookup", "redis_broadcast_and_local_enqueue", "api_callable_total",
                        "response_encode_decode", "total_with_response_decode"):
            values = samples[case + "/" + segment]
            if values:
                print(f"  {segment:36} p50={percentile(values, 50):8.3f} p95={percentile(values, 95):8.3f} p99={percentile(values, 99):8.3f} ms (n={len(values)})")
        print("  mock Redis commands:", {k.rsplit("/", 1)[-1]: v for k, v in sorted(commands.items()) if k.startswith(case + "/")})
    print(f"mock Redis total command count={sum(commands.values())}, total round trips={round_trips}")
    print("Caveats: no HTTP auth/dependency/ASGI transport, no real Redis latency or sockets; SQLite in-memory, serial process RSS peak sampling.")


if __name__ == "__main__":
    main()
