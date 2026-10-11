"""Offline synthetic *body-byte model*, not production traffic or a load test.

Run: python tests/bench_transport_bytes.py [--workers 4] [--new-workers 0]

No app imports, .env, external services, real sockets, websocket tasks, uploads,
user records, or HTTP requests. A 100/1000-socket scenario is arithmetic only:
each synthetic owner has two sockets; groups have 10/500 member IDs. The ledger
counts serialized event bytes once per *actual receiving socket*. Existing
workers have four namespace managers (user/admin/bot/game), each subscribed to
the SAME Redis channel: 4 subscriptions per old worker, including irrelevant
namespace deliveries. The optional --new-workers models one shared subscriber
per new worker (hypothetical change, NOT implemented/measured here). During
rollout Redis fanout is 4 * W_old + W_new, not just the publishing namespace.
Publish ingress is listed separately and never multiplied by subscribers.
HTTP rows assume one JSON GET per socket and one JPEG/GIF GET per ten sockets.
The JSON identity/gzip rows are alternative encodings of the SAME requests;
do not sum them together. Only fixed labels, counts and byte lengths are emitted;
fixture payloads and member IDs are NEVER printed. Excludes WS frame/header/TLS,
Redis protocol, requests/headers, caches, retransmits, presence DB work and
network overhead.
"""
from __future__ import annotations

import argparse
import gzip
import json


SOCKET_COUNTS = (100, 1000)
GROUP_MEMBERS = (10, 500)
SOCKETS_PER_OWNER = 2
OLD_SUBSCRIPTIONS_PER_WORKER = 4  # user/admin/bot/game each listen on one shared channel
NEW_SUBSCRIPTIONS_PER_WORKER = 1  # hypothetical shared subscriber; not deployed here
MEDIA_BODY_BYTES = {"/uploads/synthetic.jpg": 128 * 1024,
                    "/uploads/synthetic.gif": 512 * 1024}


def _json_bytes(value: object, *, redis: bool = False) -> bytes:
    # Starlette WebSocket.send_json uses compact separators; manager._publish
    # calls json.dumps(message) with the standard default separators.
    if redis:
        return json.dumps(value, ensure_ascii=True).encode("utf-8")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _event_rows(event_type: str, data: dict, owner_ids: list[int] | None,
                recipient_sockets: int, old_workers: int, new_workers: int) -> list[dict]:
    # The model's fixture is already JSON-encodable: no app import/jsonable_encoder
    # or repeated per-recipient encoding. Serialize each of the two wire formats
    # exactly once and multiply their byte sizes, without creating any sockets.
    event = {"type": event_type, "data": data}
    action = "broadcast" if owner_ids is None else "users"
    redis_message = {
        "origin": "0" * 32, "namespace": "user", "action": action,
        "owner_ids": owner_ids, "event_type": event_type, "data": data,
    }
    ws_bytes = len(_json_bytes(event))
    publish_bytes = len(_json_bytes(redis_message, redis=True))
    return [
        {"path": "/ws", "event": event_type, "kind": "websocket_body",
         "bytes_per_delivery": ws_bytes, "deliveries": recipient_sockets,
         "total_bytes": ws_bytes * recipient_sockets},
        {"path": "redis:publish", "event": event_type, "kind": "publish_body",
         "bytes_per_delivery": publish_bytes, "deliveries": 1,
         "total_bytes": publish_bytes},
        {"path": "redis:pubsub", "event": event_type, "kind": "subscriber_body",
         "bytes_per_delivery": publish_bytes,
         "deliveries": OLD_SUBSCRIPTIONS_PER_WORKER * old_workers
                       + NEW_SUBSCRIPTIONS_PER_WORKER * new_workers,
         "total_bytes": publish_bytes * (OLD_SUBSCRIPTIONS_PER_WORKER * old_workers
                                          + NEW_SUBSCRIPTIONS_PER_WORKER * new_workers)},
    ]


