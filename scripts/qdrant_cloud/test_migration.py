import copy
import http.server
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import common
import contract
import migrate
import verify


COUNTS = {"yudingquantangshi": 2, "tangshisanbaishou": 1}


def point(index, dataset="yudingquantangshi"):
    vector = [0.0] * 1024
    vector[index] = 1.0
    return {"id": str(uuid.UUID(int=index + 1)), "vector": vector,
            "payload": {"dataset": dataset, "source_row_id": index + 1,
                        "raw_index": 0, "normalized_index": 0,
                        "work_id": f"{dataset}:{index + 1}",
                        "generation": contract.GENERATION, "translation_hash": "a" * 64,
                        "extra": [{"enabled": True, "optional": None, "score": 1.25}]}}


def config():
    return {"params": {"vectors": {"size": 1024, "distance": "Cosine", "on_disk": True},
                       "on_disk_payload": True},
            "hnsw_config": {"m": 16, "on_disk": False, "ef_construct": 100},
            "quantization_config": {"scalar": {"type": "int8", "always_ram": True}},
            "optimizer_config": {"indexing_threshold": 1000}}


class FakeClient:
    def __init__(self, source=False):
        self.url = "http://127.0.0.1:17333" if source else "https://target.cloud.qdrant.io:443"
        records = [point(0), point(1), point(2, "tangshisanbaishou")] if source else []
        self.records = {record["id"]: record for record in records}
        self.exists, self.config = source, config()
        self.indexes = {field: {"data_type": "keyword"} for field in ("dataset", "generation")}
        self.fail_after_upsert, self.upserts = False, 0
        self.indexed = True

    def request(self, method, path, payload=None, missing=False):
        if path == "/":
            return {"version": "1.19.1"}
        if method == "GET" and path == contract.PATH:
            if not self.exists and missing:
                return None
            return {"config": self.config, "payload_schema": self.indexes,
                    "status": "green", "optimizer_status": "ok",
                    "points_count": len(self.records),
                    "indexed_vectors_count": len(self.records) if self.indexed else 0}
        if method == "PUT" and path == contract.PATH:
            self.exists = True
            return True
        if "/index?" in path:
            self.indexes[payload["field_name"]] = {"data_type": payload["field_schema"]}
            return True
        if path.endswith("/count"):
            return {"count": len(self.filtered(payload))}
        if path.endswith("/scroll"):
            records = [record for key, record in sorted(self.records.items())
                       if payload.get("offset") is None or key >= payload["offset"]]
            page = records[:payload["limit"]]
            offset = records[len(page)]["id"] if len(records) > len(page) else None
            return {"points": copy.deepcopy(page), "next_page_offset": offset}
        if path.endswith("/points?wait=true"):
            for record in payload["points"]:
                self.records[record["id"]] = copy.deepcopy(record)
            self.upserts += 1
            if self.fail_after_upsert:
                self.fail_after_upsert = False
                raise RuntimeError("Simulated interruption after durable target write")
            return {"status": "completed"}
        if path.endswith("/points/query"):
            ranked = sorted(self.filtered(payload), key=lambda record: sum(
                left * right for left, right in zip(record["vector"], payload["query"])
            ), reverse=True)
            result = copy.deepcopy(ranked[0])
            result["score"] = sum(left * right for left, right in zip(
                result.pop("vector"), payload["query"]))
            return {"points": [result]}
        if path.endswith("/points"):
            return [copy.deepcopy(self.records[key]) for key in payload["ids"]]
        raise AssertionError(f"Unexpected request: {method} {path}")

    def filtered(self, payload):
        rows = list(self.records.values())
        for condition in payload.get("filter", {}).get("must", []):
            rows = [row for row in rows
                    if row["payload"].get(condition["key"]) == condition["match"]["value"]]
        return rows


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.args = SimpleNamespace(action="migrate", checkpoint=root / "state.json",
                                    report=root / "report.json", batch_size=2, batch_delay=0,
                                    index_timeout=60, samples_per_dataset=2,
                                    allow_normalization_drift=False)
        self.source, self.target = FakeClient(True), FakeClient()
        for module in (contract, migrate, verify):
            overrides = {"TOTAL": 3}
            if hasattr(module, "COUNTS"):
                overrides["COUNTS"] = COUNTS
            patcher = patch.multiple(module, **overrides)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_interruption_after_upsert_resumes_without_duplicate_or_lost_points(self):
        self.target.fail_after_upsert = True
        with self.assertRaisesRegex(RuntimeError, "Simulated interruption"):
            migrate.execute(self.source, self.target, self.args)
        self.assertEqual(json.loads(self.args.checkpoint.read_text())["copied"], 0)
        self.assertEqual(len(self.target.records), 2)
        self.assertTrue(migrate.execute(self.source, self.target, self.args))
        self.assertEqual(self.source.records, self.target.records)
        state = json.loads(self.args.checkpoint.read_text())
        self.assertEqual((state["copied"], state["phase"]), (3, "verified"))
        self.assertEqual(self.args.checkpoint.stat().st_mode & 0o777, 0o600)

    def test_existing_collection_without_checkpoint_is_rejected(self):
        self.target.exists = True
        with self.assertRaisesRegex(ValueError, "without this migration checkpoint"):
            migrate.execute(self.source, self.target, self.args)
        self.assertEqual(self.target.upserts, 0)

    def test_changed_source_or_endpoint_cannot_reuse_checkpoint(self):
        self.assertTrue(migrate.execute(self.source, self.target, self.args))
        self.target.url = "https://other.cloud.qdrant.io:443"
        with self.assertRaisesRegex(ValueError, "Checkpoint endpoints"):
            migrate.execute(self.source, self.target, self.args)
        self.target.url = "https://target.cloud.qdrant.io:443"
        self.source.records[point(0)["id"]]["payload"]["translation_hash"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "Checkpoint endpoints"):
            migrate.execute(self.source, self.target, self.args)

    def test_plan_reads_empty_target_without_creating_collection(self):
        self.args.action = "plan"
        self.assertTrue(migrate.execute(self.source, self.target, self.args))
        self.assertFalse(self.target.exists)
        self.assertFalse(self.args.checkpoint.exists())


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.source, self.target = FakeClient(True), FakeClient(True)
        for module in (contract, verify):
            patcher = patch.multiple(module, TOTAL=3, COUNTS=COUNTS)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_full_comparison_includes_payload_types_and_both_filters(self):
        result = verify.verify(self.source, self.target, 2, 2)
        self.assertTrue(result["success"])
        self.assertEqual(result["vectors"]["changed_float32_vectors"], 0)
        self.assertEqual(result["search"]["queries_per_endpoint"], 6)
        filters = {row["dataset_filter"] for row in result["search"]["rows"]}
        self.assertEqual(filters, {None, *COUNTS})
        self.target.records[point(0)["id"]]["payload"]["extra"][0]["enabled"] = 1
        result = verify.verify(self.source, self.target, 2, 2)
        self.assertFalse(result["success"])

    def test_normalization_drift_requires_explicit_acceptance(self):
        self.source.records[point(0)["id"]]["vector"][0] = 1.0000001
        result = verify.verify(self.source, self.target, 2, 1)
        self.assertFalse(result["success"])
        self.assertEqual(result["vectors"]["normalization_candidates"], 1)
        result = verify.verify(self.source, self.target, 2, 1, allow_normalization_drift=True)
        self.assertTrue(result["success"])
        self.target.records[point(0)["id"]]["vector"][0] = 0.5
        result = verify.verify(self.source, self.target, 2, 1, allow_normalization_drift=True)
        self.assertFalse(result["success"])

    def test_count_is_not_enough_when_indexing_is_incomplete(self):
        self.target.indexed = False
        with self.assertRaisesRegex(ValueError, "fully indexed"):
            verify.verify(self.source, self.target, 2, 1)


