use serde_json::Value;

use crate::error::{AppResult, Fault};

pub fn validate(info: &Value) -> AppResult<()> {
    if info["points_count"].as_u64() != Some(127031)
        || info["status"].as_str() != Some("green")
        || info["optimizer_status"].as_str() != Some("ok")
        || info
            .pointer("/config/params/vectors/size")
            .and_then(Value::as_u64)
            != Some(1024)
        || info
            .pointer("/config/params/vectors/distance")
            .and_then(Value::as_str)
            != Some("Cosine")
    {
        return Err(Fault::unavailable("unexpected vector collection"));
    }
    for field in ["dataset", "generation"] {
        if info["payload_schema"][field]["data_type"].as_str() != Some("keyword") {
            return Err(Fault::unavailable("vector payload index unavailable"));
        }
    }
    let indexed = info["indexed_vectors_count"]
        .as_u64()
        .ok_or_else(|| Fault::unavailable("vector index count unavailable"))?;
    let indexing = info
        .pointer("/config/optimizer_config/indexing_threshold")
        .and_then(Value::as_u64)
        .unwrap_or(20000);
    let scan = info
        .pointer("/config/params/vectors/hnsw_config/full_scan_threshold")
        .or_else(|| info.pointer("/config/hnsw_config/full_scan_threshold"))
        .and_then(Value::as_u64)
        .unwrap_or(10000);
    if 127031_u64.saturating_sub(indexed) > indexing.max(scan) * 1024 / (1024 * 4) {
        return Err(Fault::unavailable("vector indexing incomplete"));
    }
    Ok(())
}
