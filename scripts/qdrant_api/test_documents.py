import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from documents_contract import (COLLECTION, CREATE_CONFIG, DOCUMENT_PATH, DocumentCorpus,
                                document_batches)
from documents_plan import prepare, reviewed_manifest
from documents_publish import publish, verify
from support import MAX_BATCH_BYTES, PATH, encoded
from test_tools import fixture


class DocumentClient:
    def __init__(self, original_points):
        self.url = "https://example.cloud.qdrant.io:443"
        self.original_points = original_points
        self.exists, self.points, self.indexes = False, {}, {}
        self.mutations, self.source_status = [], "green"
        self.fail_after_write, self.red_after_write = False, False

    def request(self, method, path, payload=None, missing=False):
        if method == "GET" and path == PATH:
            return {"status": self.source_status, "optimizer_status": "ok",
                    "points_count": self.original_points}
        if method == "GET" and path == DOCUMENT_PATH:
            if not self.exists:
                if missing:
                    return None
                raise ValueError("Document collection does not exist")
            return {"status": "green", "optimizer_status": "ok",
                    "points_count": len(self.points), "indexed_vectors_count": 0,
                    "config": {"uuid": "created-documents-uuid", "params": {
                        "vectors": {}, "on_disk_payload": True}},
                    "payload_schema": copy.deepcopy(self.indexes)}
        if method == "PUT" and path == DOCUMENT_PATH:
            assert payload == CREATE_CONFIG and payload["vectors"] == {}
            self.exists = True
            self.mutations.append((method, path, copy.deepcopy(payload)))
            return True
        if method == "PUT" and path.endswith("/index?wait=true"):
            assert path.startswith(DOCUMENT_PATH)
            assert payload["field_name"] in {"generation", "work_id"}
            assert payload["field_schema"] == {"type": "keyword", "on_disk": True}
            self.indexes[payload["field_name"]] = {
                "data_type": "keyword", "params": copy.deepcopy(payload["field_schema"])}
            self.mutations.append((method, path, copy.deepcopy(payload)))
            return {"status": "completed"}
        if method == "PUT" and path == DOCUMENT_PATH + "/points?wait=true":
            assert len(encoded(payload)) <= MAX_BATCH_BYTES and len(payload["points"]) <= 128
            for point in payload["points"]:
                assert point["vector"] == {}
                self.points[point["id"]] = copy.deepcopy(point)
            self.mutations.append((method, path, copy.deepcopy(payload)))
            if self.red_after_write:
                self.source_status = "red"
            if self.fail_after_write:
                self.fail_after_write = False
                raise RuntimeError("Injected disconnect after document upsert")
            return {"status": "completed"}
        if method == "POST" and path == DOCUMENT_PATH + "/points/scroll":
            assert payload["with_vector"] is True and payload["with_payload"] is True
            ids = sorted(key for key in self.points
                         if payload.get("offset") is None or key > payload["offset"])
            chosen = ids[:min(payload["limit"], 2)]
            return {"points": [copy.deepcopy(self.points[key]) for key in chosen],
                    "next_page_offset": chosen[-1] if len(chosen) < len(ids) else None}
        raise AssertionError((method, path))


class DocumentFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database, self.report = fixture(self.root)
        self.corpus = DocumentCorpus(self.database, self.report)
        self.directory = self.root / "document-plan"
        self.checkpoint = self.root / "document-checkpoint.json"
        self.client = DocumentClient(self.corpus.vector_points)

    def prepare(self):
        return prepare(self.database, self.report, self.directory)


