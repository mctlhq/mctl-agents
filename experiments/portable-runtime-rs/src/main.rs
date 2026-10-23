use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Duration;

use mctl_runtime::bridge;
use mctl_runtime::credential;
use mctl_runtime::hostcfg;
use mctl_runtime::json::Value;
use mctl_runtime::{allowlist, spawn};

const DEFAULT_UPSTREAM: &str = "https://api.mctl.ai/mcp";
const DEFAULT_ALLOWLIST: &str = "fixtures/allowlist.toml";
const DEFAULT_TIMEOUT_MS: u64 = 10_000;

const USAGE: &str = "usage: mctl-runtime <command> [options]

commands:
  proxy   [--upstream URL] [--allowlist PATH] [--timeout-ms N]
  init    --host fixture|claude-code [--token-file PATH] [--upstream URL]
          [--write --config-dir DIR]
  doctor  [--upstream URL] [--allowlist PATH] [--timeout-ms N]
  --version

credentials: MCTL_RUNTIME_TOKEN_FILE (a 0600 file) or MCTL_TOKEN. No flag accepts a token.";

fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

fn env_lookup(key: &str) -> Option<String> {
    std::env::var(key).ok()
}

/// Strict flag parser: unknown flags and positionals are errors, so no flag can
/// ever carry a token.
fn parse_flags(
    args: &[String],
    valued: &[&str],
    bools: &[&str],
) -> Result<HashMap<String, String>, String> {
    let mut map = HashMap::new();
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        if bools.contains(&a) {
            map.insert(a.to_string(), String::new());
            i += 1;
        } else if valued.contains(&a) {
            let v = args
                .get(i + 1)
                .ok_or_else(|| format!("{a} requires a value"))?;
            map.insert(a.to_string(), v.clone());
            i += 2;
        } else {
            return Err(format!("unknown argument: {a}"));
        }
    }
    Ok(map)
}

fn usage_error(msg: &str) -> ExitCode {
    eprintln!("mctl-runtime: {msg}");
    eprintln!("{USAGE}");
    ExitCode::from(2)
}

fn timeout_from(flags: &HashMap<String, String>) -> Result<Duration, String> {
    match flags.get("--timeout-ms") {
        None => Ok(Duration::from_millis(DEFAULT_TIMEOUT_MS)),
        Some(v) => match v.parse::<u64>() {
            Ok(ms) if ms > 0 => Ok(Duration::from_millis(ms)),
            _ => Err(format!("--timeout-ms must be a positive integer, got {v}")),
        },
    }
}

fn cmd_proxy(args: &[String]) -> ExitCode {
    let flags = match parse_flags(args, &["--upstream", "--allowlist", "--timeout-ms"], &[]) {
        Ok(f) => f,
        Err(e) => return usage_error(&e),
    };
    let timeout = match timeout_from(&flags) {
        Ok(t) => t,
        Err(e) => return usage_error(&e),
    };
    let allow_path = flags
        .get("--allowlist")
        .map_or(DEFAULT_ALLOWLIST, String::as_str);
    let allowed = match allowlist::load(Path::new(allow_path)) {
        Ok(list) => list,
        Err(e) => {
            eprintln!("mctl-runtime: {e}");
            return ExitCode::from(2);
        }
    };
    let source = credential::resolve(&env_lookup);
    let token = match source.load() {
        Ok(t) => Some(t),
        Err(e) => {
            eprintln!("mctl-runtime: credential: {e}");
            None
        }
    };
    let cfg = bridge::Config {
        upstream_url: flags
            .get("--upstream")
            .cloned()
            .unwrap_or_else(|| DEFAULT_UPSTREAM.to_string()),
        allowlist: allowed,
        timeout,
    };
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    match bridge::run(&cfg, token, stdin.lock(), &mut out) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("mctl-runtime: stdio session ended: {e}");
            ExitCode::from(1)
        }
    }
}

fn cmd_init(args: &[String]) -> ExitCode {
    let flags = match parse_flags(
        args,
        &["--host", "--token-file", "--upstream", "--config-dir"],
        &["--write"],
    ) {
        Ok(f) => f,
        Err(e) => return usage_error(&e),
    };
    let Some(host) = flags.get("--host").and_then(|h| hostcfg::Host::parse(h)) else {
        return usage_error("init requires --host fixture|claude-code");
    };
    let exe = std::env::current_exe()
        .and_then(std::fs::canonicalize)
        .map_or_else(
            |_| "mctl-runtime".to_string(),
            |p| p.to_string_lossy().into_owned(),
        );
    let content = hostcfg::render(
        host,
        &exe,
        flags.get("--upstream").map(String::as_str),
        flags.get("--token-file").map(String::as_str),
    );
    if !flags.contains_key("--write") {
        print!("{content}");
        return ExitCode::SUCCESS;
    }
    let Some(dir) = flags.get("--config-dir") else {
        eprintln!("mctl-runtime: init: --write requires --config-dir");
        return ExitCode::from(2);
    };
    let home = std::env::var_os("HOME").map(PathBuf::from);
    match hostcfg::write_config(host, Path::new(dir), &content, home.as_deref()) {
        Ok(path) => {
            println!("wrote {}", path.display());
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("mctl-runtime: init: {e}");
            ExitCode::from(2)
        }
    }
}

