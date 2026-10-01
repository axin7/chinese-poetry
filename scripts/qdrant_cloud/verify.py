"""Compare every point and bounded stored-vector searches without model calls."""

from collections import Counter
import hashlib
from itertools import zip_longest
import math
import struct

from contract import (
    COUNTS, DIMENSION, FILTER, PATH, TOTAL, points, update_fingerprint,
    validate_config, validate_counts, validate_indexes,
)


def ready(client):
    info = client.request("GET", PATH)
    validate_config(info)
    validate_indexes(info)
    return (info.get("status") == "green" and info.get("optimizer_status") == "ok"
            and info.get("points_count") == TOTAL
            and info.get("indexed_vectors_count") == TOTAL)


def float32(vector):
    return struct.pack(f"<{DIMENSION}f", *vector)


def vector_drift(source, target):
    source_norm = math.sqrt(sum(value * value for value in source))
    target_norm = math.sqrt(sum(value * value for value in target))
    delta = max(abs(left - right) for left, right in zip(source, target))
    normalized_delta = max(abs(left / source_norm - right)
                           for left, right in zip(source, target)) if source_norm else math.inf
    candidate = (delta <= 1e-6 and abs(target_norm - 1) <= 1e-6
                 and normalized_delta <= 1e-7)
    return {"max_absolute_delta": delta, "normalized_delta": normalized_delta,
            "source_norm": source_norm, "target_norm": target_norm,
            "normalization_candidate": candidate}


def compare_points(source, target, batch_size, samples_per_dataset):
    digests = [hashlib.sha256(), hashlib.sha256()]
    metadata = [hashlib.sha256(), hashlib.sha256()]
    counts, samples, mismatches = Counter(), [], []
    sample_counts, changed, max_delta = Counter(), 0, 0
    normalization_candidates = 0
    pairs = zip_longest(points(source, batch_size), points(target, batch_size))
    for left, right in pairs:
        if left is None or right is None or left["id"] != right["id"]:
            raise ValueError("Source and target ordered point IDs differ")
        for index, point in enumerate((left, right)):
            update_fingerprint(digests[index], point)
            update_fingerprint(metadata[index], point, vector=False)
        if left["payload"] != right["payload"]:
            raise ValueError("Source and target payloads differ")
        dataset = left["payload"]["dataset"]
        counts[dataset] += 1
        if sample_counts[dataset] < samples_per_dataset:
            samples.append(left)
            sample_counts[dataset] += 1
        if float32(left["vector"]) != float32(right["vector"]):
            drift = vector_drift(left["vector"], right["vector"])
            changed += 1
            max_delta = max(max_delta, drift["max_absolute_delta"])
            normalization_candidates += int(drift["normalization_candidate"])
            if len(mismatches) < 12:
                mismatches.append({"id": left["id"], **drift})
    if dict(counts) != COUNTS:
        raise ValueError("Full ordered comparison dataset counts differ")
    result = {"counts": dict(counts), "total": sum(counts.values()),
              "source_sha256": digests[0].hexdigest(), "target_sha256": digests[1].hexdigest(),
              "source_metadata_sha256": metadata[0].hexdigest(),
              "target_metadata_sha256": metadata[1].hexdigest(),
              "changed_float32_vectors": changed, "max_absolute_delta": max_delta,
              "normalization_candidates": normalization_candidates, "drift_samples": mismatches}
    return result, samples


def search(client, vector, dataset=None):
    query_filter = {"must": list(FILTER["must"])}
    if dataset:
        query_filter["must"].append({"key": "dataset", "match": {"value": dataset}})
    request = {"query": vector, "filter": query_filter, "limit": 1,
               "with_payload": True, "with_vector": False,
               "params": {"hnsw_ef": 64, "exact": False, "indexed_only": True,
                          "quantization": {"rescore": False}}}
    result = client.request("POST", PATH + "/points/query", request)["points"]
    if len(result) != 1:
        raise ValueError("Expected exactly one indexed search result")
    hit = result[0]
    if (hit["payload"].get("generation") != FILTER["must"][0]["match"]["value"]
            or hit["payload"].get("dataset") not in COUNTS
            or (dataset and hit["payload"].get("dataset") != dataset)):
        raise ValueError("Search returned a result outside the requested generation or dataset")
    return hit


def duplicate_tie(source, left_id, right_id):
    records = source.request("POST", PATH + "/points", {
        "ids": [left_id, right_id], "with_vector": True, "with_payload": True,
    })
    if len(records) != 2:
        return False
    return float32(records[0]["vector"]) == float32(records[1]["vector"])


def compare_searches(source, target, samples):
    rows, changed, ties = [], 0, 0
    for point in samples:
        for dataset in (None, point["payload"]["dataset"]):
            left = search(source, point["vector"], dataset)
            right = search(target, point["vector"], dataset)
            same = left["id"] == right["id"]
            tie = not same and duplicate_tie(source, left["id"], right["id"])
            changed += int(not same and not tie)
            ties += int(tie)
            rows.append({"query_point_id": point["id"], "dataset_filter": dataset,
                         "source_hit_id": left["id"], "target_hit_id": right["id"],
                         "source_score": left["score"], "target_score": right["score"],
                         "source_payload": left["payload"], "target_payload": right["payload"],
                         "same_top1": same, "identical_vector_tie": tie})
    return {"queries_per_endpoint": len(rows), "changed_top1": changed,
            "identical_vector_ties": ties, "rows": rows}


def verify(source, target, batch_size, samples_per_dataset, baseline=None,
           allow_normalization_drift=False):
    if not ready(source) or not ready(target):
        raise ValueError("Both indexes must be healthy and fully indexed before verification")
    result = {"source_counts": validate_counts(source), "target_counts": validate_counts(target)}
    vectors, samples = compare_points(source, target, batch_size, samples_per_dataset)
    result["vectors"] = vectors
    baseline_matches = baseline is None or baseline["sha256"] == vectors["source_sha256"]
    drift_accepted = (allow_normalization_drift
                      and vectors["normalization_candidates"] == vectors["changed_float32_vectors"])
    result["normalization_drift_explicitly_allowed"] = allow_normalization_drift
    result["baseline_matches"] = baseline_matches
    result["search"] = compare_searches(source, target, samples)
    result["success"] = (baseline_matches and result["search"]["changed_top1"] == 0
                         and vectors["source_metadata_sha256"] == vectors["target_metadata_sha256"]
                         and (vectors["changed_float32_vectors"] == 0 or drift_accepted))
    result["model_calls"] = 0
    return result
