//! Minimal HTTP/1.1 POST client over `TcpStream`. Plain `http://` always;
//! `https://` only when built with the `tls` feature (rustls, see `tls.rs`).
//! One deadline bounds connect, TLS handshake, write and every read.

use std::fmt;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
#[cfg(feature = "tls")]
use std::sync::Arc;
use std::time::{Duration, Instant};

#[derive(Debug)]
pub enum HttpError {
    Unavailable(String),
    Timeout,
    Unsupported(String),
}

impl fmt::Display for HttpError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            HttpError::Unavailable(m) => write!(f, "upstream unavailable: {m}"),
            HttpError::Timeout => write!(f, "upstream did not answer within the bound"),
            HttpError::Unsupported(m) => write!(f, "{m}"),
        }
    }
}

impl std::error::Error for HttpError {}

#[derive(Debug, Clone, PartialEq)]
pub struct Url {
    pub host: String,
    pub port: u16,
    pub path: String,
    /// True for `https://` (only produced by the `tls` build).
    pub tls: bool,
}

#[derive(Debug)]
pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Response {
    /// Case-insensitive header lookup.
    pub fn header(&self, name: &str) -> Option<&str> {
        find_header(&self.headers, name)
    }
}

fn find_header<'a>(headers: &'a [(String, String)], name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case(name))
        .map(|(_, v)| v.as_str())
}

/// Parse an `http://host[:port]/path` URL. `https://` is accepted only with
/// the `tls` feature (default port 443); otherwise it is refused.
pub fn parse_url(url: &str) -> Result<Url, HttpError> {
    #[cfg(feature = "tls")]
    if let Some(rest) = url.strip_prefix("https://") {
        return parse_authority(url, rest, 443, true);
    }
    #[cfg(not(feature = "tls"))]
    if url.starts_with("https://") {
        return Err(HttpError::Unsupported(
            "https upstream requires the tls feature".to_string(),
        ));
    }
    let rest = url
        .strip_prefix("http://")
        .ok_or_else(|| HttpError::Unavailable(format!("unsupported URL scheme: {url}")))?;
    parse_authority(url, rest, 80, false)
}

fn parse_authority(url: &str, rest: &str, default_port: u16, tls: bool) -> Result<Url, HttpError> {
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], rest[i..].to_string()),
        None => (rest, "/".to_string()),
    };
    let (host, port) = match authority.rsplit_once(':') {
        Some((h, p)) => {
            let port = p
                .parse::<u16>()
                .map_err(|_| HttpError::Unavailable(format!("bad port in URL: {url}")))?;
            (h, port)
        }
        None => (authority, default_port),
    };
    if host.is_empty() {
        return Err(HttpError::Unavailable(format!("missing host in URL: {url}")));
    }
    Ok(Url {
        host: host.to_string(),
        port,
        path,
        tls,
    })
}

fn remaining(deadline: Instant) -> Result<Duration, HttpError> {
    let now = Instant::now();
    if now >= deadline {
        Err(HttpError::Timeout)
    } else {
        Ok(deadline - now)
    }
}

