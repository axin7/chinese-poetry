mod edge_cache;
mod error;
mod http_client;
mod model;
mod qdrant_gateway;
mod readiness;
mod upstream;

use std::time::Duration;

use futures_util::{
    future::{select, Either},
    pin_mut, StreamExt,
};
use percent_encoding::percent_decode_str;
use serde_json::json;
use worker::{event, Context, Delay, Env, Method, Request, Response};

use error::{AppResult, Fault};

#[event(fetch)]
pub async fn main(mut request: Request, env: Env, _context: Context) -> worker::Result<Response> {
    if env
        .var("QDRANT_GATEWAY_ONLY")
        .is_ok_and(|value| value.to_string() == "true")
    {
        return qdrant_gateway::handle(&mut request, &env).await;
    }
    let response = bounded_dispatch(&mut request, &env).await;
    let mut response = match response {
        Ok(response) => response,
        Err(fault) => {
            Response::from_json(&json!({"detail": fault.detail}))?.with_status(fault.status)
        }
    };
    response
        .headers_mut()
        .set("Content-Type", "application/json; charset=utf-8")?;
    response.headers_mut().set("Cache-Control", "no-store")?;
    response.headers_mut().set(
        "X-Origin-ID",
        &upstream::setting(&env, "ORIGIN_ID").unwrap_or_else(|_| "cf-poetry-rust-20260930".into()),
    )?;
    Ok(response)
}

async fn bounded_dispatch(request: &mut Request, env: &Env) -> AppResult<Response> {
    let operation = dispatch(request, env);
    let timeout = Delay::from(Duration::from_secs(10));
    pin_mut!(operation, timeout);
    match select(operation, timeout).await {
        Either::Left((result, _)) => result,
        Either::Right(_) => Err(Fault::unavailable("request timed out")),
    }
}

async fn dispatch(request: &mut Request, env: &Env) -> AppResult<Response> {
    let path = request.path();
    let search = path == "/poetry/search";
    let health = path == "/poetry/health";
    let detail = path.strip_prefix("/poetry/poems/");
    if !search && !health && detail.is_none() {
        return Err(Fault::missing("route does not exist"));
    }
    let auth = request.headers().get("Authorization")?.unwrap_or_default();
    if !authorized(&auth, &upstream::secret(env, "CLIENT_TOKEN")?) {
        return Err(Fault {
            status: 401,
            detail: "unauthorized",
        });
    }
    let method = if search { Method::Post } else { Method::Get };
    if request.method() != method {
        let mut response = Response::empty()?.with_status(405);
        response.headers_mut().set("Allow", method.as_ref())?;
        return Ok(response);
    }
    if search {
        return handle_search(request, env).await;
    }
    if let Some(work_id) = detail {
        return handle_detail(request, env, work_id).await;
    }
    upstream::health(env).await?;
    upstream::detail(env, "tangshisanbaishou:1").await?;
    Ok(Response::from_json(&json!({"status": "ok",
        "generation": upstream::setting(env, "GENERATION")?}))?)
}

fn authorized(header: &str, token: &str) -> bool {
    let Some(supplied) = header.strip_prefix("Bearer ") else {
        return false;
    };
    if supplied.len() != 64 || token.len() != 64 {
        return false;
    }
    supplied
        .bytes()
        .zip(token.bytes())
        .fold(0_u8, |value, (left, right)| value | (left ^ right))
        == 0
}

async fn body(request: &mut Request) -> AppResult<Vec<u8>> {
    let mut stream = request.stream()?;
    let mut result = Vec::new();
    while let Some(part) = stream.next().await {
        let part = part?;
        if result.len() + part.len() > 65536 {
            return Err(Fault {
                status: 413,
                detail: "request body exceeds 64 KiB",
            });
        }
        result.extend(part);
    }
    Ok(result)
}

async fn handle_search(request: &mut Request, env: &Env) -> AppResult<Response> {
    let query = model::Search::decode(&body(request).await?)?;
    let tables = query.selected_tables()?;
    let identity = format!("{}\0{}", query.identity(), tables.join(","));
    let key = edge_cache::key(request, env, &identity)?;
    if let Some(response) = edge_cache::get(&key).await {
        return Ok(response);
    }
    let vector = upstream::vector(env, &query).await?;
    let hit = upstream::search(env, &query, vector).await?;
    let original = upstream::original(env, &hit.payload.locator).await?;
    let mut response = Response::from_json(&json!({"id": hit.payload.locator.work_id,
        "original": original}))?;
    response.headers_mut().set(
        "X-Poetry-Generation",
        &upstream::setting(env, "GENERATION")?,
    )?;
    response
        .headers_mut()
        .set("X-Poetry-Score", &hit.score.to_string())?;
    finish_miss(&key, response).await
}

async fn handle_detail(request: &Request, env: &Env, encoded: &str) -> AppResult<Response> {
    let work_id = percent_decode_str(encoded)
        .decode_utf8()
        .map_err(|_| Fault::missing("work does not exist"))?;
    model::validate_work_id(&work_id)?;
    let url = request.url()?;
    let generations: Vec<_> = url
        .query_pairs()
        .filter(|(key, _)| key == "generation")
        .map(|(_, value)| value.to_string())
        .collect();
    if !generations.is_empty()
        && (generations.len() != 1 || generations[0] != upstream::setting(env, "GENERATION")?)
    {
        return Err(Fault::missing("corpus generation does not exist"));
    }
    let key = edge_cache::key(request, env, &format!("detail:{work_id}"))?;
    if let Some(response) = edge_cache::get(&key).await {
        return Ok(response);
    }
    let response = Response::ok(upstream::detail(env, &work_id).await?)?;
    finish_miss(&key, response).await
}

async fn finish_miss(key: &str, mut response: Response) -> AppResult<Response> {
    response.headers_mut().set("X-Poetry-Cache", "MISS")?;
    edge_cache::put(key, &mut response).await;
    Ok(response)
}

#[cfg(test)]
mod tests;