class DocumentPlanTests(DocumentFixture):
    def test_plan_is_local_and_preserves_raw_documents_and_locator_strings(self):
        plan = self.prepare()
        self.assertEqual(plan["points"], 3)
        self.assertEqual(plan["vector_definitions"], 0)
        self.assertEqual(self.client.mutations, [])
        _, _, expected = reviewed_manifest(self.directory)
        self.assertEqual(expected, self.corpus.documents)
        self.assertTrue(all(point["vector"] == {} for point in expected.values()))
        for point in expected.values():
            payload = point["payload"]
            self.assertEqual(json.loads(payload["poetry_work_document"])["id"], payload["work_id"])
            self.assertIsInstance(json.loads(payload["poetry_locators"]), dict)
        rollback = json.loads((self.directory / "rollback.json").read_text())
        self.assertEqual(rollback["collection"], COLLECTION)
        self.assertEqual(rollback["excluded_collection"], "poetry_tang_20260922_v1")
        self.assertFalse(rollback["executed"])

    def test_batches_bound_encoded_utf8_escape_size_and_point_count(self):
        points = [{"id": str(uuid.uuid4()), "vector": {}, "payload": {
            "poetry_work_document": "\u6708'" * 45000}} for _ in range(10)]
        packed = list(document_batches(points, max_points=3))
        self.assertEqual(sum(len(batch["points"]) for batch in packed), 10)
        self.assertTrue(all(len(encoded(batch)) <= MAX_BATCH_BYTES for batch in packed))
        self.assertTrue(all(len(batch["points"]) <= 3 for batch in packed))


class DocumentPublishTests(DocumentFixture):
    def test_publish_creates_only_payload_collection_and_verifies_every_document(self):
        self.prepare()
        result = publish(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["points"], 3)
        self.assertEqual(result["indexed_vectors"], 0)
        self.assertTrue(all(path.startswith(DOCUMENT_PATH)
                            for _, path, _ in self.client.mutations))
        self.assertEqual(self.client.points, self.corpus.documents)
        state = json.loads(self.checkpoint.read_text())
        self.assertTrue(state["created"])
        self.assertEqual(state["phase"], "verified")

    def test_preexisting_collection_or_missing_apply_flag_cannot_be_adopted(self):
        self.prepare()
        with self.assertRaises(ValueError):
            publish(self.client, self.directory, self.checkpoint)
        self.client.exists = True
        with self.assertRaises(ValueError):
            publish(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertEqual(self.client.mutations, [])

    def test_crash_after_upsert_replays_with_bound_checkpoint(self):
        self.prepare()
        self.client.fail_after_write = True
        with self.assertRaises(RuntimeError):
            publish(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertEqual(json.loads(self.checkpoint.read_text())["next_point"], 0)
        result = publish(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertTrue(result["success"])
        self.assertEqual(sum(path == DOCUMENT_PATH for _, path, _ in self.client.mutations), 1)
        self.client.url = "https://another.cloud.qdrant.io:443"
        with self.assertRaises(ValueError):
            publish(self.client, self.directory, self.checkpoint, explicit=True)

    def test_health_is_checked_before_each_batch_and_halts_without_vector_writes(self):
        self.prepare()
        self.client.red_after_write = True
        with patch("documents_publish.document_batches",
                   side_effect=lambda points: document_batches(points, max_points=1)), \
                patch("documents_publish.time.sleep"):
            with self.assertRaises(ValueError):
                publish(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertEqual(len(self.client.points), 1)
        self.assertEqual(json.loads(self.checkpoint.read_text())["next_point"], 1)
        self.assertTrue(all(path.startswith(DOCUMENT_PATH)
                            for _, path, _ in self.client.mutations))

    def test_full_verification_and_resume_reject_changed_canonical_strings(self):
        self.prepare()
        publish(self.client, self.directory, self.checkpoint, explicit=True)
        identifier = next(iter(self.client.points))
        self.client.points[identifier]["payload"]["poetry_locators"] = "{}"
        with self.assertRaises(ValueError):
            verify(self.client, self.directory, self.checkpoint)
        with self.assertRaises(ValueError):
            publish(self.client, self.directory, self.checkpoint, explicit=True)


if __name__ == "__main__":
    unittest.main()
