//! stdio MCP server (newline-delimited JSON-RPC) in front of one Streamable
//! HTTP upstream. The allowlist only narrows what is exposed.

use std::io::{self, BufRead, Write};
use std::time::{Duration, Instant};

use crate::correlation::{self, Record};
use crate::credential::Secret;
use crate::json::{self, Value};
use crate::upstream::{Reply, Upstream};
use crate::uuid;

pub const SUPPORTED_PROTOCOLS: [&str; 3] = ["2025-06-18", "2025-03-26", "2024-11-05"];

pub struct Config {
    pub upstream_url: String,
    pub allowlist: Vec<String>,
    pub timeout: Duration,
}

fn ok_response(id: Value, result: Value) -> Value {
    Value::obj(vec![
        ("jsonrpc", Value::text("2.0")),
        ("id", id),
        ("result", result),
    ])
}

fn error_body(code: i64, message: &str, data: Option<Value>) -> Value {
    let mut pairs = vec![
        ("code", Value::Num(code.to_string())),
        ("message", Value::text(message)),
    ];
    if let Some(d) = data {
        pairs.push(("data", d));
    }
    Value::obj(pairs)
}

fn error_response(id: Value, body: Value) -> Value {
    Value::obj(vec![
        ("jsonrpc", Value::text("2.0")),
        ("id", id),
        ("error", body),
    ])
}

/// An `isError: true` tool result carrying a machine-readable reason code.
fn refusal(reason: &str, detail: &str) -> Value {
    Value::obj(vec![
        (
            "content",
            Value::Arr(vec![Value::obj(vec![
                ("type", Value::text("text")),
                ("text", Value::text(&format!("{reason}: {detail}"))),
            ])]),
        ),
        ("isError", Value::Bool(true)),
        (
            "structuredContent",
            Value::obj(vec![("reason_code", Value::text(reason))]),
        ),
    ])
}

fn write_msg<W: Write>(out: &mut W, msg: &Value) -> io::Result<()> {
    out.write_all(msg.to_compact().as_bytes())?;
    out.write_all(b"\n")?;
    out.flush()
}

