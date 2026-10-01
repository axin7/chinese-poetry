use serde_json::json;

use crate::model::{
    normalize, validate_work_id, OriginalPayload, Search, SearchPayload, WorkPayload, API_SCHEMA,
    DIMENSION, PROFILE,
};

#[test]
fn rejects_null_unknown_fields_and_wrong_profile() {
    for payload in [
        json!({"query": null}),
        json!({"vector": null}),
        json!({"query": "moon", "unexpected": 1}),
        json!({"query": "moon", "embedding_profile": "other"}),
        json!({"query": "moon", "filters": {"tables": []}}),
    ] {
        assert!(Search::decode(&serde_json::to_vec(&payload).unwrap()).is_err());
    }
}

#[test]
fn preserves_vector_cache_identity_with_irrelevant_text() {
    let mut vector = vec![0.0; DIMENSION];
    vector[0] = 3.0;
    vector[1] = 4.0;
    let make = |query: &str| {
        Search::decode(
            &serde_json::to_vec(&json!({
                "query": query, "vector": vector, "embedding_profile": PROFILE,
                "filters": {"tables": ["tangshisanbaishou", "tangshisanbaishou"]},
            }))
            .unwrap(),
        )
        .unwrap()
    };
    let left = make("moon");
    let right = make("river");
    assert_eq!(left.identity(), right.identity());
    assert_eq!(left.selected_tables().unwrap(), vec!["tangshisanbaishou"]);
    assert_eq!(left.vector.unwrap()[0], 0.6);
}

#[test]
fn normalization_handles_large_and_subnormal_vectors() {
    for scale in [f32::MAX, f32::MIN_POSITIVE / 10.0] {
        let mut vector = vec![0.0; DIMENSION];
        vector[0] = scale;
        vector[1] = scale;
        normalize(&mut vector).unwrap();
        let norm: f64 = vector.iter().map(|value| f64::from(*value).powi(2)).sum();
        assert!((norm - 1.0).abs() < 1e-6);
    }
    assert!(normalize(&mut vec![0.0; DIMENSION]).is_err());
    assert!(normalize(&mut vec![f32::NAN; DIMENSION]).is_err());
}

#[test]
fn distinguishes_excluded_filters_and_invalid_work_ids() {
    let excluded = Search::decode(br#"{"query":"moon","filters":{"tables":["songci"]}}"#).unwrap();
    assert_eq!(excluded.selected_tables().unwrap_err().status, 404);
    for value in [
        "songci:1",
        "tangshisanbaishou:01",
        "tangshisanbaishou:0",
        "tangshisanbaishou:1:2",
    ] {
        assert!(validate_work_id(value).is_err());
    }
}

#[test]
fn bearer_auth_requires_exact_token() {
    let token = "a".repeat(64);
    assert!(crate::authorized(&format!("Bearer {token}"), &token));
    for header in [
        token.clone(),
        format!("Bearer {}", "b".repeat(64)),
        format!("bearer {token}"),
        format!("Bearer {token} "),
    ] {
        assert!(!crate::authorized(&header, &token));
    }
}

#[test]
fn readiness_rejects_incomplete_index_and_unhealthy_collection() {
    let ready = json!({
        "points_count": 127031, "indexed_vectors_count": 127031,
        "status": "green", "optimizer_status": "ok",
        "config": {"params": {"vectors": {"size": 1024, "distance": "Cosine"}},
            "optimizer_config": {"indexing_threshold": 20000},
            "hnsw_config": {"full_scan_threshold": 10000}},
        "payload_schema": {"dataset": {"data_type": "keyword"},
            "generation": {"data_type": "keyword"}},
    });
    assert!(crate::readiness::validate(&ready).is_ok());
    let mut incomplete = ready.clone();
    incomplete["indexed_vectors_count"] = json!(120000);
    assert!(crate::readiness::validate(&incomplete).is_err());
    incomplete["indexed_vectors_count"] = json!(125000);
    assert!(crate::readiness::validate(&incomplete).is_ok());
    for (path, replacement) in [
        ("/status", json!("yellow")),
        ("/optimizer_status", json!({"error": "failed"})),
        ("/config/params/vectors/size", json!(512)),
        ("/payload_schema/generation/data_type", json!("text")),
    ] {
        let mut changed = ready.clone();
        *changed.pointer_mut(path).unwrap() = replacement;
        assert!(crate::readiness::validate(&changed).is_err());
    }
}

#[test]
fn search_requires_valid_qdrant_locator() {
    let payload = json!({"dataset": "tangshisanbaishou", "source_row_id": 1,
        "raw_index": 0, "normalized_index": 0, "work_id": "tangshisanbaishou:1",
        "generation": "v1"});
    let decoded: SearchPayload = serde_json::from_value(payload.clone()).unwrap();
    assert!(decoded.validate("v1").is_ok());
    for (field, value) in [("dataset", "other"), ("generation", "old")] {
        let mut invalid = payload.clone();
        invalid[field] = json!(value);
        let decoded: SearchPayload = serde_json::from_value(invalid).unwrap();
        assert!(decoded.validate("v1").is_err());
    }
    assert!(decoded.validate("v2").is_err());
}

#[test]
fn originals_resolve_exact_multirow_locator_and_reject_missing_or_wrong_version() {
    let search: SearchPayload = serde_json::from_value(json!({
        "dataset": "tangshisanbaishou", "source_row_id": 2, "raw_index": 2,
        "normalized_index": 1, "work_id": "tangshisanbaishou:1", "generation": "v1"
    }))
    .unwrap();
    let payload = json!({"work_id": "tangshisanbaishou:1", "generation": "v1",
        "poetry_api_schema": API_SCHEMA,
        "poetry_locators": "{\"1:0:0\":\"first\",\"2:2:1\":\"second\"}"});
    let decode = || serde_json::from_value::<OriginalPayload>(payload.clone()).unwrap();
    assert_eq!(decode().original(&search.locator, "v1").unwrap(), "second");
    assert!(decode().original(&search.locator, "v2").is_err());
    for (field, value) in [
        ("work_id", "tangshisanbaishou:2"),
        ("poetry_api_schema", "old"),
        ("poetry_locators", "{}"),
        ("poetry_locators", "{\"2:2:1\":null}"),
    ] {
        let mut invalid = payload.clone();
        invalid[field] = json!(value);
        let decoded: OriginalPayload = serde_json::from_value(invalid).unwrap();
        assert!(decoded.original(&search.locator, "v1").is_err());
    }
}

#[test]
fn detail_payload_preserves_raw_document_and_rejects_wrong_identity() {
    let document =
        "{\"id\":\"tangshisanbaishou:1\",\"dataset\":\"tangshisanbaishou\",\"title\":\"poem\"}";
    let payload = json!({"work_id": "tangshisanbaishou:1", "generation": "v1",
        "poetry_api_schema": API_SCHEMA, "poetry_work_document": document});
    let decode = || serde_json::from_value::<WorkPayload>(payload.clone()).unwrap();
    assert_eq!(
        decode().document("tangshisanbaishou:1", "v1").unwrap(),
        document
    );
    assert!(decode().document("tangshisanbaishou:2", "v1").is_err());
    assert!(decode().document("tangshisanbaishou:1", "v2").is_err());
}
