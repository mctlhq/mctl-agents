//! In-process Streamable HTTP `/mcp` server on `127.0.0.1:0`.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use mctl_runtime::json::{self, Value};

pub const SESSION_ID: &str = "fake-session-1";

// Tool definitions exactly as the upstream sends them: compact JSON with a
// deliberately non-alphabetical key order and number text that must survive.
pub const TOOL_WHOAMI: &str = r#"{"name":"mctl_whoami","description":"Who am I","inputSchema":{"type":"object","properties":{"verbose":{"type":"boolean","default":false},"limit":{"type":"integer","maximum":1.50}},"required":[],"additionalProperties":false}}"#;
pub const TOOL_DENY: &str = r#"{"name":"deny_tool","description":"Always denied","inputSchema":{"required":["target"],"type":"object","properties":{"target":{"type":"string"},"dry_run":{"default":true,"type":"boolean"}}}}"#;
pub const TOOL_APPROVAL: &str = r#"{"name":"approval_tool","description":"Needs approval","inputSchema":{"type":"object","properties":{"z":{"type":"array","items":{"type":"number","minimum":0.10}},"a":{"type":"string"}},"required":["a"]}}"#;
pub const TOOL_SLOW: &str = r#"{"name":"slow_tool","description":"Slow","inputSchema":{"type":"object","properties":{},"additionalProperties":true}}"#;
pub const TOOL_SECRET: &str = r#"{"name":"secret_tool","description":"Never allowlisted","inputSchema":{"type":"object","properties":{"k":{"type":"string"}}}}"#;

pub const ALL_TOOLS: [&str; 5] = [TOOL_WHOAMI, TOOL_DENY, TOOL_APPROVAL, TOOL_SLOW, TOOL_SECRET];

pub const DENY_RESULT: &str =
    r#"{"content":[{"type":"text","text":"DENY: policy forbids deny_tool"}],"isError":true}"#;
pub const APPROVAL_RESULT: &str =
    r#"{"content":[{"type":"text","text":"REQUIRE_APPROVAL: approval_id=ap-1"}],"isError":true}"#;

#[derive(Clone, Default)]
pub struct Knobs {
    /// Every request answers 401.
    pub status_401: bool,
    /// Accept the connection, then drop it without answering.
    pub reset: bool,
    /// `mctl_whoami` reports the headers it received.
    pub echo_headers: bool,
    /// Answer with `text/event-stream` (chunked) instead of plain JSON.
    pub sse: bool,
    /// Sleep of `slow_tool` in milliseconds (0 means the 2000 ms default).
    pub slow_ms: u64,
}

#[derive(Clone, Debug)]
pub struct Recorded {
    pub rpc_method: String,
    /// Header names are lower-cased.
    pub headers: Vec<(String, String)>,
}

impl Recorded {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
    }
}

type Recorder = Arc<Mutex<Vec<Recorded>>>;
type Serve = Arc<dyn Fn(TcpStream, &Knobs, &Recorder, &Arc<AtomicU64>) + Send + Sync>;

pub struct FakeUpstream {
    scheme: &'static str,
    addr: SocketAddr,
    recorded: Arc<Mutex<Vec<Recorded>>>,
    stop: Arc<AtomicBool>,
}

impl FakeUpstream {
    pub fn start(knobs: Knobs) -> FakeUpstream {
        let serve: Serve = Arc::new(|mut stream: TcpStream, knobs: &Knobs, recorded: &Recorder, counter: &Arc<AtomicU64>| {
            let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
            handle(&mut stream, knobs, recorded, counter);
        });
        FakeUpstream::spawn("http", knobs, serve)
    }

    /// Same server behind rustls; sends `close_notify` after the response.
    #[cfg(feature = "tls")]
    pub fn start_tls(knobs: Knobs, config: Arc<rustls::ServerConfig>) -> FakeUpstream {
        let serve: Serve = Arc::new(move |tcp: TcpStream, knobs: &Knobs, recorded: &Recorder, counter: &Arc<AtomicU64>| {
            let _ = tcp.set_read_timeout(Some(Duration::from_secs(10)));
            let Ok(conn) = rustls::ServerConnection::new(Arc::clone(&config)) else {
                return;
            };
            let mut tls = rustls::StreamOwned::new(conn, tcp);
            handle(&mut tls, knobs, recorded, counter);
            tls.conn.send_close_notify();
            let _ = tls.flush();
        });
        FakeUpstream::spawn("https", knobs, serve)
    }

