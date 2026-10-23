//! Scenario functions T2-T10. Each test file calls one; `tests/stability.rs`
//! runs T2-T8 three times in sequence.

use std::time::{Duration, Instant};

use mctl_runtime::json::{self, Value};

use super::fake_upstream::{
    closed_port_url, FakeUpstream, Knobs, APPROVAL_RESULT, DENY_RESULT, SESSION_ID, TOOL_APPROVAL,
    TOOL_DENY, TOOL_SLOW, TOOL_WHOAMI,
};
use super::host::{run_cli, Host};
use super::{
    manifest_dir, write_file, write_token_file, TempDir, ALLOWLIST_PRESENT_TEXT, ALLOWLIST_TEXT,
    SENTINEL,
};

const TOKEN: &str = "test-token";
const FIXED_ARGS_JSON: &str = r#"{"b":2,"a":{"z":1,"y":"argvalue-é"}}"#;
const FIXED_ARGS_DIGEST: &str =
    "sha256:842613ef2de7aac81c9aff148236f275a7d3e85537809cb0b6876e256e3f96fc";

fn empty() -> Value {
    Value::obj(vec![])
}

fn path_str(p: &std::path::Path) -> &str {
    p.to_str().expect("utf-8 path")
}

/// Spawn `proxy` against `upstream_url`, with the allowlist written into `files`, and handshake.
fn start_proxy(
    upstream_url: &str,
    files: &TempDir,
    allowlist_text: &str,
    timeout_ms: u64,
    env: &[(&str, &str)],
) -> Host {
    let allow = write_file(files, "allowlist.toml", allowlist_text);
    let timeout = timeout_ms.to_string();
    let mut host = Host::spawn(
        &[
            "proxy",
            "--upstream",
            upstream_url,
            "--allowlist",
            path_str(&allow),
            "--timeout-ms",
            &timeout,
        ],
        env,
    );
    host.handshake();
    host
}

fn result_of(resp: &Value) -> &Value {
    resp.get("result")
        .unwrap_or_else(|| panic!("expected a result, got {resp:?}"))
}

fn assert_refusal(resp: &Value, reason: &str) {
    let result = result_of(resp);
    assert_eq!(
        result.get("isError").and_then(Value::as_bool),
        Some(true),
        "{resp:?}"
    );
    let code = result
        .get("structuredContent")
        .and_then(|s| s.get("reason_code"))
        .and_then(Value::as_str);
    assert_eq!(code, Some(reason), "{resp:?}");
}

fn listed_tools(resp: &Value) -> Vec<Value> {
    result_of(resp)
        .get("tools")
        .and_then(Value::as_array)
        .expect("tools array")
        .to_vec()
}

// T2
pub fn t2_allowlist() {
    for knobs in [
        Knobs::default(),
        Knobs {
            sse: true,
            ..Knobs::default()
        },
    ] {
        let fake = FakeUpstream::start(knobs);
        let files = TempDir::new("t2");
        let mut host = start_proxy(&fake.url(), &files, ALLOWLIST_TEXT, 5000, &[("MCTL_TOKEN", TOKEN)]);
        let tools = listed_tools(&host.request("tools/list", empty()));
        let names: Vec<&str> = tools
            .iter()
            .map(|t| t.get("name").and_then(Value::as_str).expect("name"))
            .collect();
        assert_eq!(names, vec!["mctl_whoami", "deny_tool", "approval_tool", "slow_tool"]);
        for (tool, upstream_json) in tools
            .iter()
            .zip([TOOL_WHOAMI, TOOL_DENY, TOOL_APPROVAL, TOOL_SLOW])
        {
            assert_eq!(tool.to_compact(), upstream_json, "tool differs from upstream");
            let schema = tool.get("inputSchema").expect("inputSchema").to_compact();
            assert!(
                upstream_json.contains(&schema),
                "inputSchema is not byte-equal to upstream: {schema}"
            );
        }
        host.finish();
    }
}

