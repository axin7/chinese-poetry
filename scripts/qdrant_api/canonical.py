"""Read the already verified canonical Go export; never decode source corpus rows."""

from collections import Counter
import json
from pathlib import Path
import sqlite3
import uuid

from support import GENERATION, digest_file

NAMESPACE = uuid.UUID("cc0fab8e-ff7c-49f2-809d-d96a73b5e4fc")
DETAIL_FIELDS = {"id", "dataset", "title", "author", "original", "translation",
                 "interpretations"}


class Canonical:
    def __init__(self, path, report_path=None):
        self.points, self.documents, self.anchors = {}, {}, {}
        self.counts = Counter()
        self.sha256 = digest_file(path)
        if report_path is not None:
            validate_report(report_path, self.sha256)
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as database:
            metadata = dict(database.execute("SELECT key,value FROM metadata"))
            if metadata.get("generation") != GENERATION:
                raise ValueError("Canonical export generation mismatch")
            query = "SELECT work_id,generation,document,locators FROM works"
            for record in database.execute(query):
                load_work(self, *record)
        self.total = len(self.points)
        if self.total != int(metadata["sentences_count"]):
            raise ValueError("Canonical sentence count mismatch")
        if len(self.documents) != int(metadata["works_count"]):
            raise ValueError("Canonical work count mismatch")
        if dict(self.counts) != json.loads(metadata["dataset_counts"]):
            raise ValueError("Canonical dataset counts mismatch")
        if set(self.anchors) != set(self.documents):
            raise ValueError("A completed work has no existing vector point for its detail")


def validate_report(path, sha256):
    report = json.loads(Path(path).read_text())
    if report.get("generation") != GENERATION or report["sqlite"]["sha256"] != sha256:
        raise ValueError("Canonical Go verification report does not match the export")
    if report["works"] != report["verified_works"] or \
            report["sentences"] != report["verified_sentences"]:
        raise ValueError("Canonical Go export was not fully verified")


def validate_document(work_id, document):
    detail = json.loads(document)
    if not isinstance(detail, dict) or set(detail) != DETAIL_FIELDS or detail["id"] != work_id:
        raise ValueError("Canonical detail document fields differ from the Go API")
    if any(not isinstance(detail[field], str) for field in ("id", "dataset", "title")):
        raise ValueError("Canonical detail text field type mismatch")
    if detail["author"] is not None and not isinstance(detail["author"], str):
        raise ValueError("Canonical author must be a string or null")
    for field in ("original", "translation", "interpretations"):
        if not isinstance(detail[field], list) or \
                any(not isinstance(value, str) for value in detail[field]):
            raise ValueError("Canonical detail arrays must contain strings")
    if len(detail["original"]) != len(detail["translation"]):
        raise ValueError("Canonical original and translation arrays must align")
    return detail


def load_work(canonical, work_id, generation, document, locator_json):
    if generation != GENERATION or work_id in canonical.documents:
        raise ValueError("Canonical work generation or uniqueness mismatch")
    detail = validate_document(work_id, document)
    dataset, anchor = detail["dataset"], work_id.partition(":")[2]
    if work_id != f"{dataset}:{int(anchor)}" or int(anchor) <= 0:
        raise ValueError("Canonical work ID is invalid")
    locators = json.loads(locator_json)
    if not isinstance(locators, dict):
        raise ValueError("Canonical locators must be an object")
    canonical.documents[work_id] = document
    for key, original in locators.items():
        parts = key.split(":")
        if len(parts) != 3 or any(str(int(value)) != value for value in parts):
            raise ValueError("Canonical locator key is invalid")
        row, raw, normalized = map(int, parts)
        if row < int(anchor) or min(raw, normalized) < 0 or not isinstance(original, str):
            raise ValueError("Canonical locator values are invalid")
        if original not in detail["original"]:
            raise ValueError("Canonical locator original is missing from its full work")
        point_id = str(uuid.uuid5(NAMESPACE, f"{generation}:{dataset}:{row}:{raw}:{normalized}"))
        if point_id in canonical.points:
            raise ValueError("Canonical point UUID is duplicated")
        canonical.points[point_id] = {"dataset": dataset, "generation": generation,
            "work_id": work_id, "source_row_id": row, "raw_index": raw,
            "normalized_index": normalized, "original": original}
        canonical.counts[dataset] += 1
        previous = canonical.anchors.get(work_id)
        canonical.anchors[work_id] = point_id if previous is None else min(previous, point_id)