    fn spawn(scheme: &'static str, knobs: Knobs, serve: Serve) -> FakeUpstream {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind fake upstream");
        let addr = listener.local_addr().expect("local addr");
        let recorded = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let counter = Arc::new(AtomicU64::new(0));
        {
            let recorded = Arc::clone(&recorded);
            let stop = Arc::clone(&stop);
            std::thread::spawn(move || {
                for conn in listener.incoming() {
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    let Ok(stream) = conn else { continue };
                    let recorded = Arc::clone(&recorded);
                    let counter = Arc::clone(&counter);
                    let knobs = knobs.clone();
                    let serve = Arc::clone(&serve);
                    std::thread::spawn(move || {
                        serve(stream, &knobs, &recorded, &counter);
                    });
                }
            });
        }
        FakeUpstream {
            scheme,
            addr,
            recorded,
            stop,
        }
    }

    pub fn url(&self) -> String {
        format!("{}://{}/mcp", self.scheme, self.addr)
    }

    pub fn recorded(&self) -> Vec<Recorded> {
        self.recorded.lock().expect("lock").clone()
    }

    /// Number of requests whose JSON-RPC method equals `rpc_method`.
    pub fn count(&self, rpc_method: &str) -> usize {
        self.recorded()
            .iter()
            .filter(|r| r.rpc_method == rpc_method)
            .count()
    }

    /// Total number of requests seen.
    pub fn total(&self) -> usize {
        self.recorded().len()
    }

    pub fn last(&self, rpc_method: &str) -> Option<Recorded> {
        self.recorded()
            .into_iter()
            .rev()
            .find(|r| r.rpc_method == rpc_method)
    }
}

impl Drop for FakeUpstream {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        // Wake the accept loop so the thread exits.
        let _ = TcpStream::connect(self.addr);
    }
}

/// A closed local port: bind, take the port, drop the listener.
pub fn closed_port_url() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let addr = listener.local_addr().expect("addr");
    drop(listener);
    format!("http://{addr}/mcp")
}

/// Request headers and body as read off the socket.
type Request = (Vec<(String, String)>, Vec<u8>);

fn read_request<R: BufRead>(reader: &mut R) -> Option<Request> {
    let mut request_line = String::new();
    reader.read_line(&mut request_line).ok()?;
    if request_line.trim().is_empty() {
        return None;
    }
    let mut headers = Vec::new();
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).ok()?;
        let line = line.trim_end();
        if line.is_empty() {
            break;
        }
        let (k, v) = line.split_once(':')?;
        headers.push((k.trim().to_ascii_lowercase(), v.trim().to_string()));
    }
    let len = headers
        .iter()
        .find(|(k, _)| k == "content-length")
        .and_then(|(_, v)| v.parse::<usize>().ok())
        .unwrap_or(0);
    let mut body = vec![0u8; len];
    reader.read_exact(&mut body).ok()?;
    Some((headers, body))
}