// T3
pub fn t3_fail_closed() {
    let fake = FakeUpstream::start(Knobs::default());
    let files = TempDir::new("t3");
    let mut host = start_proxy(&fake.url(), &files, ALLOWLIST_TEXT, 5000, &[("MCTL_TOKEN", TOKEN)]);
    for name in ["secret_tool", "no_such_tool"] {
        assert_refusal(&host.call_tool(name, empty()), "not-eligible");
    }
    assert_eq!(fake.count("tools/call"), 0);
    assert_eq!(fake.total(), 0, "a refused call must perform no upstream I/O");
    host.finish();

    let missing = files.join("does-not-exist.toml");
    let empty_list = write_file(&files, "empty.toml", "tools = []\n");
    let zero_bytes = write_file(&files, "zero.toml", "");
    let invalid = write_file(&files, "invalid.toml", "this is === not toml\n");
    for path in [&missing, &empty_list, &zero_bytes, &invalid] {
        let r = run_cli(
            &[
                "proxy",
                "--upstream",
                "http://127.0.0.1:9/mcp",
                "--allowlist",
                path_str(path),
            ],
            &[],
            files.path(),
        );
        assert_ne!(r.code, 0, "proxy must refuse to start with {path:?}");
        assert!(r.stdout.is_empty(), "proxy served despite a bad allowlist");
    }
}

// T4
pub fn t4_remote_refusal_preserved() {
    let fake = FakeUpstream::start(Knobs::default());
    let files = TempDir::new("t4");
    let mut host = start_proxy(&fake.url(), &files, ALLOWLIST_TEXT, 5000, &[("MCTL_TOKEN", TOKEN)]);
    for (name, upstream_result) in [("deny_tool", DENY_RESULT), ("approval_tool", APPROVAL_RESULT)] {
        let resp = host.call_tool(name, empty());
        let result = result_of(&resp);
        assert_eq!(result.get("isError").and_then(Value::as_bool), Some(true));
        assert_eq!(result.to_compact(), upstream_result, "{name} was altered");
    }
    host.finish();
}

// T5
pub fn t5_unavailable_and_timeout() {
    let files = TempDir::new("t5");
    let mut host = start_proxy(
        &closed_port_url(),
        &files,
        ALLOWLIST_TEXT,
        300,
        &[("MCTL_TOKEN", TOKEN)],
    );
    assert_refusal(&host.call_tool("mctl_whoami", empty()), "provider-unavailable");
    assert!(host.ping().get("result").is_some(), "ping after failure");
    host.finish();

    let fake = FakeUpstream::start(Knobs {
        slow_ms: 2000,
        ..Knobs::default()
    });
    let mut host = start_proxy(&fake.url(), &files, ALLOWLIST_TEXT, 300, &[("MCTL_TOKEN", TOKEN)]);
    let started = Instant::now();
    let resp = host.call_tool("slow_tool", empty());
    let elapsed = started.elapsed();
    assert_refusal(&resp, "provider-timeout");
    assert!(
        elapsed < Duration::from_millis(1500),
        "timeout answered after {elapsed:?}"
    );
    assert!(host.ping().get("result").is_some(), "ping after timeout");
    host.finish();

    // A connection reset is also provider-unavailable.
    let fake = FakeUpstream::start(Knobs {
        reset: true,
        ..Knobs::default()
    });
    let mut host = start_proxy(&fake.url(), &files, ALLOWLIST_TEXT, 1000, &[("MCTL_TOKEN", TOKEN)]);
    assert_refusal(&host.call_tool("mctl_whoami", empty()), "provider-unavailable");
    assert!(host.ping().get("result").is_some());
    host.finish();
}

// T6
pub fn t6_token_absent_or_rejected() {
    let fake = FakeUpstream::start(Knobs::default());
    let files = TempDir::new("t6");
    let mut host = start_proxy(&fake.url(), &files, ALLOWLIST_TEXT, 2000, &[]);
    assert_refusal(&host.call_tool("mctl_whoami", empty()), "credential-absent");
    host.finish();
    let allow = write_file(&files, "doctor-allowlist.toml", ALLOWLIST_PRESENT_TEXT);
    let url = fake.url();
    let r = run_cli(
        &["doctor", "--upstream", &url, "--allowlist", path_str(&allow)],
        &[],
        files.path(),
    );
    assert_ne!(r.code, 0);
    assert!(r.combined().contains("credential absent"), "{}", r.combined());
    assert_eq!(fake.total(), 0, "no credential must mean zero upstream requests");

    let rejecting = FakeUpstream::start(Knobs {
        status_401: true,
        ..Knobs::default()
    });
    let mut host = start_proxy(
        &rejecting.url(),
        &files,
        ALLOWLIST_TEXT,
        2000,
        &[("MCTL_TOKEN", TOKEN)],
    );
    assert_refusal(&host.call_tool("mctl_whoami", empty()), "credential-rejected");
    assert_eq!(rejecting.total(), 1, "401 must cost exactly one upstream attempt");
    let stderr = host.finish();
    // A handshake that fails before any request id is issued must not
    // report a stale id from an earlier call.
    let rec_line = stderr.lines().find(|l| l.starts_with('{')).expect("correlation line");
    let rec = json::parse(rec_line).expect("correlation JSON");
    assert_eq!(rec.get("outcome").and_then(Value::as_str), Some("credential-rejected"));
    assert!(
        matches!(rec.get("upstream_request_id"), None | Some(Value::Null)),
        "failed handshake carries a request id: {rec_line}"
    );
}

