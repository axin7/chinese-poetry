import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from apply import apply, initial_state, inspect_plan, reviewed_plan, verify
from batches import batches
from canonical import Canonical, NAMESPACE
from prepare import prepare
from support import (GENERATION, MAX_BATCH_BYTES, OWNED, PATH, SCHEMA,
                     atomic_json, digest_file, encoded)


def fixture(directory):
    path = directory / "canonical.sqlite"
    table = "tangshisanbaishou"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);"
                         "CREATE TABLE works(work_id TEXT PRIMARY KEY,generation TEXT,"
                         "document TEXT,locators TEXT);")
        for row in (1, 3, 4):
            work_id = f"{table}:{row}"
            original = [f"first {row}", f"second {row}"]
            detail = {"id": work_id, "dataset": table, "title": "Quoted 'title'",
                      "author": None, "original": original, "translation": ["one", "two"],
                      "interpretations": []}
            second_row = 2 if row == 1 else row
            locators = {f"{row}:0:0": original[0], f"{second_row}:2:1": original[1]}
            db.execute("INSERT INTO works VALUES (?,?,?,?)",
                       (work_id, GENERATION, json.dumps(detail), json.dumps(locators)))
        metadata = {"generation": GENERATION, "sentences_count": "6", "works_count": "3",
                    "dataset_counts": json.dumps({table: 6})}
        db.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
    report = directory / "canonical-report.json"
    atomic_json(report, {"generation": GENERATION, "sqlite": {"sha256": digest_file(path)},
                         "works": 3, "verified_works": 3,
                         "sentences": 6, "verified_sentences": 6})
    return path, report


def config():
    return {"params": {"vectors": {"size": 1024, "distance": "Cosine", "on_disk": True},
                       "on_disk_payload": True},
            "hnsw_config": {"m": 16, "on_disk": False},
            "quantization_config": {"scalar": {"type": "int8", "always_ram": True}}}


class FakeClient:
    def __init__(self, canonical):
        self.url = "https://example.cloud.qdrant.io:443"
        self.schema = {"generation": {"data_type": "keyword"},
                       "dataset": {"data_type": "keyword"}}
        self.data, self.mutations, self.fail_after_update = {}, [], False
        self.red_after_update = False
        for point_id, expected in canonical.points.items():
            payload = {key: value for key, value in expected.items() if key != "original"}
            payload["translation_hash"] = hashlib.sha256(b"canonical translation").hexdigest()
            self.data[point_id] = {"id": point_id, "payload": payload}

    def request(self, method, path, payload=None, missing=False):
        if method == "GET" and path == PATH:
            updated = any(path.endswith("/points/batch?wait=true")
                          for _, path in self.mutations)
            return {"status": "red" if self.red_after_update and updated else "green",
                    "points_count": len(self.data),
                    "indexed_vectors_count": len(self.data), "config": config(),
                    "payload_schema": copy.deepcopy(self.schema)}
        if method == "POST" and path.endswith("/scroll"):
            assert payload["with_vector"] is False and payload["with_payload"] is True
            ids = sorted(point_id for point_id in self.data
                         if payload.get("offset") is None or point_id > payload["offset"])
            selected = ids[:payload["limit"]]
            return {"points": [copy.deepcopy(self.data[key]) for key in selected],
                    "next_page_offset": selected[-1] if len(selected) < len(ids) else None}
        if method == "PUT" and path.endswith("/index?wait=true"):
            assert payload == {"field_name": "work_id",
                               "field_schema": {"type": "keyword", "on_disk": True}}
            self.schema["work_id"] = {"data_type": "keyword",
                                      "params": {"type": "keyword", "on_disk": True}}
            self.mutations.append((method, path))
            return {"status": "completed"}
        if method == "POST" and path.endswith("/points/batch?wait=true"):
            assert len(encoded(payload)) <= MAX_BATCH_BYTES
            assert len(payload["operations"]) <= 128
            for operation in payload["operations"]:
                assert set(operation) == {"set_payload"}
                update = operation["set_payload"]
                assert set(update) == {"payload", "points"}
                assert set(update["payload"]).issubset(OWNED)
                self.data[update["points"][0]]["payload"].update(update["payload"])
            self.mutations.append((method, path))
            if self.fail_after_update:
                self.fail_after_update = False
                raise RuntimeError("Injected disconnect after completed additive writes")
            return [{"status": "completed"} for _ in payload["operations"]]
        raise AssertionError((method, path))


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db, self.report = fixture(self.root)
        self.canonical = Canonical(self.db, self.report)
        self.client = FakeClient(self.canonical)
        self.directory = self.root / "plan"
        self.checkpoint = self.root / "apply-state.json"

    def prepare(self):
        return prepare(self.client, self.db, self.report, self.directory)


