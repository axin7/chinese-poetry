use sha2::{Digest, Sha256};
use worker::{Cache, Env, Headers, Request, Response};

use crate::error::AppResult;
use crate::model::PROFILE;
use crate::upstream::setting;

pub fn key(request: &Request, env: &Env, identity: &str) -> AppResult<String> {
    let namespace = format!(
        "{}\0{PROFILE}\0{}\0{}\0{identity}",
        setting(env, "GENERATION")?,
        setting(env, "EMBEDDING_MODEL")?,
        setting(env, "ORIGIN_ID")?
    );
    let digest = Sha256::digest(namespace.as_bytes());
    let mut url = request.url()?;
    url.set_path(&format!("/__poetry_cache/{digest:x}"));
    url.set_query(None);
    Ok(url.to_string())
}

pub async fn get(key: &str) -> Option<Response> {
    let mut stored = Cache::default().get(key, false).await.ok()??;
    let headers = Headers::new();
    for name in ["X-Poetry-Generation", "X-Poetry-Score"] {
        if let Ok(Some(value)) = stored.headers().get(name) {
            headers.set(name, &value).ok()?;
        }
    }
    headers.set("X-Poetry-Cache", "HIT").ok()?;
    Some(
        Response::ok(stored.text().await.ok()?)
            .ok()?
            .with_headers(headers),
    )
}

pub async fn put(key: &str, response: &mut Response) {
    if let Ok(mut copy) = response.cloned() {
        if copy
            .headers_mut()
            .set("Cache-Control", "public, max-age=3600")
            .is_ok()
        {
            let _ = Cache::default().put(key, copy).await;
        }
    }
}