fn cmd_doctor(args: &[String]) -> ExitCode {
    let flags = match parse_flags(args, &["--upstream", "--allowlist", "--timeout-ms"], &[]) {
        Ok(f) => f,
        Err(e) => return usage_error(&e),
    };
    let timeout = match timeout_from(&flags) {
        Ok(t) => t,
        Err(e) => return usage_error(&e),
    };
    let source = credential::resolve(&env_lookup);
    let doctor_args = hostcfg::DoctorArgs {
        upstream_url: flags
            .get("--upstream")
            .map_or(DEFAULT_UPSTREAM, String::as_str),
        allowlist_path: Path::new(
            flags
                .get("--allowlist")
                .map_or(DEFAULT_ALLOWLIST, String::as_str),
        ),
        timeout,
    };
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    match hostcfg::doctor(&doctor_args, source.as_ref(), &mut out) {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::from(1),
        Err(e) => {
            eprintln!("mctl-runtime: doctor: {e}");
            ExitCode::from(1)
        }
    }
}

/// Hidden test hook: prove a credential was loaded in this process, then spawn
/// a child through the scrubbing helper and relay what the child sees.
fn cmd_spawn_probe() -> ExitCode {
    let source = credential::resolve(&env_lookup);
    if let Err(e) = source.load() {
        eprintln!("mctl-runtime: spawn-probe: credential: {e}");
        return ExitCode::from(3);
    }
    let exe = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("mctl-runtime: spawn-probe: {e}");
            return ExitCode::from(1);
        }
    };
    match spawn::command(&exe).arg("__probe-dump").output() {
        Ok(output) => {
            let _ = std::io::stdout().write_all(&output.stdout);
            let code = output.status.code().unwrap_or(1);
            ExitCode::from(u8::try_from(code).unwrap_or(1))
        }
        Err(e) => {
            eprintln!("mctl-runtime: spawn-probe: {e}");
            ExitCode::from(1)
        }
    }
}

/// Hidden test hook: print this process's argv and environment as JSON.
fn cmd_probe_dump() -> ExitCode {
    let argv = Value::Arr(
        std::env::args_os()
            .map(|a| Value::Str(a.to_string_lossy().into_owned()))
            .collect(),
    );
    let mut vars: Vec<(String, String)> = std::env::vars_os()
        .map(|(k, v)| {
            (
                k.to_string_lossy().into_owned(),
                v.to_string_lossy().into_owned(),
            )
        })
        .collect();
    vars.sort();
    let env = Value::Obj(vars.into_iter().map(|(k, v)| (k, Value::Str(v))).collect());
    println!("{}", Value::obj(vec![("argv", argv), ("env", env)]).to_compact());
    ExitCode::SUCCESS
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = args.first().map(String::as_str) else {
        println!("mctl-runtime {}", version());
        return ExitCode::SUCCESS;
    };
    let rest = &args[1..];
    match cmd {
        "--version" | "-V" | "version" => {
            println!("mctl-runtime {}", version());
            ExitCode::SUCCESS
        }
        "--help" | "-h" | "help" => {
            println!("{USAGE}");
            ExitCode::SUCCESS
        }
        "proxy" => cmd_proxy(rest),
        "init" => cmd_init(rest),
        "doctor" => cmd_doctor(rest),
        "--spawn-probe" => cmd_spawn_probe(),
        "__probe-dump" => cmd_probe_dump(),
        other => usage_error(&format!("unknown command: {other}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_is_not_empty() {
        assert!(!version().is_empty());
    }

    #[test]
    fn flags_reject_unknown_and_token_values() {
        let args = vec!["--token".to_string(), "secret".to_string()];
        assert!(parse_flags(&args, &["--upstream"], &[]).is_err());
        let ok = vec!["--upstream".to_string(), "http://h/mcp".to_string()];
        let flags = parse_flags(&ok, &["--upstream"], &[]).unwrap();
        assert_eq!(flags.get("--upstream").map(String::as_str), Some("http://h/mcp"));
    }
}