fn write_response<W: Write>(
    stream: &mut W,
    status: &str,
    content_type: &str,
    extra: &[(String, String)],
    body: &str,
    chunked: bool,
) {
    let mut head = format!("HTTP/1.1 {status}\r\nConnection: close\r\n");
    if !content_type.is_empty() {
        head.push_str(&format!("Content-Type: {content_type}\r\n"));
    }
    for (k, v) in extra {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    let payload = if chunked {
        head.push_str("Transfer-Encoding: chunked\r\n\r\n");
        let bytes = body.as_bytes();
        let split = bytes.len() / 2;
        let mut out = head.into_bytes();
        for part in [&bytes[..split], &bytes[split..]] {
            out.extend_from_slice(format!("{:x}\r\n", part.len()).as_bytes());
            out.extend_from_slice(part);
            out.extend_from_slice(b"\r\n");
        }
        out.extend_from_slice(b"0\r\n\r\n");
        out
    } else {
        head.push_str(&format!("Content-Length: {}\r\n\r\n{body}", body.len()));
        head.into_bytes()
    };
    let _ = stream.write_all(&payload);
    let _ = stream.flush();
}

fn handle<S: Read + Write>(
    stream: &mut S,
    knobs: &Knobs,
    recorded: &Arc<Mutex<Vec<Recorded>>>,
    counter: &Arc<AtomicU64>,
) {
    let request = {
        let mut reader = BufReader::new(&mut *stream);
        read_request(&mut reader)
    };
    let Some((headers, body)) = request else {
        return;
    };
    let text = String::from_utf8_lossy(&body).into_owned();
    let msg = json::parse(&text).unwrap_or(Value::Null);
    let rpc_method = msg
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    recorded.lock().expect("lock").push(Recorded {
        rpc_method: rpc_method.clone(),
        headers: headers.clone(),
    });
    if knobs.reset {
        return; // dropping the stream closes the socket without a response
    }
    if knobs.status_401 {
        write_response(
            stream,
            "401 Unauthorized",
            "application/json",
            &[],
            "{\"error\":\"unauthorized\"}",
            false,
        );
        return;
    }
    let n = counter.fetch_add(1, Ordering::SeqCst) + 1;
    let mut extra = vec![("X-Request-Id".to_string(), format!("req-{n}"))];
    let id = msg.get("id").map(Value::to_compact);
    let params = msg.get("params");

    let result: Option<String> = match rpc_method.as_str() {
        "initialize" => {
            extra.push(("Mcp-Session-Id".to_string(), SESSION_ID.to_string()));
            Some(r#"{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"fake","version":"0"}}"#.to_string())
        }
        "tools/list" => Some(format!("{{\"tools\":[{}]}}", ALL_TOOLS.join(","))),
        "tools/call" => {
            let name = params
                .and_then(|p| p.get("name"))
                .and_then(Value::as_str)
                .unwrap_or("");
            match name {
                "mctl_whoami" => {
                    let detail = if knobs.echo_headers {
                        let h = |k: &str| {
                            headers
                                .iter()
                                .find(|(n, _)| n == k)
                                .map(|(_, v)| v.clone())
                                .unwrap_or_default()
                        };
                        format!(
                            "user: tester; invocation={}; authorization={}",
                            h("x-mctl-invocation-id"),
                            h("authorization")
                        )
                    } else {
                        "user: tester".to_string()
                    };
                    Some(format!(
                        "{{\"content\":[{{\"type\":\"text\",\"text\":\"{detail}\"}}],\"isError\":false}}"
                    ))
                }
                "deny_tool" => Some(DENY_RESULT.to_string()),
                "approval_tool" => Some(APPROVAL_RESULT.to_string()),
                "slow_tool" => {
                    let ms = if knobs.slow_ms == 0 { 2000 } else { knobs.slow_ms };
                    std::thread::sleep(Duration::from_millis(ms));
                    Some(r#"{"content":[{"type":"text","text":"slow done"}],"isError":false}"#.to_string())
                }
                "secret_tool" => Some(
                    r#"{"content":[{"type":"text","text":"secret"}],"isError":false}"#.to_string(),
                ),
                _ => None,
            }
        }
        _ => {
            // Notifications and anything else: accepted, no body.
            write_response(stream, "202 Accepted", "", &extra, "", false);
            return;
        }
    };

    let id = id.unwrap_or_else(|| "null".to_string());
    let reply = match result {
        Some(r) => format!("{{\"jsonrpc\":\"2.0\",\"id\":{id},\"result\":{r}}}"),
        None => format!(
            "{{\"jsonrpc\":\"2.0\",\"id\":{id},\"error\":{{\"code\":-32602,\"message\":\"unknown tool\"}}}}"
        ),
    };
    if knobs.sse {
        let event = format!("event: message\ndata: {reply}\n\n");
        write_response(stream, "200 OK", "text/event-stream", &extra, &event, true);
    } else {
        write_response(stream, "200 OK", "application/json", &extra, &reply, false);
    }
}
