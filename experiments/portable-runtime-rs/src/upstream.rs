//! Streamable HTTP MCP client (single `/mcp` endpoint, POST only). No GET
//! stream and no legacy SSE transport. `text/event-stream` is accepted only as
//! a response body form of a POST.

use std::time::{Duration, Instant};

use crate::credential::Secret;
use crate::http::{self, HttpError};
use crate::json::{self, Value};

#[derive(Debug)]
pub enum UpstreamError {
    Unavailable(String),
    Timeout,
    CredentialRejected,
    CredentialAbsent,
}

impl UpstreamError {
    /// Reason code surfaced to the host (ADR 017 vocabulary plus two local codes).
    pub fn reason_code(&self) -> &'static str {
        match self {
            UpstreamError::Unavailable(_) => "provider-unavailable",
            UpstreamError::Timeout => "provider-timeout",
            UpstreamError::CredentialRejected => "credential-rejected",
            UpstreamError::CredentialAbsent => "credential-absent",
        }
    }

    pub fn detail(&self) -> String {
        match self {
            UpstreamError::Unavailable(m) => m.clone(),
            UpstreamError::Timeout => "upstream did not answer within the bound".to_string(),
            UpstreamError::CredentialRejected => "upstream rejected the credential".to_string(),
            UpstreamError::CredentialAbsent => "no credential available".to_string(),
        }
    }
}

impl From<HttpError> for UpstreamError {
    fn from(e: HttpError) -> Self {
        match e {
            HttpError::Timeout => UpstreamError::Timeout,
            other => UpstreamError::Unavailable(other.to_string()),
        }
    }
}

/// A JSON-RPC reply body from upstream.
#[derive(Debug)]
pub enum Reply {
    Result(Value),
    Error(Value),
}

pub struct Upstream {
    url: String,
    token: Option<Secret>,
    timeout: Duration,
    session_id: Option<String>,
    initialized: bool,
    next_id: u64,
    last_request_id: Option<String>,
}

/// Data payloads of the events in a `text/event-stream` body.
pub fn sse_events(body: &str) -> Vec<String> {
    let mut events = Vec::new();
    let mut current: Vec<&str> = Vec::new();
    for line in body.lines() {
        if line.is_empty() {
            if !current.is_empty() {
                events.push(current.join("\n"));
                current.clear();
            }
        } else if let Some(data) = line.strip_prefix("data:") {
            current.push(data.strip_prefix(' ').unwrap_or(data));
        }
    }
    if !current.is_empty() {
        events.push(current.join("\n"));
    }
    events
}

/// First event whose JSON-RPC `id` equals `id`.
pub fn find_sse_reply(body: &str, id: u64) -> Option<Value> {
    let wanted = id.to_string();
    for event in sse_events(body) {
        if let Ok(v) = json::parse(&event) {
            let matches = matches!(v.get("id"), Some(Value::Num(t)) if *t == wanted);
            if matches {
                return Some(v);
            }
        }
    }
    None
}

impl Upstream {
    pub fn new(url: &str, token: Option<Secret>, timeout: Duration) -> Upstream {
        Upstream {
            url: url.to_string(),
            token,
            timeout,
            session_id: None,
            initialized: false,
            next_id: 0,
            last_request_id: None,
        }
    }

    pub fn session_id(&self) -> Option<&str> {
        self.session_id.as_deref()
    }

    pub fn last_request_id(&self) -> Option<&str> {
        self.last_request_id.as_deref()
    }

    /// All tools reported by upstream (follows `nextCursor`, at most 10 pages).
    pub fn list_tools(&mut self) -> Result<Vec<Value>, UpstreamError> {
        let deadline = Instant::now() + self.timeout;
        self.ensure_session(deadline)?;
        let mut tools = Vec::new();
        let mut cursor: Option<String> = None;
        for _ in 0..10 {
            let params = match &cursor {
                Some(c) => Value::obj(vec![("cursor", Value::text(c))]),
                None => Value::obj(vec![]),
            };
            match self.rpc("tools/list", params, None, deadline)? {
                Reply::Error(e) => {
                    return Err(UpstreamError::Unavailable(format!(
                        "upstream tools/list error: {}",
                        e.to_compact()
                    )))
                }
                Reply::Result(r) => {
                    if let Some(arr) = r.get("tools").and_then(Value::as_array) {
                        tools.extend(arr.iter().cloned());
                    }
                    cursor = r
                        .get("nextCursor")
                        .and_then(Value::as_str)
                        .map(str::to_string);
                    if cursor.is_none() {
                        break;
                    }
                }
            }
        }
        Ok(tools)
    }

    /// Forward one `tools/call`, tagging the request with `invocation_id`.
    pub fn call_tool(
        &mut self,
        name: &str,
        arguments: &Value,
        invocation_id: &str,
    ) -> Result<Reply, UpstreamError> {
        let deadline = Instant::now() + self.timeout;
        self.ensure_session(deadline)?;
        self.last_request_id = None;
        let params = Value::obj(vec![
            ("name", Value::text(name)),
            ("arguments", arguments.clone()),
        ]);
        self.rpc("tools/call", params, Some(invocation_id), deadline)
    }