#[cfg(target_os = "linux")]
fn process_args(pid: u32) -> String {
    std::fs::read(format!("/proc/{pid}/cmdline"))
        .map(|b| String::from_utf8_lossy(&b).replace('\0', " "))
        .unwrap_or_default()
}

#[cfg(not(target_os = "linux"))]
fn process_args(pid: u32) -> String {
    let out = std::process::Command::new("ps")
        .args(["-o", "args=", "-p", &pid.to_string()])
        .output()
        .expect("run ps");
    String::from_utf8_lossy(&out.stdout).into_owned()
}

// T7
pub fn t7_no_credential_in_argv_env_or_config() {
    let fake = FakeUpstream::start(Knobs {
        echo_headers: true,
        ..Knobs::default()
    });
    let files = TempDir::new("t7");
    let mut host = start_proxy(
        &fake.url(),
        &files,
        ALLOWLIST_TEXT,
        5000,
        &[("MCTL_TOKEN", SENTINEL)],
    );
    let args = process_args(host.pid());
    assert!(args.contains("proxy"), "could not read process args: {args:?}");
    assert!(!args.contains(SENTINEL), "token found in argv: {args}");

    let resp = host.call_tool("mctl_whoami", empty());
    assert_eq!(
        result_of(&resp).get("isError").and_then(Value::as_bool),
        Some(false)
    );
    let received = fake.last("tools/call").expect("a tools/call was made");
    assert_eq!(
        received.header("authorization"),
        Some(format!("Bearer {SENTINEL}").as_str()),
        "upstream did not receive the token"
    );
    let stderr = host.finish();
    assert!(!stderr.contains(SENTINEL), "token found in stderr: {stderr}");

    // The scrubbing spawn helper: parent holds the credential, child must not.
    let token_file = write_token_file(&files, "token", 0o600, SENTINEL);
    let token_file_str = path_str(&token_file);
    for env in [
        vec![("MCTL_TOKEN", SENTINEL)],
        vec![("MCTL_RUNTIME_TOKEN_FILE", token_file_str)],
    ] {
        let r = run_cli(&["--spawn-probe"], &env, files.path());
        assert_eq!(r.code, 0, "spawn probe failed: {}", r.stderr);
        assert!(!r.stdout.contains(SENTINEL), "token visible to child: {}", r.stdout);
        let dump = json::parse(r.stdout.trim()).expect("probe JSON");
        let child_env = dump.get("env").expect("env");
        assert!(child_env.get("PATH").is_some(), "probe saw no environment");
        assert!(child_env.get("MCTL_TOKEN").is_none());
        assert!(child_env.get("MCTL_RUNTIME_TOKEN_FILE").is_none());
    }

    // Generated host config references the credential, never contains it.
    let allowlist_path = write_file(&files, "init-allow.toml", ALLOWLIST_TEXT);
    let allowlist_str = path_str(&allowlist_path);
    for host_kind in ["fixture", "claude-code"] {
        let r = run_cli(
            &["init", "--host", host_kind, "--allowlist", allowlist_str],
            &[("MCTL_TOKEN", SENTINEL)],
            files.path(),
        );
        assert_eq!(r.code, 0, "{}", r.stderr);
        assert!(!r.combined().contains(SENTINEL));
        let r = run_cli(
            &["init", "--host", host_kind, "--allowlist", allowlist_str, "--token-file", token_file_str],
            &[("MCTL_TOKEN", SENTINEL)],
            files.path(),
        );
        assert_eq!(r.code, 0, "{}", r.stderr);
        assert!(r.stdout.contains(token_file_str));
        assert!(!r.combined().contains(SENTINEL));
    }
}

