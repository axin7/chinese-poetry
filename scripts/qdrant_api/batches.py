"""Bounded set-payload batches; no vector or full-payload replacement operations."""

from support import MAX_BATCH_BYTES, encoded


def operation(record):
    return {"set_payload": {"payload": record["additions"], "points": [record["id"]]}}


def batches(records, max_operations=128):
    if not 1 <= max_operations <= 128:
        raise ValueError("Batch size must be between 1 and 128 operations")
    batch, size = [], len(encoded({"operations": []}))
    for record in records:
        item = operation(record)
        item_bytes = len(encoded(item))
        following = size + item_bytes + (2 if batch else 0)
        if batch and (len(batch) == max_operations or following > MAX_BATCH_BYTES):
            yield {"operations": batch}
            batch, size = [], len(encoded({"operations": []}))
        size += item_bytes + (2 if batch else 0)
        batch.append(item)
        if size > MAX_BATCH_BYTES:
            raise ValueError("One additive payload operation exceeds the 1 MiB request limit")
    if batch:
        yield {"operations": batch}
