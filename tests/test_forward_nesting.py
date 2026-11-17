"""合并转发套娃：套得太深的聊天记录要截断，否则序列化超过 pydantic 深度上限，整个会话列表 500。"""

import json
from typing import Any

import pytest
from pydantic import TypeAdapter
from pydantic_core import PydanticSerializationError

from app.services import messages


def bundle(levels: int) -> dict:
    meta: dict | None = None
    for _ in range(levels):
        meta = {
            "source_title": "群",
            "items": [{"message_id": 1, "type": "FORWARD_BUNDLE" if meta else "TEXT", "content": "x", "metadata": meta}],
        }
    return meta  # type: ignore[return-value]


def levels_of(meta: dict | None) -> int:
    count = 0
    while isinstance(meta, dict) and isinstance(meta.get("items"), list):
        count += 1
        inner = meta["items"][0].get("metadata") if meta["items"] else None
        if not (isinstance(inner, dict) and isinstance(inner.get("items"), list)):
            break
        meta = inner
    return count


def test_shallow_bundle_untouched():
    meta = bundle(3)
    assert messages._cap_forward_nesting(meta) is meta


def test_deep_bundle_capped_and_marked():
    capped = messages._cap_forward_nesting(bundle(90))
    assert levels_of(capped) == messages.FORWARD_NESTING_LIMIT + 1
    node = capped
    for _ in range(messages.FORWARD_NESTING_LIMIT):
        node = node["items"][0]["metadata"]
    assert node["items"] == [] and node["truncated"] is True and node["source_title"] == "群"


def test_deep_bundle_serializes_through_read_metadata():
    raw = json.dumps(bundle(90))
    adapter = TypeAdapter(dict[str, Any])
    with pytest.raises(PydanticSerializationError):
        adapter.dump_json(json.loads(raw))  # 线上 500 的原因
    adapter.dump_json(messages._read_metadata(raw))