/// Serve the stdio session until the host closes input.
pub fn run<R: BufRead, W: Write>(
    cfg: &Config,
    token: Option<Secret>,
    mut input: R,
    out: &mut W,
) -> io::Result<()> {
    let mut upstream = Upstream::new(&cfg.upstream_url, token, cfg.timeout);
    let mut line = String::new();
    loop {
        line.clear();
        if input.read_line(&mut line)? == 0 {
            return Ok(());
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let msg = match json::parse(trimmed) {
            Ok(m) => m,
            Err(e) => {
                let body = error_body(-32700, &format!("parse error: {e}"), None);
                write_msg(out, &error_response(Value::Null, body))?;
                continue;
            }
        };
        if let Some(resp) = handle(cfg, &mut upstream, &msg) {
            write_msg(out, &resp)?;
        }
    }
}

fn handle(cfg: &Config, upstream: &mut Upstream, msg: &Value) -> Option<Value> {
    // Messages without an id are notifications: never answered.
    let id = msg.get("id").cloned()?;
    let method = msg.get("method").and_then(Value::as_str).unwrap_or("");
    let params = msg.get("params");
    let resp = match method {
        "initialize" => {
            let requested = params
                .and_then(|p| p.get("protocolVersion"))
                .and_then(Value::as_str)
                .unwrap_or("");
            let version = if SUPPORTED_PROTOCOLS.contains(&requested) {
                requested
            } else {
                SUPPORTED_PROTOCOLS[0]
            };
            ok_response(
                id,
                Value::obj(vec![
                    ("protocolVersion", Value::text(version)),
                    ("capabilities", Value::obj(vec![("tools", Value::obj(vec![]))])),
                    (
                        "serverInfo",
                        Value::obj(vec![
                            ("name", Value::text("mctl-runtime")),
                            ("version", Value::text(crate::VERSION)),
                        ]),
                    ),
                ]),
            )
        }
        "ping" => ok_response(id, Value::obj(vec![])),
        "tools/list" => handle_list(cfg, upstream, id),
        "tools/call" => handle_call(cfg, upstream, id, params),
        "" => error_response(id, error_body(-32600, "invalid request", None)),
        other => error_response(
            id,
            error_body(-32601, &format!("method not found: {other}"), None),
        ),
    };
    Some(resp)
}

fn is_allowed(cfg: &Config, name: &str) -> bool {
    cfg.allowlist.iter().any(|a| a == name)
}

fn handle_list(cfg: &Config, upstream: &mut Upstream, id: Value) -> Value {
    match upstream.list_tools() {
        Ok(tools) => {
            let kept: Vec<Value> = tools
                .into_iter()
                .filter(|t| {
                    t.get("name")
                        .and_then(Value::as_str)
                        .is_some_and(|n| is_allowed(cfg, n))
                })
                .collect();
            ok_response(id, Value::obj(vec![("tools", Value::Arr(kept))]))
        }
        Err(e) => {
            let data = Value::obj(vec![("reason_code", Value::text(e.reason_code()))]);
            let message = format!("{}: {}", e.reason_code(), e.detail());
            error_response(id, error_body(-32000, &message, Some(data)))
        }
    }
}

fn handle_call(cfg: &Config, upstream: &mut Upstream, id: Value, params: Option<&Value>) -> Value {
    let start = Instant::now();
    let Some(name) = params.and_then(|p| p.get("name")).and_then(Value::as_str) else {
        return error_response(
            id,
            error_body(-32602, "tools/call requires params.name", None),
        );
    };
    let empty = Value::obj(vec![]);
    let args = params.and_then(|p| p.get("arguments")).unwrap_or(&empty);
    let invocation_id = uuid::v4();

    let (resp, outcome, is_error) = if !is_allowed(cfg, name) {
        // Fail closed: no upstream I/O at all, not even a lazy initialize.
        (
            ok_response(
                id.clone(),
                refusal("not-eligible", &format!("{name} is not allowlisted")),
            ),
            "not-eligible",
            true,
        )
    } else {
        match upstream.call_tool(name, args, &invocation_id) {
            Ok(Reply::Result(r)) => {
                let failed = matches!(r.get("isError"), Some(Value::Bool(true)));
                let outcome = if failed { "provider-error" } else { "ok" };
                (ok_response(id.clone(), r), outcome, failed)
            }
            Ok(Reply::Error(e)) => (error_response(id.clone(), e), "provider-error", true),
            Err(e) => {
                let code = e.reason_code();
                (
                    ok_response(id.clone(), refusal(code, &e.detail())),
                    code,
                    true,
                )
            }
        }
    };

    Record {
        invocation_id,
        host_request_id: id,
        tool: name.to_string(),
        args_sha256: correlation::args_sha256(args),
        upstream_session_id: upstream.session_id().map(str::to_string),
        upstream_request_id: upstream.last_request_id().map(str::to_string),
        outcome,
        is_error,
        latency_ms: start.elapsed().as_millis(),
    }
    .emit();
    resp
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn cfg() -> Config {
        Config {
            upstream_url: "http://127.0.0.1:9/mcp".to_string(),
            allowlist: vec!["allowed".to_string()],
            timeout: Duration::from_millis(100),
        }
    }

    fn session(input: &str) -> Vec<Value> {
        let mut out = Vec::new();
        run(&cfg(), None, Cursor::new(input.as_bytes().to_vec()), &mut out).unwrap();
        String::from_utf8(out)
            .unwrap()
            .lines()
            .map(|l| json::parse(l).unwrap())
            .collect()
    }

    #[test]
    fn ping_initialize_and_unknown_method() {
        let replies = session(concat!(
            "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\"}}\n",
            "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n",
            "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"ping\"}\n",
            "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"nope\"}\n",
            "not json\n",
        ));
        assert_eq!(replies.len(), 4);
        let version = replies[0].get("result").unwrap().get("protocolVersion");
        assert_eq!(version.unwrap().as_str(), Some("2024-11-05"));
        assert_eq!(replies[1].get("result").unwrap().to_compact(), "{}");
        let code = replies[2].get("error").unwrap().get("code").unwrap();
        assert_eq!(code.to_compact(), "-32601");
        let code = replies[3].get("error").unwrap().get("code").unwrap();
        assert_eq!(code.to_compact(), "-32700");
    }

    #[test]
    fn non_allowlisted_call_is_not_eligible() {
        let replies = session(
            "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"secret_tool\"}}\n",
        );
        let result = replies[0].get("result").unwrap();
        assert_eq!(result.get("isError").unwrap().as_bool(), Some(true));
        let reason = result.get("structuredContent").unwrap().get("reason_code");
        assert_eq!(reason.unwrap().as_str(), Some("not-eligible"));
    }
}
