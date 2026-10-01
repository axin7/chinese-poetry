use serde::{de::DeserializeOwned, Deserialize};
use serde_json::{json, Value};
use worker::Env;

use crate::error::{AppResult, Fault};
use crate::http_client::{call, call_service};
use crate::model::{
    normalize, validate_work_id, Locator, OriginalPayload, Search, SearchPayload, WorkPayload,
};

#[derive(Deserialize)]
struct Embeddings {
    data: Vec<Embedding>,
}

#[derive(Deserialize)]
struct Embedding {
    embedding: Vec<f32>,
}

#[derive(Deserialize)]
struct Points {
    result: PointResult,
}

#[derive(Deserialize)]
struct PointResult {
    points: Vec<Hit>,
}

#[derive(Deserialize)]
pub struct Hit {
    pub score: f32,
    pub payload: SearchPayload,
}

pub fn setting(env: &Env, key: &str) -> AppResult<String> {
    Ok(env.var(key)?.to_string())
}

pub fn secret(env: &Env, key: &str) -> AppResult<String> {
    Ok(env
        .secret(key)
        .map_err(|_| Fault::unavailable("required credential missing"))?
        .to_string())
}

async fn qdrant<T: DeserializeOwned>(env: &Env, url: &str, body: Option<Value>) -> AppResult<T> {
    let service = env
        .service("QDRANT_GATEWAY")
        .map_err(|_| Fault::unavailable("vector store service binding missing"))?;
    call_service(service, url, &secret(env, "QDRANT_API_KEY")?, body).await
}

pub async fn vector(env: &Env, request: &Search) -> AppResult<Vec<f32>> {
    if let Some(vector) = &request.vector {
        return Ok(vector.clone());
    }
    let response: Embeddings = call(
        "https://api.siliconflow.cn/v1/embeddings",
        "Authorization",
        &format!("Bearer {}", secret(env, "SILICONFLOW_API_KEY")?),
        Some(json!({"model": setting(env, "EMBEDDING_MODEL")?,
            "input": request.query, "encoding_format": "float"})),
        8,
    )
    .await?;
    if response.data.len() != 1 {
        return Err(Fault::unavailable("invalid embedding response"));
    }
    let mut vector = response.data.into_iter().next().unwrap().embedding;
    normalize(&mut vector).map_err(|_| Fault::unavailable("invalid embedding vector"))?;
    Ok(vector)
}

pub async fn search(env: &Env, request: &Search, vector: Vec<f32>) -> AppResult<Hit> {
    let generation = setting(env, "GENERATION")?;
    let tables = request.selected_tables()?;
    let body = json!({
        "query": vector, "limit": 1, "with_vector": false,
        "with_payload": ["dataset", "source_row_id", "raw_index", "normalized_index",
            "work_id", "generation"],
        "params": {"hnsw_ef": 64, "exact": false, "indexed_only": true,
            "quantization": {"rescore": false}},
        "filter": {"must": [
            {"key": "generation", "match": {"value": generation}},
            {"key": "dataset", "match": {"any": tables}},
        ]},
    });
    let base = secret(env, "QDRANT_REST_URL")?;
    let collection = setting(env, "COLLECTION_NAME")?;
    let url = format!(
        "{}/collections/{collection}/points/query",
        base.trim_end_matches('/')
    );
    let response: Points = qdrant(env, &url, Some(body)).await?;
    let hit = response
        .result
        .points
        .into_iter()
        .next()
        .ok_or_else(|| Fault::missing("no matching poems"))?;
    if !hit.score.is_finite() {
        return Err(Fault::unavailable("invalid search score"));
    }
    hit.payload.validate(&generation)?;
    if !tables.contains(&hit.payload.locator.dataset.as_str()) {
        return Err(Fault::unavailable("dataset filter mismatch"));
    }
    Ok(hit)
}

pub async fn health(env: &Env) -> AppResult<()> {
    let base = secret(env, "QDRANT_REST_URL")?;
    let collection = setting(env, "COLLECTION_NAME")?;
    let url = format!("{}/collections/{collection}", base.trim_end_matches('/'));
    let response: Value = qdrant(env, &url, None).await?;
    crate::readiness::validate(&response["result"])?;
    let count_url = format!("{url}/points/count");
    let count: Value = qdrant(
        env,
        &count_url,
        Some(
            json!({"exact": true, "filter": {"must": [{"key": "generation",
            "match": {"value": setting(env, "GENERATION")?}}]}}),
        ),
    )
    .await?;
    if count.pointer("/result/count").and_then(Value::as_u64) != Some(127031) {
        return Err(Fault::unavailable("vector generation mismatch"));
    }
    Ok(())
}

#[derive(Deserialize)]
struct WorkPoints<T> {
    result: WorkResult<T>,
}

#[derive(Deserialize)]
struct WorkResult<T> {
    points: Vec<WorkPoint<T>>,
}

#[derive(Deserialize)]
struct WorkPoint<T> {
    payload: T,
}

pub async fn detail(env: &Env, work_id: &str) -> AppResult<String> {
    let payload: WorkPayload = work_payload(env, work_id, "poetry_work_document").await?;
    payload.document(work_id, &setting(env, "GENERATION")?)
}

pub async fn original(env: &Env, locator: &Locator) -> AppResult<String> {
    let payload: OriginalPayload = work_payload(env, &locator.work_id, "poetry_locators").await?;
    payload.original(locator, &setting(env, "GENERATION")?)
}

async fn work_payload<T: DeserializeOwned>(env: &Env, work_id: &str, field: &str) -> AppResult<T> {
    validate_work_id(work_id)?;
    let generation = setting(env, "GENERATION")?;
    let base = secret(env, "QDRANT_REST_URL")?;
    let collection = setting(env, "API_COLLECTION_NAME")?;
    let url = format!(
        "{}/collections/{collection}/points/scroll",
        base.trim_end_matches('/')
    );
    let body = json!({"limit": 1, "with_vector": false,
    "with_payload": ["work_id", "generation", "poetry_api_schema", field],
    "filter": {"must": [
        {"key": "generation", "match": {"value": generation}},
        {"key": "work_id", "match": {"value": work_id}},
    ]}});
    let response: WorkPoints<T> = qdrant(env, &url, Some(body)).await?;
    let point = response
        .result
        .points
        .into_iter()
        .next()
        .ok_or_else(|| Fault::missing("work does not exist"))?;
    Ok(point.payload)
}
