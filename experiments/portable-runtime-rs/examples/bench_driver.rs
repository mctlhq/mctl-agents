//! std-only stdio driver for the #746 slice 3 measurements.
//!
//! bench_driver <mode> --upstream URL [--n N] [--subject NAME] [--note TEXT] [--raw] -- <server command...>
//!
//! Modes: `ready`, `latency`, `rss`, `parity`. One driver measures both
//! subjects (the Rust proxy and the Python baseline) with the same clock, the
//! same JSON canonicalizer and the same direct-HTTP reference. Output is JSON
//! lines: subject, metric, value, unit, status, reason, os, head_sha, run_id.
//! A value is null (never 0) when a metric could not be measured.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

use mctl_runtime::credential::Secret;
use mctl_runtime::json::{self, Value};
use mctl_runtime::upstream::Upstream;

const TOKEN: &str = "bench-token-746";
const DEADLINE: Duration = Duration::from_secs(30);
const PARITY_TOOLS: [&str; 5] = [
    "mctl_whoami",
    "deny_tool",
    "approval_tool",
    "secret_tool",
    "no_such_tool",
];

struct Opts {
    mode: String,
    upstream: String,
    n: usize,
    subject: String,
    note: Option<String>,
    raw: bool,
    command: Vec<String>,
}

fn parse_opts(args: &[String]) -> Result<Opts, String> {
    let mut it = args.iter();
    let mode = it.next().ok_or("missing mode")?.clone();
    let mut o = Opts {
        mode,
        upstream: String::new(),
        n: 0,
        subject: "subject".to_string(),
        note: None,
        raw: false,
        command: Vec::new(),
    };
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--" => {
                o.command = it.by_ref().cloned().collect();
                break;
            }
            "--raw" => o.raw = true,
            "--upstream" => o.upstream = it.next().ok_or("--upstream needs a value")?.clone(),
            "--subject" => o.subject = it.next().ok_or("--subject needs a value")?.clone(),
            "--note" => o.note = Some(it.next().ok_or("--note needs a value")?.clone()),
            "--n" => {
                let v = it.next().ok_or("--n needs a value")?;
                o.n = v.parse().map_err(|_| format!("--n must be a number, got {v}"))?;
            }
            other => return Err(format!("unknown argument {other}")),
        }
    }
    if o.command.is_empty() {
        return Err("no server command after --".to_string());
    }
    if o.n == 0 {
        o.n = if o.mode == "ready" { 20 } else { 100 };
    }
    Ok(o)
}

struct Ctx {
    subject: String,
    note: Option<String>,
    os: String,
    head_sha: String,
    run_id: String,
}

impl Ctx {
    fn record(&self, subject: &str, metric: &str, value: Option<f64>, unit: &str, reason: &str) {
        let status = if value.is_some() { "measured" } else { "failed" };
        self.record_status(subject, metric, value, unit, status, reason);
    }

    fn record_status(&self, subject: &str, metric: &str, value: Option<f64>, unit: &str, status: &str, reason: &str) {
        let num = match value {
            Some(v) => Value::Num(format!("{v:.3}")),
            None => Value::Null,
        };
        let mut pairs = vec![
            ("subject", Value::text(subject)),
            ("metric", Value::text(metric)),
            ("value", num),
            ("unit", Value::text(unit)),
            ("status", Value::text(status)),
            ("reason", Value::text(reason)),
            ("os", Value::text(&self.os)),
            ("head_sha", Value::text(&self.head_sha)),
            ("run_id", Value::text(&self.run_id)),
        ];
        if let (Some(note), true) = (&self.note, subject == self.subject) {
            pairs.push(("note", Value::text(note)));
        }
        println!("{}", Value::obj(pairs).to_compact());
    }
}

fn ms(d: Duration) -> f64 {
    d.as_secs_f64() * 1000.0
}

fn percentile(samples: &[f64], p: f64) -> f64 {
    let mut sorted = samples.to_vec();
    sorted.sort_by(|a, b| a.total_cmp(b));
    let rank = (p * sorted.len() as f64).ceil() as usize;
    sorted[rank.saturating_sub(1).min(sorted.len() - 1)]
}

struct Server {
    child: Child,
    stdin: Option<ChildStdin>,
    lines: Receiver<String>,
    next_id: u64,
}

