use std::time::Duration;

use futures_util::{
    future::{select, Either},
    pin_mut,
};
use serde::de::DeserializeOwned;
use serde_json::Value;
use wasm_bindgen::JsValue;
use worker::{
    AbortController, Delay, Fetch, Fetcher, Headers, Method, Request, RequestInit, RequestRedirect,
    Response,
};

use crate::error::{AppResult, Fault};

struct Cancellation(Option<AbortController>);

impl Drop for Cancellation {
    fn drop(&mut self) {
        if let Some(controller) = self.0.take() {
            controller.abort();
        }
    }
}

pub async fn call<T: DeserializeOwned>(
    url: &str,
    header: &str,
    auth: &str,
    body: Option<Value>,
    seconds: u64,
) -> AppResult<T> {
    let request = build_request(url, header, auth, body)?;
    let abort = AbortController::default();
    let signal = abort.signal();
    let _cancellation = Cancellation(Some(abort));
    let operation = async {
        let response = Fetch::Request(request)
            .send_with_signal(&signal)
            .await
            .map_err(|_| fault(header, "connection"))?;
        decode(response, header).await
    };
    let timeout = Delay::from(Duration::from_secs(seconds));
    pin_mut!(operation, timeout);
    match select(operation, timeout).await {
        Either::Left((result, _)) => result,
        Either::Right(_) => Err(fault(header, "timeout")),
    }
}

fn build_request(url: &str, header: &str, auth: &str, body: Option<Value>) -> AppResult<Request> {
    let headers = Headers::new();
    headers.set(header, auth)?;
    headers.set("Content-Type", "application/json")?;
    let mut init = RequestInit::new();
    init.with_headers(headers)
        .with_redirect(RequestRedirect::Manual);
    if let Some(body) = body {
        init.with_method(Method::Post)
            .with_body(Some(JsValue::from_str(&body.to_string())));
    }
    Request::new_with_init(url, &init).map_err(|_| Fault::unavailable("invalid upstream request"))
}

pub async fn call_service<T: DeserializeOwned>(
    service: Fetcher,
    url: &str,
    auth: &str,
    body: Option<Value>,
) -> AppResult<T> {
    let request = build_request(url, "api-key", auth, body)?;
    let operation = async {
        let response = service
            .fetch_request(request)
            .await
            .map_err(|_| fault("api-key", "connection"))?;
        decode(response, "api-key").await
    };
    let timeout = Delay::from(Duration::from_secs(7));
    pin_mut!(operation, timeout);
    match select(operation, timeout).await {
        Either::Left((result, _)) => result,
        Either::Right(_) => Err(fault("api-key", "timeout")),
    }
}

async fn decode<T: DeserializeOwned>(mut response: Response, header: &str) -> AppResult<T> {
    if response.status_code() != 200 {
        return Err(fault(
            header,
            if response.status_code() == 429 {
                "rate"
            } else {
                "reject"
            },
        ));
    }
    let text = response.text().await.map_err(|_| fault(header, "read"))?;
    if text.len() > 2 * 1024 * 1024 {
        return Err(Fault::unavailable("upstream response too large"));
    }
    serde_json::from_str(&text).map_err(|_| Fault::unavailable("invalid upstream response"))
}

fn fault(header: &str, kind: &str) -> Fault {
    let detail = match (header == "Authorization", kind) {
        (true, "connection") => "embedding connection failed",
        (false, "connection") => "vector store connection failed",
        (true, "timeout") => "embedding request timed out",
        (false, "timeout") => "vector store request timed out",
        (true, "rate") => "embedding rate limited",
        (false, "rate") => "vector store rate limited",
        (true, "reject") => "embedding request rejected",
        (false, "reject") => "vector store request rejected",
        _ => "upstream response read failed",
    };
    Fault::unavailable(detail)
}
