use std::time::Duration;

use futures_util::{
    future::{select, Either},
    pin_mut,
};
use serde_json::json;
use worker::{
    js_sys::Uint8Array, AbortController, Delay, Env, Fetch, Headers, Method, Request, RequestInit,
    RequestRedirect, Response,
};

use crate::error::{AppResult, Fault};
use crate::upstream::{secret, setting};

struct Cancellation(Option<AbortController>);

impl Drop for Cancellation {
    fn drop(&mut self) {
        if let Some(controller) = self.0.take() {
            controller.abort();
        }
    }
}

pub async fn handle(request: &mut Request, env: &Env) -> worker::Result<Response> {
    let mut response = match dispatch(request, env).await {
        Ok(response) => response,
        Err(fault) => Response::from_json(&json!({"detail": fault.detail}))?.with_status(503),
    };
    response.headers_mut().set("Cache-Control", "no-store")?;
    Ok(response)
}

async fn dispatch(request: &mut Request, env: &Env) -> AppResult<Response> {
    let url = request.url()?;
    let collection = setting(env, "COLLECTION_NAME")?;
    let api_collection = setting(env, "API_COLLECTION_NAME")?;
    if !allowed(
        request.method(),
        url.path(),
        url.query(),
        &collection,
        &api_collection,
    ) || url.fragment().is_some()
    {
        return Err(Fault::unavailable("gateway operation rejected"));
    }
    let body = if request.method() == Method::Post {
        Some(crate::body(request).await?)
    } else {
        None
    };
    let upstream = build_request(request, env, url.path(), body)?;
    forward(upstream).await
}

fn allowed(
    method: Method,
    path: &str,
    query: Option<&str>,
    collection: &str,
    api_collection: &str,
) -> bool {
    if query.is_some() {
        return false;
    }
    match method {
        Method::Get => path == format!("/collections/{collection}"),
        Method::Post => {
            path == format!("/collections/{collection}/points/count")
                || path == format!("/collections/{collection}/points/query")
                || path == format!("/collections/{api_collection}/points/scroll")
        }
        _ => false,
    }
}

fn build_request(
    request: &Request,
    env: &Env,
    path: &str,
    body: Option<Vec<u8>>,
) -> AppResult<Request> {
    let base = secret(env, "QDRANT_REST_URL")?;
    let headers = Headers::new();
    headers.set("api-key", &secret(env, "QDRANT_API_KEY")?)?;
    if let Some(content_type) = request.headers().get("Content-Type")? {
        headers.set("Content-Type", &content_type)?;
    }
    let mut init = RequestInit::new();
    init.with_method(request.method())
        .with_headers(headers)
        .with_redirect(RequestRedirect::Manual);
    if let Some(body) = body {
        init.with_body(Some(Uint8Array::from(body.as_slice()).into()));
    }
    Ok(Request::new_with_init(
        &format!("{}{path}", base.trim_end_matches('/')),
        &init,
    )?)
}

async fn forward(request: Request) -> AppResult<Response> {
    let controller = AbortController::default();
    let signal = controller.signal();
    let _cancellation = Cancellation(Some(controller));
    let operation = async {
        let mut response = Fetch::Request(request)
            .send_with_signal(&signal)
            .await
            .map_err(|_| Fault::unavailable("vector store connection failed"))?;
        let status = response.status_code();
        let content_type = response.headers().get("Content-Type")?;
        let bytes = response
            .bytes()
            .await
            .map_err(|_| Fault::unavailable("vector store response read failed"))?;
        if bytes.len() > 2 * 1024 * 1024 {
            return Err(Fault::unavailable("vector store response too large"));
        }
        let mut result = if bytes.is_empty() {
            Response::empty()?
        } else {
            Response::from_bytes(bytes)?
        }
        .with_status(status);
        if let Some(content_type) = content_type {
            result.headers_mut().set("Content-Type", &content_type)?;
        }
        Ok(result)
    };
    let timeout = Delay::from(Duration::from_secs(5));
    pin_mut!(operation, timeout);
    match select(operation, timeout).await {
        Either::Left((result, _)) => result,
        Either::Right(_) => Err(Fault::unavailable("vector store request timed out")),
    }
}

#[cfg(test)]
mod tests {
    use super::allowed;
    use worker::Method;

    const VECTOR: &str = "poetry_tang_20260922_v1";
    const API: &str = "poetry_tang_api_20260930_v1";

    #[test]
    fn accepts_only_four_required_read_operations() {
        for (method, path) in [
            (Method::Get, format!("/collections/{VECTOR}")),
            (Method::Post, format!("/collections/{VECTOR}/points/count")),
            (Method::Post, format!("/collections/{VECTOR}/points/query")),
            (Method::Post, format!("/collections/{API}/points/scroll")),
        ] {
            assert!(allowed(method, &path, None, VECTOR, API), "{path}");
        }
    }

    #[test]
    fn rejects_mutations_wrong_methods_and_other_collections() {
        for (method, path) in [
            (Method::Put, format!("/collections/{VECTOR}/points")),
            (Method::Delete, format!("/collections/{VECTOR}")),
            (Method::Post, format!("/collections/{VECTOR}/points/delete")),
            (
                Method::Post,
                format!("/collections/{VECTOR}/points/payload"),
            ),
            (Method::Post, format!("/collections/{VECTOR}/snapshots")),
            (Method::Post, format!("/collections/{VECTOR}")),
            (Method::Get, format!("/collections/{VECTOR}/points/query")),
            (Method::Get, format!("/collections/{API}")),
            (Method::Post, format!("/collections/{API}/points/query")),
            (Method::Post, format!("/collections/{VECTOR}/points/scroll")),
            (Method::Get, "/collections/another".into()),
        ] {
            assert!(!allowed(method, &path, None, VECTOR, API), "{path}");
        }
    }

    #[test]
    fn rejects_query_strings_path_suffixes_and_encoded_paths() {
        let path = format!("/collections/{VECTOR}/points/query");
        for query in ["", "wait=true", "host=evil.example", "timeout=120"] {
            assert!(!allowed(Method::Post, &path, Some(query), VECTOR, API));
        }
        for invalid in [
            format!("{path}/"),
            format!("{path}/../delete"),
            format!("{path}%2F..%2Fdelete"),
            format!("//collections/{VECTOR}/points/query"),
            format!("/collections/{VECTOR}%2Fpoints/query"),
            "https://evil.example/collections/poetry_tang_20260922_v1".into(),
        ] {
            assert!(!allowed(Method::Post, &invalid, None, VECTOR, API));
        }
    }
}