class PreparationTests(FixtureTest):
    def test_prepare_reads_all_locators_and_uses_min_uuid_anchors_without_writes(self):
        result = self.prepare()
        self.assertEqual(result["inventory"]["points"], 6)
        self.assertEqual(result["sizing"]["work_documents"], 3)
        self.assertEqual(self.client.mutations, [])
        lines = (self.directory / "manifest.jsonl").read_text().splitlines()
        records = [json.loads(line) for line in lines]
        for work_id, anchor in self.canonical.anchors.items():
            matching = [row for row in records if row["before"]["work_id"] == work_id]
            documents = [row for row in matching if "poetry_work_document" in row["additions"]]
            self.assertEqual([row["id"] for row in documents], [min(row["id"] for row in matching)])
            self.assertEqual(documents[0]["id"], anchor)
            self.assertEqual(documents[0]["additions"]["poetry_work_document"],
                             self.canonical.documents[work_id])

    def test_conflicting_fields_or_wrong_locator_reject_preparation(self):
        identifier = next(iter(self.client.data))
        self.client.data[identifier]["payload"]["raw_index"] = 9
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.client.mutations, [])

    def test_modified_manifest_cannot_be_applied(self):
        self.prepare()
        with (self.directory / "manifest.jsonl").open("a") as handle:
            handle.write("{}\n")
        with self.assertRaises(ValueError):
            apply(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertEqual(self.client.mutations, [])

    def test_readonly_preflight_validates_anchor_coverage(self):
        self.prepare()
        result = inspect_plan(self.client, self.directory)
        self.assertEqual(result["work_id_coverage"], 3)
        self.assertEqual(result["document_anchors"], 3)
        self.assertEqual(result["anchor_order"], "default UUID ascending")
        self.assertEqual(self.client.mutations, [])

    def test_preexisting_api_field_cannot_be_claimed(self):
        key = next(iter(self.client.data))
        self.client.data[key]["payload"]["poetry_api_schema"] = SCHEMA
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.client.mutations, [])


class ApplicationTests(FixtureTest):
    def test_batch_loop_stops_before_next_write_when_optimizer_turns_red(self):
        self.prepare()
        self.client.red_after_update = True
        with patch("apply.batches", lambda records: batches(records, max_operations=1)):
            with self.assertRaises(ValueError):
                apply(self.client, self.directory, self.checkpoint, explicit=True)
        writes = [path for _, path in self.client.mutations
                  if path.endswith("/points/batch?wait=true")]
        self.assertEqual(len(writes), 1)
        self.assertEqual(json.loads(self.checkpoint.read_text())["next_operation"], 1)

    def test_apply_preserves_original_payloads_and_verifies_all_metadata(self):
        original = copy.deepcopy(self.client.data)
        self.prepare()
        with patch("apply.time.sleep"):
            result = apply(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["work_documents"], 3)
        for key, point in self.client.data.items():
            non_owned = {k: v for k, v in point["payload"].items() if k not in OWNED}
            self.assertEqual(non_owned, original[key]["payload"])
        state = json.loads(self.checkpoint.read_text())
        self.assertEqual(state["next_operation"], 6)
        self.assertTrue(state["index_created"])
        self.assertEqual(state["phase"], "verified")

    def test_apply_needs_explicit_flag_and_detects_original_payload_changes(self):
        self.prepare()
        with self.assertRaises(ValueError):
            apply(self.client, self.directory, self.checkpoint)
        key = next(iter(self.client.data))
        self.client.data[key]["payload"]["translation_hash"] = "a" * 64
        with self.assertRaises(ValueError):
            apply(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertEqual(self.client.mutations, [])

    def test_unacknowledged_completed_batch_replays_with_bound_checkpoint(self):
        self.prepare()
        self.client.fail_after_update = True
        with self.assertRaises(RuntimeError):
            apply(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertEqual(json.loads(self.checkpoint.read_text())["next_operation"], 0)
        with patch("apply.time.sleep"):
            result = apply(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertTrue(result["success"])
        self.assertEqual(sum(path.endswith("/index?wait=true")
                             for _, path in self.client.mutations), 1)
        self.client.url = "https://another.cloud.qdrant.io:443"
        with self.assertRaises(ValueError):
            reviewed_plan(self.directory, self.client)

    def test_existing_work_index_is_preserved_and_not_owned(self):
        self.client.schema["work_id"] = {"data_type": "keyword"}
        self.prepare()
        with patch("apply.time.sleep"):
            apply(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertFalse(json.loads(self.checkpoint.read_text())["index_created"])
        self.assertFalse(any(method == "PUT" for method, _ in self.client.mutations))

    def test_verification_rejects_changed_original_or_document(self):
        self.prepare()
        with patch("apply.time.sleep"):
            apply(self.client, self.directory, self.checkpoint, explicit=True)
        anchor = next(iter(self.canonical.anchors.values()))
        self.client.data[anchor]["payload"]["poetry_work_document"] = "{}"
        with self.assertRaises(ValueError):
            verify(self.client, self.directory)

    def test_checkpoint_cannot_be_ahead_of_remote_metadata(self):
        plan = self.prepare()
        state = initial_state(plan)
        state["next_operation"] = 6
        atomic_json(self.checkpoint, state)
        with self.assertRaises(ValueError):
            apply(self.client, self.directory, self.checkpoint, explicit=True)
        self.assertEqual(self.client.mutations, [])


class ContractTests(unittest.TestCase):
    def test_uuid_matches_existing_go_importer_contract(self):
        self.assertEqual(str(uuid.uuid5(NAMESPACE, "v1:tangsong:7:2:1")),
                         "a118c345-2319-5907-aa39-2e047233f3e1")

    def test_batches_bound_actual_ascii_encoded_request_bytes_and_operations(self):
        records = [{"id": str(uuid.uuid4()), "additions": {
            "poetry_api_schema": SCHEMA, "poetry_original": "\u6708",
            "poetry_work_document": "\u65e5'" * 40000}} for _ in range(10)]
        packed = list(batches(records, max_operations=3))
        self.assertEqual(sum(len(row["operations"]) for row in packed), 10)
        self.assertTrue(all(len(encoded(row)) <= MAX_BATCH_BYTES for row in packed))
        self.assertTrue(all(len(row["operations"]) <= 3 for row in packed))
        self.assertGreater(len(packed), 3)


if __name__ == "__main__":
    unittest.main()