// T8
pub fn t8_correlation() {
    let fake = FakeUpstream::start(Knobs {
        echo_headers: true,
        ..Knobs::default()
    });
    let files = TempDir::new("t8");
    let mut host = start_proxy(&fake.url(), &files, ALLOWLIST_TEXT, 5000, &[("MCTL_TOKEN", TOKEN)]);
    let args = json::parse(FIXED_ARGS_JSON).expect("args");
    let resp = host.call_tool("mctl_whoami", args);
    assert_eq!(
        result_of(&resp).get("isError").and_then(Value::as_bool),
        Some(false)
    );
    let refused = host.call_tool("not_allowlisted_tool", json::parse("{}").expect("args"));
    assert_refusal(&refused, "not-eligible");
    let stderr = host.finish();

    let all: Vec<&str> = stderr.lines().filter(|l| l.starts_with('{')).collect();
    assert_eq!(all.len(), 2, "expected two correlation lines, stderr: {stderr}");
    let refused_rec = json::parse(all[1]).expect("refused record JSON");
    assert_eq!(refused_rec.get("outcome").and_then(Value::as_str), Some("not-eligible"));
    for field in ["upstream_session_id", "upstream_request_id"] {
        assert!(
            matches!(refused_rec.get(field), None | Some(Value::Null)),
            "refused call carries {field}: {}",
            all[1]
        );
    }
    let lines: Vec<&str> = all[..1].to_vec();
    let rec = json::parse(lines[0]).expect("correlation JSON");
    for field in [
        "invocation_id",
        "host_request_id",
        "tool",
        "args_sha256",
        "upstream_session_id",
        "upstream_request_id",
        "outcome",
        "is_error",
        "latency_ms",
    ] {
        assert!(rec.get(field).is_some(), "missing field {field}: {}", lines[0]);
    }
    let invocation = rec.get("invocation_id").and_then(Value::as_str).expect("id");
    let sent = fake.last("tools/call").expect("tools/call");
    assert_eq!(sent.header("x-mctl-invocation-id"), Some(invocation));
    assert_eq!(invocation.len(), 36);
    assert_eq!(invocation.as_bytes()[14], b'4', "not a UUIDv4: {invocation}");
    assert_eq!(rec.get("host_request_id").map(Value::to_compact), Some("2".to_string()));
    assert_eq!(rec.get("tool").and_then(Value::as_str), Some("mctl_whoami"));
    assert_eq!(
        rec.get("args_sha256").and_then(Value::as_str),
        Some(FIXED_ARGS_DIGEST)
    );
    assert_eq!(
        rec.get("upstream_session_id").and_then(Value::as_str),
        Some(SESSION_ID)
    );
    assert!(rec
        .get("upstream_request_id")
        .and_then(Value::as_str)
        .is_some_and(|r| r.starts_with("req-")));
    assert_eq!(rec.get("outcome").and_then(Value::as_str), Some("ok"));
    assert_eq!(rec.get("is_error").and_then(Value::as_bool), Some(false));
    assert!(matches!(rec.get("latency_ms"), Some(Value::Num(_))));

    // No argument or result values, and nothing written to disk.
    assert!(!stderr.contains("argvalue"), "argument value leaked: {stderr}");
    assert!(!stderr.contains("tester"), "result value leaked: {stderr}");
    assert!(host.tmp.listing().is_empty(), "files appeared in TMPDIR");
    assert!(host.work.listing().is_empty(), "files appeared in the cwd");
}

fn copy_dir(src: &std::path::Path, dst: &std::path::Path) {
    for entry in std::fs::read_dir(src).expect("read fixture dir") {
        let entry = entry.expect("entry");
        std::fs::copy(entry.path(), dst.join(entry.file_name())).expect("copy fixture file");
    }
}