def build_ledger(socket_count: int, member_count: int, workers: int = 4,
                 new_workers: int = 0) -> dict:
    """Return a synthetic summary: workers are old subscribers; new_workers are optional."""
    if (socket_count not in SOCKET_COUNTS or member_count not in GROUP_MEMBERS
            or workers < 0 or new_workers < 0 or workers + new_workers < 1):
        raise ValueError("sockets: 100/1000; members: 10/500; old/new workers: >=0, at least one total")
    online_owners = socket_count // SOCKETS_PER_OWNER
    # Owner IDs 1..N are arithmetic, not instantiated connections. The caller's
    # authorized, online recipients are the intersection of group and online IDs.
    recipient_ids = list(range(1, min(member_count, online_owners) + 1))
    recipient_sockets = len(recipient_ids) * SOCKETS_PER_OWNER
    rows = _event_rows(
        "presence.updated",
        {"user_id": 1, "online": True, "online_count": online_owners,
         "presence_status": "online", "presence_text": None,
         "presence_updated_at": None},
        None, socket_count, workers, new_workers,
    )
    rows += _event_rows(
        "message.created",
        {"id": 1, "conversation_id": 1, "sender_id": 1,
         "content": "FIXTURE_PRIVATE_CONTENT_MUST_NOT_APPEAR_IN_OUTPUT"},
        recipient_ids, recipient_sockets, workers, new_workers,
    )
    # HTTP is body bytes only; these are imaginary file lengths, not fake files
    # or measured JPEG/GIF compression ratios. Media remains identity encoded.
    media_requests = socket_count // 10
    for path, body_bytes in MEDIA_BODY_BYTES.items():
        rows.append({"path": path, "event": "synthetic_get", "kind": "http_identity_body",
                     "bytes_per_delivery": body_bytes, "deliveries": media_requests,
                     "total_bytes": body_bytes * media_requests})
    http_body = _json_bytes({"items": [
        {"id": index, "title": "synthetic-item", "unread_count": 0}
        for index in range(60)
    ]})
    for kind, encoded in (("http_identity_body", http_body),
                          ("http_gzip_body", gzip.compress(http_body, mtime=0))):
        rows.append({"path": "/api/synthetic/json", "event": "synthetic_get",
                     "kind": kind, "bytes_per_delivery": len(encoded),
                     "deliveries": socket_count, "total_bytes": len(encoded) * socket_count})
    return {
        "model_only_not_production_usage": True,
        "socket_count": socket_count, "distinct_online_owners": online_owners,
        "group_member_count": member_count, "online_group_owners": len(recipient_ids),
        "message_recipient_sockets": recipient_sockets,
        "redis_old_workers": workers, "redis_new_workers": new_workers,
        "redis_old_subscribers_per_worker": OLD_SUBSCRIPTIONS_PER_WORKER,
        "redis_new_subscribers_per_worker": NEW_SUBSCRIPTIONS_PER_WORKER,
        "redis_total_subscribers": OLD_SUBSCRIPTIONS_PER_WORKER * workers
                                   + NEW_SUBSCRIPTIONS_PER_WORKER * new_workers,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4,
                        help="old workers with four subscriptions each (default: 4)")
    parser.add_argument("--new-workers", type=int, default=0,
                        help="hypothetical new workers with one shared subscription each (default: 0)")
    args = parser.parse_args(argv)
    if args.workers < 0 or args.new_workers < 0 or args.workers + args.new_workers < 1:
        parser.error("old/new worker counts must be nonnegative with at least one total")
    print(json.dumps({"disclaimer": "Synthetic body-byte model only; NOT measured production traffic. "
                                    "One shared subscriber per new worker is hypothetical, not deployed here.",
                      "scenarios": [build_ledger(sockets, members, args.workers, args.new_workers)
                                    for sockets in SOCKET_COUNTS for members in GROUP_MEMBERS]},
                     separators=(",", ":")))


if __name__ == "__main__":
    main()