impl Server {
    /// Spawn with a cleared environment plus `PATH`, `HOME` and the bench token.
    fn spawn(command: &[String]) -> Result<Server, String> {
        let (prog, args) = command.split_first().ok_or("empty server command")?;
        let mut child = Command::new(prog)
            .args(args)
            .env_clear()
            .env("PATH", std::env::var("PATH").unwrap_or_default())
            .env("HOME", std::env::var("HOME").unwrap_or_default())
            .env("MCTL_TOKEN", TOKEN)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("spawn {prog}: {e}"))?;
        let stdin = child.stdin.take();
        let stdout = child.stdout.take().ok_or("no stdout")?;
        let (tx, lines) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if tx.send(line).is_err() {
                    break;
                }
            }
        });
        Ok(Server {
            child,
            stdin,
            lines,
            next_id: 0,
        })
    }

    fn pid(&self) -> u32 {
        self.child.id()
    }

    fn send(&mut self, msg: &Value) -> Result<(), String> {
        let stdin = self.stdin.as_mut().ok_or("stdin closed")?;
        stdin
            .write_all(format!("{}\n", msg.to_compact()).as_bytes())
            .and_then(|()| stdin.flush())
            .map_err(|e| format!("write to server: {e}"))
    }

    fn begin(&mut self, method: &str, params: Value) -> Result<(), String> {
        self.next_id += 1;
        let id = self.next_id;
        self.send(&Value::obj(vec![
            ("jsonrpc", Value::text("2.0")),
            ("id", Value::num(id)),
            ("method", Value::text(method)),
            ("params", params),
        ]))
    }

    fn read(&self) -> Result<Value, String> {
        let line = self
            .lines
            .recv_timeout(DEADLINE)
            .map_err(|e| format!("no reply from server: {e}"))?;
        json::parse(&line).map_err(|e| format!("non-JSON reply: {e}: {line}"))
    }

    fn request(&mut self, method: &str, params: Value) -> Result<Value, String> {
        self.begin(method, params)?;
        self.read()
    }

    fn notify(&mut self, method: &str) -> Result<(), String> {
        self.send(&Value::obj(vec![
            ("jsonrpc", Value::text("2.0")),
            ("method", Value::text(method)),
        ]))
    }

    fn handshake(&mut self) -> Result<(), String> {
        let resp = self.request("initialize", init_params())?;
        if resp.get("result").is_none() {
            return Err(format!("initialize failed: {}", resp.to_compact()));
        }
        self.notify("notifications/initialized")
    }

    fn call(&mut self, name: &str) -> Result<Value, String> {
        self.request("tools/call", call_params(name))
    }

    /// One `mctl_whoami` call, in milliseconds.
    fn timed_whoami(&mut self) -> Result<f64, String> {
        let t = Instant::now();
        let resp = self.call("mctl_whoami")?;
        let elapsed = ms(t.elapsed());
        match resp.get("result").and_then(|r| r.get("isError")) {
            Some(Value::Bool(true)) => Err(format!("whoami returned an error: {}", resp.to_compact())),
            _ if resp.get("result").is_none() => {
                Err(format!("whoami had no result: {}", resp.to_compact()))
            }
            _ => Ok(elapsed),
        }
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.stdin.take();
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn init_params() -> Value {
    Value::obj(vec![
        ("protocolVersion", Value::text("2025-06-18")),
        ("capabilities", Value::obj(vec![])),
        (
            "clientInfo",
            Value::obj(vec![
                ("name", Value::text("bench-driver")),
                ("version", Value::text("0")),
            ]),
        ),
    ])
}

fn call_params(name: &str) -> Value {
    Value::obj(vec![
        ("name", Value::text(name)),
        ("arguments", Value::obj(vec![])),
    ])
}

/// Resident set size and, on Linux, the high-water mark, both in kB.
fn rss_kb(pid: u32) -> Option<(u64, Option<u64>)> {
    if cfg!(target_os = "linux") {
        let text = std::fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
        let field = |name: &str| {
            text.lines().find_map(|l| {
                let rest = l.strip_prefix(name)?;
                rest.trim().trim_end_matches("kB").trim().parse::<u64>().ok()
            })
        };
        Some((field("VmRSS:")?, field("VmHWM:")))
    } else {
        let out = Command::new("ps")
            .args(["-o", "rss=", "-p", &pid.to_string()])
            .output()
            .ok()?;
        let kb = String::from_utf8_lossy(&out.stdout).trim().parse::<u64>().ok()?;
        Some((kb, None))
    }
}

fn mode_ready(ctx: &Ctx, o: &Opts) -> Result<(), String> {
    let mut samples = Vec::with_capacity(o.n);
    for _ in 0..o.n {
        let t = Instant::now();
        let mut server = Server::spawn(&o.command)?;
        let resp = server.request("initialize", init_params())?;
        if resp.get("result").is_none() {
            return Err(format!("initialize failed: {}", resp.to_compact()));
        }
        samples.push(ms(t.elapsed()));
    }
    ctx.record(&ctx.subject, "ready_median_ms", Some(percentile(&samples, 0.5)), "ms", "");
    ctx.record(&ctx.subject, "ready_p95_ms", Some(percentile(&samples, 0.95)), "ms", "");
    Ok(())
}

fn mode_latency(ctx: &Ctx, o: &Opts) -> Result<(), String> {
    let mut server = Server::spawn(&o.command)?;
    server.handshake()?;
    let first = server.timed_whoami()?;
    let mut steady = Vec::with_capacity(o.n);
    for _ in 0..o.n {
        steady.push(server.timed_whoami()?);
    }

    // Direct reference: the same calls through the crate's own HTTP client.
    let mut direct = Upstream::new(&o.upstream, Some(Secret::new(TOKEN.to_string())), DEADLINE);
    let mut direct_samples = Vec::with_capacity(o.n + 1);
    for _ in 0..=o.n {
        let t = Instant::now();
        direct
            .call_tool("mctl_whoami", &Value::obj(vec![]), "bench-direct")
            .map_err(|e| format!("direct call failed: {}", e.detail()))?;
        direct_samples.push(ms(t.elapsed()));
    }
    let direct_first = direct_samples[0];
    let direct_steady = &direct_samples[1..];

    let (med, p95) = (percentile(&steady, 0.5), percentile(&steady, 0.95));
    let (dmed, dp95) = (percentile(direct_steady, 0.5), percentile(direct_steady, 0.95));
    let s = ctx.subject.as_str();
    ctx.record(s, "first_call_ms", Some(first), "ms", "");
    ctx.record(s, "steady_median_ms", Some(med), "ms", "");
    ctx.record(s, "steady_p95_ms", Some(p95), "ms", "");
    ctx.record("direct", "first_call_ms", Some(direct_first), "ms", "");
    ctx.record("direct", "steady_median_ms", Some(dmed), "ms", "");
    ctx.record("direct", "steady_p95_ms", Some(dp95), "ms", "");
    ctx.record(s, "overhead_first_ms", Some(first - direct_first), "ms", "subject minus direct");
    ctx.record(s, "overhead_median_ms", Some(med - dmed), "ms", "subject minus direct");
    ctx.record(s, "overhead_p95_ms", Some(p95 - dp95), "ms", "subject minus direct");
    Ok(())
}

fn mode_rss(ctx: &Ctx, o: &Opts) -> Result<(), String> {
    let mut server = Server::spawn(&o.command)?;
    server.handshake()?;
    // One warm call so a lazily opened upstream session counts for every subject.
    server.timed_whoami()?;
    std::thread::sleep(Duration::from_millis(300));
    let pid = server.pid();
    let Some((idle, _)) = rss_kb(pid) else {
        return Err("could not read RSS of the server process".to_string());
    };
    ctx.record(&ctx.subject, "rss_idle_kb", Some(idle as f64), "kB", "");

    server.begin("tools/call", call_params("slow_tool"))?;
    let mut peak = idle;
    let mut hwm = None;
    let deadline = Instant::now() + DEADLINE;
    loop {
        if let Some((rss, h)) = rss_kb(pid) {
            peak = peak.max(rss);
            hwm = h.or(hwm);
        }
        match server.lines.recv_timeout(Duration::from_millis(20)) {
            Ok(_) => break,
            Err(RecvTimeoutError::Timeout) if Instant::now() < deadline => {}
            Err(e) => return Err(format!("slow_tool did not complete: {e}")),
        }
    }
    ctx.record(&ctx.subject, "rss_peak_call_kb", Some(peak as f64), "kB", "max of 20 ms samples during slow_tool");
    match hwm {
        Some(h) => ctx.record(&ctx.subject, "rss_hwm_kb", Some(h as f64), "kB", "VmHWM after slow_tool"),
        None => ctx.record_status(&ctx.subject, "rss_hwm_kb", None, "kB", "unmeasured", "high-water mark not available on this OS"),
    }
    Ok(())
}

fn print_line(label: &str, resp: &Value, raw: bool) {
    let (key, body) = match (resp.get("result"), resp.get("error")) {
        (Some(r), _) => ("result", r.clone()),
        (None, Some(e)) => ("error", e.clone()),
        (None, None) => ("error", Value::Null),
    };
    let line = Value::obj(vec![("request", Value::text(label)), (key, body)]);
    if raw {
        println!("{}", line.to_compact());
    } else {
        println!("{}", line.to_canonical());
    }
}

fn mode_parity(o: &Opts) -> Result<(), String> {
    let mut server = Server::spawn(&o.command)?;
    let init = server.request("initialize", init_params())?;
    print_line("initialize", &init, o.raw);
    server.notify("notifications/initialized")?;
    let list = server.request("tools/list", Value::obj(vec![]))?;
    print_line("tools/list", &list, o.raw);
    for name in PARITY_TOOLS {
        let resp = server.call(name)?;
        print_line(&format!("tools/call {name}"), &resp, o.raw);
    }
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let o = match parse_opts(&args) {
        Ok(o) => o,
        Err(e) => {
            eprintln!("bench_driver: {e}");
            std::process::exit(2);
        }
    };
    let env_or = |k: &str, d: &str| std::env::var(k).unwrap_or_else(|_| d.to_string());
    let ctx = Ctx {
        subject: o.subject.clone(),
        note: o.note.clone(),
        os: std::env::consts::OS.to_string(),
        head_sha: env_or("HEAD_SHA", "unknown"),
        run_id: env_or("RUN_ID", "local"),
    };
    let result = match o.mode.as_str() {
        "ready" => mode_ready(&ctx, &o),
        "latency" => mode_latency(&ctx, &o),
        "rss" => mode_rss(&ctx, &o),
        "parity" => mode_parity(&o),
        other => Err(format!("unknown mode {other}")),
    };
    if let Err(e) = result {
        if o.mode == "parity" {
            eprintln!("bench_driver: parity failed: {e}");
            std::process::exit(1);
        }
        ctx.record(&ctx.subject, &format!("{}_run", o.mode), None, "", &e);
    }
}