    fn ensure_session(&mut self, deadline: Instant) -> Result<(), UpstreamError> {
        if self.token.is_none() {
            return Err(UpstreamError::CredentialAbsent);
        }
        if self.initialized {
            return Ok(());
        }
        self.session_id = None;
        let params = Value::obj(vec![
            ("protocolVersion", Value::text("2025-06-18")),
            ("capabilities", Value::obj(vec![])),
            (
                "clientInfo",
                Value::obj(vec![
                    ("name", Value::text("mctl-runtime")),
                    ("version", Value::text(crate::VERSION)),
                ]),
            ),
        ]);
        if let Reply::Error(e) = self.rpc("initialize", params, None, deadline)? {
            return Err(UpstreamError::Unavailable(format!(
                "upstream initialize error: {}",
                e.to_compact()
            )));
        }
        let note = Value::obj(vec![
            ("jsonrpc", Value::text("2.0")),
            ("method", Value::text("notifications/initialized")),
        ])
        .to_compact();
        self.post(&note, None, deadline)?;
        self.initialized = true;
        Ok(())
    }

    fn post(
        &mut self,
        body: &str,
        invocation_id: Option<&str>,
        deadline: Instant,
    ) -> Result<http::Response, UpstreamError> {
        let url = http::parse_url(&self.url)?;
        let token = self
            .token
            .as_ref()
            .ok_or(UpstreamError::CredentialAbsent)?;
        let mut headers = vec![
            ("Content-Type".to_string(), "application/json".to_string()),
            (
                "Accept".to_string(),
                "application/json, text/event-stream".to_string(),
            ),
            (
                "Authorization".to_string(),
                format!("Bearer {}", token.expose()),
            ),
        ];
        if let Some(s) = &self.session_id {
            headers.push(("Mcp-Session-Id".to_string(), s.clone()));
        }
        if let Some(i) = invocation_id {
            headers.push(("X-Mctl-Invocation-Id".to_string(), i.to_string()));
        }
        let resp = http::post(&url, &headers, body.as_bytes(), deadline)?;
        if let Some(rid) = resp.header("x-request-id") {
            self.last_request_id = Some(rid.to_string());
        }
        if self.session_id.is_none() {
            if let Some(sid) = resp.header("mcp-session-id") {
                self.session_id = Some(sid.to_string());
            }
        }
        match resp.status {
            401 | 403 => Err(UpstreamError::CredentialRejected),
            404 if self.session_id.is_some() => {
                self.initialized = false;
                Err(UpstreamError::Unavailable(
                    "upstream session expired (HTTP 404)".to_string(),
                ))
            }
            200..=299 => Ok(resp),
            other => Err(UpstreamError::Unavailable(format!(
                "upstream HTTP status {other}"
            ))),
        }
    }

    fn rpc(
        &mut self,
        method: &str,
        params: Value,
        invocation_id: Option<&str>,
        deadline: Instant,
    ) -> Result<Reply, UpstreamError> {
        self.next_id += 1;
        let id = self.next_id;
        let body = Value::obj(vec![
            ("jsonrpc", Value::text("2.0")),
            ("id", Value::num(id)),
            ("method", Value::text(method)),
            ("params", params),
        ])
        .to_compact();
        let resp = self.post(&body, invocation_id, deadline)?;
        let content_type = resp
            .header("content-type")
            .unwrap_or("")
            .to_ascii_lowercase();
        let text = String::from_utf8_lossy(&resp.body).into_owned();
        let msg = if content_type.contains("text/event-stream") {
            find_sse_reply(&text, id).ok_or_else(|| {
                UpstreamError::Unavailable("no matching event in event-stream".to_string())
            })?
        } else {
            json::parse(&text).map_err(|e| {
                UpstreamError::Unavailable(format!("invalid JSON from upstream: {e}"))
            })?
        };
        if let Some(r) = msg.get("result") {
            return Ok(Reply::Result(r.clone()));
        }
        if let Some(e) = msg.get("error") {
            return Ok(Reply::Error(e.clone()));
        }
        Err(UpstreamError::Unavailable(
            "response has neither result nor error".to_string(),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_stream_data_extraction() {
        let body = ": comment\nevent: message\ndata: {\"id\":1}\n\nevent: message\ndata: {\"jsonrpc\":\"2.0\",\n data: x\n\n";
        let events = sse_events(body);
        assert_eq!(events.len(), 2);
        assert_eq!(events[0], "{\"id\":1}");
    }

    #[test]
    fn finds_matching_id() {
        let body = "data: {\"id\":1,\"result\":{}}\n\ndata: {\"id\":2,\"result\":{\"ok\":true}}\n\n";
        let v = find_sse_reply(body, 2).unwrap();
        assert_eq!(v.get("result").unwrap().to_compact(), "{\"ok\":true}");
        assert!(find_sse_reply(body, 3).is_none());
    }

    #[test]
    fn https_is_provider_unavailable_without_io() {
        let mut up = Upstream::new(
            "https://api.mctl.ai/mcp",
            Some(Secret::new("t".to_string())),
            Duration::from_millis(100),
        );
        let err = up.list_tools().unwrap_err();
        assert_eq!(err.reason_code(), "provider-unavailable");
    }

    #[test]
    fn missing_token_makes_no_request() {
        let mut up = Upstream::new("http://127.0.0.1:9/mcp", None, Duration::from_millis(100));
        let err = up
            .call_tool("x", &Value::obj(vec![]), "inv")
            .unwrap_err();
        assert_eq!(err.reason_code(), "credential-absent");
    }
}
