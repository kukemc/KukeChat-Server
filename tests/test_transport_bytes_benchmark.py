"""Pure-model regression tests: no ASGI server, Redis, app import, or websocket tasks."""
from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import unittest


_SCRIPT = Path(__file__).with_name("bench_transport_bytes.py")
_spec = importlib.util.spec_from_file_location("bench_transport_bytes", _SCRIPT)
assert _spec is not None and _spec.loader is not None
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


class TransportBytesBenchmarkTests(unittest.TestCase):
    def test_actual_group_socket_recipients_not_all_connected_sockets(self):
        for sockets, group, expected in ((100, 10, 20), (100, 500, 100),
                                         (1000, 10, 20), (1000, 500, 1000)):
            with self.subTest(sockets=sockets, group=group):
                ledger = bench.build_ledger(sockets, group)
                message_ws = next(row for row in ledger["rows"]
                                  if row["path"] == "/ws" and row["event"] == "message.created")
                presence_ws = next(row for row in ledger["rows"]
                                   if row["path"] == "/ws" and row["event"] == "presence.updated")
                self.assertEqual(message_ws["deliveries"], expected)
                self.assertEqual(message_ws["total_bytes"], message_ws["bytes_per_delivery"] * expected)
                self.assertEqual(presence_ws["deliveries"], sockets)
                self.assertEqual(ledger["message_recipient_sockets"], expected)

    def test_redis_publish_and_subscriber_fanout_are_separate(self):
        baseline = bench.build_ledger(100, 10, workers=3)
        self.assertEqual(baseline["redis_old_subscribers_per_worker"], 4)
        self.assertEqual(baseline["redis_new_subscribers_per_worker"], 1)
        self.assertEqual(baseline["redis_total_subscribers"], 12)
        rows = baseline["rows"]
        for event in ("presence.updated", "message.created"):
            ingress, subscribers = (next(row for row in rows if row["event"] == event
                                          and row["path"] == path)
                                    for path in ("redis:publish", "redis:pubsub"))
            self.assertEqual(ingress["deliveries"], 1)
            self.assertEqual(subscribers["deliveries"], 4 * 3)
            self.assertEqual(ingress["bytes_per_delivery"], subscribers["bytes_per_delivery"])
            self.assertEqual(subscribers["total_bytes"], ingress["total_bytes"] * (4 * 3))
        small = next(row for row in rows if row["event"] == "message.created"
                     and row["path"] == "redis:publish")
        large = next(row for row in bench.build_ledger(1000, 500)["rows"]
                     if row["event"] == "message.created" and row["path"] == "redis:publish")
        self.assertGreater(large["bytes_per_delivery"], small["bytes_per_delivery"])

    def test_rolling_migration_and_shared_subscriber_are_not_baseline(self):
        for old, new, expected in ((3, 0, 12), (2, 1, 9), (0, 3, 3)):
            with self.subTest(old=old, new=new):
                scenario = bench.build_ledger(100, 10, workers=old, new_workers=new)
                self.assertEqual(scenario["redis_old_workers"], old)
                self.assertEqual(scenario["redis_new_workers"], new)
                self.assertEqual(scenario["redis_total_subscribers"], expected)
                for event in ("presence.updated", "message.created"):
                    subscriber = next(row for row in scenario["rows"]
                                      if row["event"] == event and row["path"] == "redis:pubsub")
                    ingress = next(row for row in scenario["rows"]
                                   if row["event"] == event and row["path"] == "redis:publish")
                    self.assertEqual(subscriber["deliveries"], 4 * old + new)
                    self.assertEqual(subscriber["total_bytes"], ingress["total_bytes"] * expected)

    def test_http_identity_media_and_gzip_json(self):
        rows = bench.build_ledger(1000, 10)["rows"]
        media = [row for row in rows if row["path"].startswith("/uploads/")]
        self.assertEqual({row["path"].split(".")[-1] for row in media}, {"jpg", "gif"})
        self.assertTrue(all(row["kind"] == "http_identity_body" and row["deliveries"] == 100
                            for row in media))
        identity, compressed = (next(row for row in rows if row["path"] == "/api/synthetic/json"
                                     and row["kind"] == kind)
                                for kind in ("http_identity_body", "http_gzip_body"))
        self.assertEqual(identity["deliveries"], 1000)
        self.assertLess(compressed["total_bytes"], identity["total_bytes"])

    def test_output_contains_no_message_content_or_recipient_ids(self):
        output = io.StringIO()
        with redirect_stdout(output):
            bench.main(["--workers", "2", "--new-workers", "1"])
        text = output.getvalue()
        self.assertNotIn("FIXTURE_PRIVATE_CONTENT_MUST_NOT_APPEAR_IN_OUTPUT", text)
        self.assertNotIn("owner_ids", text)
        self.assertNotIn("sender_id", text)
        doc = json.loads(text)
        self.assertEqual(len(doc["scenarios"]), 4)
        self.assertTrue(all(item["model_only_not_production_usage"] for item in doc["scenarios"]))
        self.assertIn("NOT measured production traffic", doc["disclaimer"])
        self.assertTrue(all(item["redis_old_workers"] == 2 and item["redis_new_workers"] == 1
                            and item["redis_total_subscribers"] == 9 for item in doc["scenarios"]))

    def test_invalid_scenario_is_rejected(self):
        for options in ((99, 10, 4, 0), (100, 11, 4, 0),
                        (100, 10, 0, 0), (100, 10, -1, 1), (100, 10, 1, -1)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                bench.build_ledger(*options)


if __name__ == "__main__":
    unittest.main()