// T9
pub fn t9_init_dry_run() {
    let fixture = TempDir::new("t9-fixture");
    copy_dir(&manifest_dir().join("fixtures").join("host-fixture"), fixture.path());
    let home = TempDir::new("t9-home");
    let home_str = path_str(home.path());
    let fixture_str = path_str(fixture.path());
    let allowlist_path = manifest_dir().join("fixtures").join("allowlist.toml");
    let allowlist_str = path_str(&allowlist_path);
    let before = fixture.listing();
    assert!(!before.is_empty(), "fixture dir should not be empty");
    let mtime_before = std::fs::metadata(fixture.path())
        .and_then(|m| m.modified())
        .expect("mtime");

    for kind in ["fixture", "claude-code"] {
        let r = run_cli(
            &["init", "--host", kind, "--allowlist", allowlist_str],
            &[("HOME", home_str)],
            fixture.path(),
        );
        assert_eq!(r.code, 0, "{}", r.stderr);
        assert!(r.stdout.contains("\"command\""), "{}", r.stdout);
        assert!(r.stdout.contains(allowlist_str), "{}", r.stdout);
    }
    for kind in ["fixture", "claude-code"] {
        let r = run_cli(&["init", "--host", kind], &[("HOME", home_str)], fixture.path());
        assert_eq!(r.code, 2, "missing allowlist must be refused: {}", r.stdout);
    }
    assert_eq!(fixture.listing(), before, "dry run wrote files");
    let mtime_after = std::fs::metadata(fixture.path())
        .and_then(|m| m.modified())
        .expect("mtime");
    assert_eq!(mtime_before, mtime_after, "dry run touched the directory");

    let r = run_cli(
        &["init", "--host", "fixture", "--write", "--allowlist", allowlist_str],
        &[("HOME", home_str)],
        fixture.path(),
    );
    assert_ne!(r.code, 0, "--write without --config-dir must be refused");
    assert_eq!(fixture.listing(), before);

    let r = run_cli(
        &["init", "--host", "fixture", "--write", "--config-dir", fixture_str, "--allowlist", allowlist_str],
        &[("HOME", home_str)],
        fixture.path(),
    );
    assert_eq!(r.code, 0, "{}", r.stderr);
    let mut expected = before.clone();
    expected.push("mcp.json".to_string());
    expected.sort();
    assert_eq!(fixture.listing(), expected);
    let written = std::fs::read_to_string(fixture.join("mcp.json")).expect("mcp.json");
    assert!(json::parse(written.trim()).expect("valid JSON").get("servers").is_some());

    let claude_dir = TempDir::new("t9-claude");
    let r = run_cli(
        &["init", "--host", "claude-code", "--write", "--config-dir", path_str(claude_dir.path()), "--allowlist", allowlist_str],
        &[("HOME", home_str)],
        fixture.path(),
    );
    assert_eq!(r.code, 0, "{}", r.stderr);
    assert_eq!(claude_dir.listing(), vec![".mcp.json".to_string()]);

    // Known real host config locations are refused.
    for sub in [".claude", ".config/claude", ".cursor"] {
        let dir = home.path().join(sub);
        std::fs::create_dir_all(&dir).expect("create fake host dir");
        let r = run_cli(
            &["init", "--host", "claude-code", "--write", "--config-dir", path_str(&dir), "--allowlist", allowlist_str],
            &[("HOME", home_str)],
            fixture.path(),
        );
        assert_ne!(r.code, 0, "{sub} must be refused");
        assert!(
            std::fs::read_dir(&dir).expect("read").next().is_none(),
            "{sub} was written to"
        );
    }
}

// T10
pub fn t10_token_file_mode() {
    let files = TempDir::new("t10");
    let allow = write_file(&files, "allowlist.toml", ALLOWLIST_PRESENT_TEXT);
    let allow_str = path_str(&allow);

    let fake = FakeUpstream::start(Knobs::default());
    let url = fake.url();
    let loose = write_token_file(&files, "loose", 0o644, "file-secret-token");
    let r = run_cli(
        &["doctor", "--upstream", &url, "--allowlist", allow_str],
        &[("MCTL_RUNTIME_TOKEN_FILE", path_str(&loose))],
        files.path(),
    );
    assert_ne!(r.code, 0);
    let out = r.combined();
    assert!(out.contains("0644"), "{out}");
    assert!(out.contains(path_str(&loose)), "{out}");
    assert!(!out.contains("file-secret-token"), "{out}");
    assert_eq!(fake.total(), 0, "a refused token file must cost no request");

    let strict = write_token_file(&files, "strict", 0o600, "file-secret-token");
    let r = run_cli(
        &["doctor", "--upstream", &url, "--allowlist", allow_str, "--timeout-ms", "5000"],
        &[("MCTL_RUNTIME_TOKEN_FILE", path_str(&strict))],
        files.path(),
    );
    assert_eq!(r.code, 0, "{}", r.combined());
    assert!(r.stdout.contains("credential: ok source=token-file"), "{}", r.stdout);
    assert!(r.stdout.contains("token-file-mode: ok 0600"), "{}", r.stdout);
    assert!(r.stdout.contains("allowlist-missing-upstream: ok none"), "{}", r.stdout);
    assert!(!r.combined().contains("file-secret-token"));
    let seen = fake.last("tools/list").expect("doctor listed tools");
    assert_eq!(seen.header("authorization"), Some("Bearer file-secret-token"));

    // An allowlisted tool the upstream does not offer fails the preflight.
    let ghost = write_file(&files, "ghost.toml", ALLOWLIST_TEXT);
    let r = run_cli(
        &["doctor", "--upstream", &url, "--allowlist", path_str(&ghost), "--timeout-ms", "5000"],
        &[("MCTL_RUNTIME_TOKEN_FILE", path_str(&strict))],
        files.path(),
    );
    assert_ne!(r.code, 0);
    assert!(r.stdout.contains("ghost_tool"), "{}", r.stdout);
}