fn map_io(e: std::io::Error) -> HttpError {
    match e.kind() {
        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut => HttpError::Timeout,
        _ => HttpError::Unavailable(e.to_string()),
    }
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

/// Decode a chunked body. `Ok(None)` means more bytes are needed.
pub fn decode_chunked(data: &[u8]) -> Result<Option<Vec<u8>>, String> {
    let mut out = Vec::new();
    let mut pos = 0;
    loop {
        let Some(rel) = find(&data[pos..], b"\r\n") else {
            return Ok(None);
        };
        let line = std::str::from_utf8(&data[pos..pos + rel])
            .map_err(|_| "bad chunk size line".to_string())?;
        let size_text = line.split(';').next().unwrap_or("").trim();
        let size = usize::from_str_radix(size_text, 16)
            .map_err(|_| format!("bad chunk size: {size_text}"))?;
        pos += rel + 2;
        if size == 0 {
            // Optional trailers, terminated by an empty line.
            loop {
                let Some(r) = find(&data[pos..], b"\r\n") else {
                    return Ok(None);
                };
                if r == 0 {
                    return Ok(Some(out));
                }
                pos += r + 2;
            }
        }
        if data.len() < pos + size + 2 {
            return Ok(None);
        }
        out.extend_from_slice(&data[pos..pos + size]);
        pos += size + 2;
    }
}

/// Parse a (possibly partial) response. `Ok(None)` means more bytes are needed;
/// `eof` says the peer closed the connection.
pub fn parse_response(buf: &[u8], eof: bool) -> Result<Option<Response>, String> {
    let Some(end) = find(buf, b"\r\n\r\n") else {
        return if eof {
            Err("connection closed before response headers".to_string())
        } else {
            Ok(None)
        };
    };
    let head = std::str::from_utf8(&buf[..end]).map_err(|_| "non-UTF-8 headers".to_string())?;
    let mut lines = head.split("\r\n");
    let status_line = lines.next().unwrap_or("");
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or_else(|| format!("bad status line: {status_line}"))?;
    let headers: Vec<(String, String)> = lines
        .filter_map(|l| l.split_once(':'))
        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
        .collect();
    let body_raw = &buf[end + 4..];
    let chunked = find_header(&headers, "transfer-encoding")
        .is_some_and(|v| v.to_ascii_lowercase().contains("chunked"));
    let content_length = find_header(&headers, "content-length").and_then(|v| v.parse::<usize>().ok());
    let truncated = || "connection closed before the body was complete".to_string();
    let body = if chunked {
        match decode_chunked(body_raw)? {
            Some(b) => b,
            None if eof => return Err(truncated()),
            None => return Ok(None),
        }
    } else if let Some(n) = content_length {
        if body_raw.len() >= n {
            body_raw[..n].to_vec()
        } else if eof {
            return Err(truncated());
        } else {
            return Ok(None);
        }
    } else if status < 200 || status == 202 || status == 204 || status == 304 {
        Vec::new()
    } else if eof {
        body_raw.to_vec()
    } else {
        return Ok(None);
    };
    Ok(Some(Response {
        status,
        headers,
        body,
    }))
}

/// How `post_with` reaches an https upstream. Empty in the default build.
#[derive(Clone, Default)]
pub struct Connector {
    /// Client config for https; `None` builds `tls::client_config()` on use.
    #[cfg(feature = "tls")]
    pub tls: Option<Arc<rustls::ClientConfig>>,
}

impl Connector {
    #[cfg(feature = "tls")]
    pub fn with_config(cfg: Arc<rustls::ClientConfig>) -> Connector {
        Connector { tls: Some(cfg) }
    }
}

/// POST `body` to `url` and read one response, all within `deadline`.
pub fn post(
    url: &Url,
    headers: &[(String, String)],
    body: &[u8],
    deadline: Instant,
) -> Result<Response, HttpError> {
    post_with(url, headers, body, deadline, &Connector::default())
}

/// Like `post`, with an explicit connector (test-injected TLS config).
pub fn post_with(
    url: &Url,
    headers: &[(String, String)],
    body: &[u8],
    deadline: Instant,
    connector: &Connector,
) -> Result<Response, HttpError> {
    let addrs: Vec<_> = (url.host.as_str(), url.port)
        .to_socket_addrs()
        .map_err(|e| HttpError::Unavailable(e.to_string()))?
        .collect();
    let mut last_err = HttpError::Unavailable(format!("no address for {}", url.host));
    let mut stream = None;
    for addr in addrs {
        let budget = remaining(deadline)?;
        match TcpStream::connect_timeout(&addr, budget) {
            Ok(s) => {
                stream = Some(s);
                break;
            }
            Err(e) => last_err = map_io(e),
        }
    }
    let Some(stream) = stream else {
        return Err(last_err);
    };
    let _ = stream.set_nodelay(true);

    if url.tls {
        #[cfg(feature = "tls")]
        {
            return tls_exchange(stream, url, headers, body, deadline, connector);
        }
        #[cfg(not(feature = "tls"))]
        {
            let _ = connector;
            return Err(HttpError::Unsupported(
                "https upstream requires the tls feature".to_string(),
            ));
        }
    }
    let mut io = &stream;
    exchange(&mut io, &stream, url, headers, body, deadline)
}

/// Handshake, then the exchange, over rustls. A handshake failure (untrusted
/// or mismatched certificate) returns before any request byte is written.
#[cfg(feature = "tls")]
fn tls_exchange(
    tcp: TcpStream,
    url: &Url,
    headers: &[(String, String)],
    body: &[u8],
    deadline: Instant,
    connector: &Connector,
) -> Result<Response, HttpError> {
    let cfg = match &connector.tls {
        Some(c) => Arc::clone(c),
        None => crate::tls::client_config().map_err(|e| HttpError::Unavailable(format!("tls: {e}")))?,
    };
    let name = rustls::pki_types::ServerName::try_from(url.host.clone())
        .map_err(|e| HttpError::Unavailable(format!("tls: bad server name: {e:?}")))?;
    let mut conn = rustls::ClientConnection::new(cfg, name)
        .map_err(|e| HttpError::Unavailable(format!("tls: {e}")))?;
    // The same socket, kept to set timeouts while the stream owns `tcp`.
    let ctl = tcp
        .try_clone()
        .map_err(|e| HttpError::Unavailable(e.to_string()))?;
    let mut sock = tcp;
    while conn.is_handshaking() {
        let budget = remaining(deadline)?;
        ctl.set_read_timeout(Some(budget)).map_err(map_io)?;
        ctl.set_write_timeout(Some(budget)).map_err(map_io)?;
        conn.complete_io(&mut sock).map_err(map_tls_io)?;
    }
    let mut stream = rustls::StreamOwned::new(conn, sock);
    exchange(&mut stream, &ctl, url, headers, body, deadline)
}

#[cfg(feature = "tls")]
fn map_tls_io(e: std::io::Error) -> HttpError {
    match e.kind() {
        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut => HttpError::Timeout,
        _ => HttpError::Unavailable(format!("tls: {e}")),
    }
}

/// Write the request and read one framed response. `ctl` is the socket under
/// `stream`; its timeouts are set from `deadline` before each I/O.
fn exchange<S: Read + Write>(
    stream: &mut S,
    ctl: &TcpStream,
    url: &Url,
    headers: &[(String, String)],
    body: &[u8],
    deadline: Instant,
) -> Result<Response, HttpError> {
    let mut head = format!(
        "POST {} HTTP/1.1\r\nHost: {}:{}\r\nContent-Length: {}\r\nConnection: close\r\n",
        url.path,
        url.host,
        url.port,
        body.len()
    );
    for (k, v) in headers {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    head.push_str("\r\n");
    ctl.set_write_timeout(Some(remaining(deadline)?))
        .map_err(map_io)?;
    stream.write_all(head.as_bytes()).map_err(map_io)?;
    stream.write_all(body).map_err(map_io)?;
    stream.flush().map_err(map_io)?;

    let mut buf = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        if let Some(resp) = parse_response(&buf, false).map_err(HttpError::Unavailable)? {
            return Ok(resp);
        }
        ctl.set_read_timeout(Some(remaining(deadline)?))
            .map_err(map_io)?;
        match stream.read(&mut chunk) {
            Ok(0) => {
                return match parse_response(&buf, true).map_err(HttpError::Unavailable)? {
                    Some(resp) => Ok(resp),
                    None => Err(HttpError::Unavailable(
                        "connection closed before a response".to_string(),
                    )),
                };
            }
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
            // A TLS stream ends without close_notify: the framing check above
            // already returned every complete response, so this is truncation.
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
                return Err(HttpError::Unavailable(
                    "connection closed before a complete response".to_string(),
                ));
            }
            Err(e) => return Err(map_io(e)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_parsing() {
        let u = parse_url("http://127.0.0.1:8080/mcp").unwrap();
        assert_eq!(u.host, "127.0.0.1");
        assert_eq!(u.port, 8080);
        assert_eq!(u.path, "/mcp");
        assert_eq!(parse_url("http://example.test").unwrap().port, 80);
        #[cfg(not(feature = "tls"))]
        assert!(matches!(
            parse_url("https://api.mctl.ai/mcp"),
            Err(HttpError::Unsupported(_))
        ));
        #[cfg(feature = "tls")]
        {
            let u = parse_url("https://api.mctl.ai/mcp").unwrap();
            assert_eq!(u.port, 443);
            assert!(u.tls);
            assert!(!parse_url("http://example.test").unwrap().tls);
        }
        assert!(parse_url("ftp://x").is_err());
    }

    #[test]
    fn chunked_decoding() {
        let data = b"4\r\nWiki\r\n5;ext=1\r\npedia\r\n0\r\n\r\n";
        assert_eq!(decode_chunked(data).unwrap().unwrap(), b"Wikipedia");
        assert!(decode_chunked(b"4\r\nWi").unwrap().is_none());
        assert!(decode_chunked(b"zz\r\n").is_err());
    }

    #[test]
    fn content_length_response() {
        let raw = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nX-Request-Id: r1\r\n\r\nokEXTRA";
        let r = parse_response(raw, false).unwrap().unwrap();
        assert_eq!(r.status, 200);
        assert_eq!(r.body, b"ok");
        assert_eq!(r.header("x-request-id"), Some("r1"));
        assert!(parse_response(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nok", false)
            .unwrap()
            .is_none());
        assert!(parse_response(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nok", true).is_err());
    }

    #[test]
    fn chunked_and_eof_framing() {
        let raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nhi\r\n0\r\n\r\n";
        assert_eq!(parse_response(raw, false).unwrap().unwrap().body, b"hi");
        let raw = b"HTTP/1.1 200 OK\r\n\r\nuntil-close";
        assert!(parse_response(raw, false).unwrap().is_none());
        assert_eq!(parse_response(raw, true).unwrap().unwrap().body, b"until-close");
        let raw = b"HTTP/1.1 202 Accepted\r\n\r\n";
        assert!(parse_response(raw, false).unwrap().unwrap().body.is_empty());
    }
}
