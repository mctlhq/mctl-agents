//! Fixture host: drives the `mctl-runtime` binary over stdio.

use std::io::{BufRead, BufReader, Read, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use mctl_runtime::json::{self, Value};

use super::{bin, TempDir};

const REQUEST_DEADLINE: Duration = Duration::from_secs(15);

pub struct Host {
    child: Child,
    stdin: Option<ChildStdin>,
    lines: Receiver<String>,
    stderr: Arc<Mutex<String>>,
    stderr_thread: Option<JoinHandle<()>>,
    next_id: u64,
    /// The child's `TMPDIR`, expected to stay empty.
    pub tmp: TempDir,
    /// The child's working directory, expected to stay empty.
    pub work: TempDir,
    _home: TempDir,
}

impl Host {
    /// Spawn the binary with a cleared environment: only `PATH`, `HOME`,
    /// `TMPDIR` and the explicit `env` pairs.
    pub fn spawn(args: &[&str], env: &[(&str, &str)]) -> Host {
        let tmp = TempDir::new("host-tmp");
        let work = TempDir::new("host-work");
        let home = TempDir::new("host-home");
        let mut cmd = Command::new(bin());
        cmd.args(args)
            .env_clear()
            .env("PATH", std::env::var("PATH").unwrap_or_default())
            .env("HOME", home.path())
            .env("TMPDIR", tmp.path())
            .current_dir(work.path())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for (k, v) in env {
            cmd.env(k, v);
        }
        let mut child = cmd.spawn().expect("spawn mctl-runtime");
        let stdin = child.stdin.take();
        let stdout = child.stdout.take().expect("stdout");
        let mut stderr_pipe = child.stderr.take().expect("stderr");

        let (tx, lines) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if tx.send(line).is_err() {
                    break;
                }
            }
        });
        let stderr = Arc::new(Mutex::new(String::new()));
        let sink = Arc::clone(&stderr);
        let stderr_thread = std::thread::spawn(move || {
            let mut raw = Vec::new();
            let _ = stderr_pipe.read_to_end(&mut raw);
            sink.lock()
                .expect("lock")
                .push_str(&String::from_utf8_lossy(&raw));
        });

        Host {
            child,
            stdin,
            lines,
            stderr,
            stderr_thread: Some(stderr_thread),
            next_id: 0,
            tmp,
            work,
            _home: home,
        }
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    pub fn send(&mut self, msg: &Value) {
        let stdin = self.stdin.as_mut().expect("stdin open");
        stdin
            .write_all(format!("{}\n", msg.to_compact()).as_bytes())
            .expect("write to proxy");
        stdin.flush().expect("flush");
    }

    pub fn send_raw(&mut self, line: &str) {
        let stdin = self.stdin.as_mut().expect("stdin open");
        stdin
            .write_all(format!("{line}\n").as_bytes())
            .expect("write to proxy");
        stdin.flush().expect("flush");
    }

    pub fn notify(&mut self, method: &str) {
        self.send(&Value::obj(vec![
            ("jsonrpc", Value::text("2.0")),
            ("method", Value::text(method)),
        ]));
    }

    /// The id the next `request` will use.
    pub fn peek_next_id(&self) -> u64 {
        self.next_id + 1
    }

    /// Send a request and return the full JSON-RPC response.
    pub fn request(&mut self, method: &str, params: Value) -> Value {
        self.next_id += 1;
        let id = self.next_id;
        self.send(&Value::obj(vec![
            ("jsonrpc", Value::text("2.0")),
            ("id", Value::num(id)),
            ("method", Value::text(method)),
            ("params", params),
        ]));
        self.read_response()
    }

    pub fn read_response(&mut self) -> Value {
        let line = self
            .lines
            .recv_timeout(REQUEST_DEADLINE)
            .expect("proxy answered within the deadline");
        json::parse(&line).unwrap_or_else(|e| panic!("proxy printed non-JSON on stdout: {e}: {line}"))
    }

    /// `initialize` plus `notifications/initialized`.
    pub fn handshake(&mut self) {
        let resp = self.request(
            "initialize",
            Value::obj(vec![
                ("protocolVersion", Value::text("2025-06-18")),
                ("capabilities", Value::obj(vec![])),
                (
                    "clientInfo",
                    Value::obj(vec![
                        ("name", Value::text("fixture-host")),
                        ("version", Value::text("0")),
                    ]),
                ),
            ]),
        );
        assert!(resp.get("result").is_some(), "initialize failed: {resp:?}");
        self.notify("notifications/initialized");
    }

    pub fn call_tool(&mut self, name: &str, arguments: Value) -> Value {
        self.request(
            "tools/call",
            Value::obj(vec![
                ("name", Value::text(name)),
                ("arguments", arguments),
            ]),
        )
    }

    pub fn ping(&mut self) -> Value {
        self.request("ping", Value::obj(vec![]))
    }

    /// Close stdin, wait for exit, and return everything written to stderr.
    pub fn finish(&mut self) -> String {
        self.stdin.take();
        let _ = self.child.wait();
        if let Some(t) = self.stderr_thread.take() {
            let _ = t.join();
        }
        self.stderr.lock().expect("lock").clone()
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub struct CliOutput {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

impl CliOutput {
    pub fn combined(&self) -> String {
        format!("{}{}", self.stdout, self.stderr)
    }
}

/// Run the binary to completion with a cleared environment plus `env`.
pub fn run_cli(args: &[&str], env: &[(&str, &str)], cwd: &Path) -> CliOutput {
    let mut cmd = Command::new(bin());
    cmd.args(args)
        .env_clear()
        .env("PATH", std::env::var("PATH").unwrap_or_default())
        .current_dir(cwd)
        .stdin(Stdio::null());
    for (k, v) in env {
        cmd.env(k, v);
    }
    let out = cmd.output().expect("run mctl-runtime");
    CliOutput {
        code: out.status.code().unwrap_or(-1),
        stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
    }
}