class Handler(http.server.BaseHTTPRequestHandler):
    attempts = 0
    received = []

    def do_GET(self):
        self.respond(200, {"title": "qdrant", "version": "1.19.1", "commit": "test"})

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append(body)
        type(self).attempts += 1
        if self.path == "/rejected":
            self.respond(401, {"error": "sensitive response content"})
        elif type(self).attempts == 1:
            self.respond(503, {"status": {"error": "busy"}})
        else:
            self.respond(200, {"status": "ok", "result": {"status": "completed"}})

    def respond(self, status, body):
        content = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


class ClientTests(unittest.TestCase):
    def setUp(self):
        Handler.attempts, Handler.received = 0, []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = common.Client(f"http://127.0.0.1:{self.server.server_port}", "test-secret")
        self.addCleanup(self.stop)

    def stop(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_root_version_document_and_bounded_idempotent_retry(self):
        self.assertEqual(self.client.request("GET", "/")["version"], "1.19.1")
        payload = {"points": [point(0)]}
        with patch("common.time.sleep"):
            self.assertEqual(self.client.request("PUT", "/retry", payload)["status"], "completed")
        self.assertEqual(Handler.received, [payload, payload])

    def test_errors_do_not_expose_credentials_or_response_content(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as error:
            self.client.request("PUT", "/rejected", {})
        self.assertNotIn("test-secret", str(error.exception))
        self.assertNotIn("sensitive", str(error.exception))

    def test_cloud_endpoint_rejects_credentials_and_non_cloud_host(self):
        for url in ("https://user:key@target.cloud.qdrant.io", "http://target.cloud.qdrant.io",
                    "https://example.com", "https://target.cloud.qdrant.io?api-key=secret"):
            with self.assertRaises(ValueError):
                common.endpoint(url, cloud=True)


if __name__ == "__main__":
    unittest.main()
