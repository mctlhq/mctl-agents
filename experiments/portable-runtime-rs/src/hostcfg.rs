//! `init` (host config rendering, guarded `--write`) and `doctor` (preflight).
//! Config references the credential by env var name or file path, never by value.

use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::allowlist;
use crate::credential::{self, CredentialSource};
use crate::json::Value;
use crate::upstream::Upstream;

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Host {
    Fixture,
    ClaudeCode,
}

impl Host {
    pub fn parse(s: &str) -> Option<Host> {
        match s {
            "fixture" => Some(Host::Fixture),
            "claude-code" => Some(Host::ClaudeCode),
            _ => None,
        }
    }

    pub fn file_name(self) -> &'static str {
        match self {
            Host::Fixture => "mcp.json",
            Host::ClaudeCode => ".mcp.json",
        }
    }
}

/// Render the host config. Never resolves a credential value.
pub fn render(host: Host, exe: &str, upstream: Option<&str>, token_file: Option<&str>) -> String {
    let mut args = vec![Value::text("proxy")];
    if let Some(u) = upstream {
        args.push(Value::text("--upstream"));
        args.push(Value::text(u));
    }
    let mut server = vec![("command", Value::text(exe)), ("args", Value::Arr(args))];
    match token_file {
        Some(path) => server.push((
            "env",
            Value::obj(vec![(credential::TOKEN_FILE_VAR, Value::text(path))]),
        )),
        None => server.push((
            "_comment",
            Value::text(
                "export MCTL_TOKEN in the host environment, or re-run init with --token-file PATH",
            ),
        )),
    }
    let doc = match host {
        Host::ClaudeCode => Value::obj(vec![(
            "mcpServers",
            Value::obj(vec![("mctl", Value::obj(server))]),
        )]),
        Host::Fixture => {
            let mut entry = vec![("name", Value::text("mctl"))];
            entry.extend(server);
            Value::obj(vec![("servers", Value::Arr(vec![Value::obj(entry)]))])
        }
    };
    format!("{}\n", doc.to_compact())
}

/// Real host config locations under `home` that `--write` must never touch.
pub fn blocked_locations(home: &Path) -> Vec<PathBuf> {
    vec![
        home.join(".claude"),
        home.join(".claude.json"),
        home.join(".config").join("claude"),
        home.join("Library").join("Application Support").join("Claude"),
        home.join(".cursor"),
    ]
}

/// Write the config into `dir` (which must exist), refusing real host config
/// locations under `home`.
pub fn write_config(
    host: Host,
    dir: &Path,
    content: &str,
    home: Option<&Path>,
) -> Result<PathBuf, String> {
    let canon = std::fs::canonicalize(dir)
        .map_err(|e| format!("config dir {} is not usable: {e}", dir.display()))?;
    if let Some(home) = home {
        let home = std::fs::canonicalize(home).unwrap_or_else(|_| home.to_path_buf());
        for blocked in blocked_locations(&home) {
            let resolved = std::fs::canonicalize(&blocked).unwrap_or(blocked);
            if canon.starts_with(&resolved) {
                return Err(format!(
                    "refusing to write inside real host config location {}",
                    resolved.display()
                ));
            }
        }
    }
    let target = canon.join(host.file_name());
    std::fs::write(&target, content)
        .map_err(|e| format!("cannot write {}: {e}", target.display()))?;
    Ok(target)
}

pub struct DoctorArgs<'a> {
    pub upstream_url: &'a str,
    pub allowlist_path: &'a Path,
    pub timeout: Duration,
}

fn report(out: &mut dyn Write, ok: &mut bool, check: &str, pass: bool, detail: &str) -> io::Result<()> {
    if !pass {
        *ok = false;
    }
    writeln!(out, "{check}: {} {detail}", if pass { "ok" } else { "FAIL" })
}

/// Run the preflight checks. Returns `Ok(true)` when every check passed.
pub fn doctor(
    args: &DoctorArgs,
    cred: &dyn CredentialSource,
    out: &mut dyn Write,
) -> io::Result<bool> {
    let mut ok = true;
    report(out, &mut ok, "version", true, &format!("mctl-runtime {}", crate::VERSION))?;

    let token = match cred.load() {
        Ok(t) => {
            report(out, &mut ok, "credential", true, &format!("source={}", cred.name()))?;
            Some(t)
        }
        Err(e) => {
            let detail = format!("{e} (source={})", cred.name());
            report(out, &mut ok, "credential", false, &detail)?;
            None
        }
    };

    if let Some(path) = cred.path() {
        match credential::file_mode(path) {
            Some(mode) => report(
                out,
                &mut ok,
                "token-file-mode",
                mode & 0o077 == 0,
                &format!("{mode:04o} {}", path.display()),
            )?,
            None => report(
                out,
                &mut ok,
                "token-file-mode",
                false,
                &format!("cannot stat {}", path.display()),
            )?,
        }
    }

    let allowed = match allowlist::load(args.allowlist_path) {
        Ok(list) => {
            report(out, &mut ok, "allowlist", true, &format!("{} tools", list.len()))?;
            Some(list)
        }
        Err(e) => {
            report(out, &mut ok, "allowlist", false, &e.to_string())?;
            None
        }
    };

    match token {
        None => report(
            out,
            &mut ok,
            "upstream",
            false,
            "skipped: credential unavailable, no request made",
        )?,
        Some(token) => {
            let mut upstream = Upstream::new(args.upstream_url, Some(token), args.timeout);
            match upstream.list_tools() {
                Ok(tools) => {
                    let detail = format!("initialize + tools/list, {} tools", tools.len());
                    report(out, &mut ok, "upstream", true, &detail)?;
                    if let Some(list) = &allowed {
                        let names: Vec<&str> = tools
                            .iter()
                            .filter_map(|t| t.get("name").and_then(Value::as_str))
                            .collect();
                        let missing: Vec<&str> = list
                            .iter()
                            .map(String::as_str)
                            .filter(|n| !names.contains(n))
                            .collect();
                        let detail = if missing.is_empty() {
                            "none".to_string()
                        } else {
                            missing.join(", ")
                        };
                        report(
                            out,
                            &mut ok,
                            "allowlist-missing-upstream",
                            missing.is_empty(),
                            &detail,
                        )?;
                    }
                }
                Err(e) => {
                    let detail = format!("{}: {}", e.reason_code(), e.detail());
                    report(out, &mut ok, "upstream", false, &detail)?;
                }
            }
        }
    }
    Ok(ok)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_shapes() {
        let c = render(Host::ClaudeCode, "/bin/mctl-runtime", None, Some("/tmp/t"));
        assert_eq!(
            c,
            "{\"mcpServers\":{\"mctl\":{\"command\":\"/bin/mctl-runtime\",\"args\":[\"proxy\"],\"env\":{\"MCTL_RUNTIME_TOKEN_FILE\":\"/tmp/t\"}}}}\n"
        );
        let f = render(Host::Fixture, "/bin/x", Some("http://h/mcp"), None);
        assert!(f.starts_with("{\"servers\":[{\"name\":\"mctl\""));
        assert!(f.contains("MCTL_TOKEN"));
        assert!(f.contains("--upstream"));
    }

    #[test]
    fn host_parse() {
        assert_eq!(Host::parse("fixture"), Some(Host::Fixture));
        assert_eq!(Host::parse("claude-code"), Some(Host::ClaudeCode));
        assert_eq!(Host::parse("cursor"), None);
    }
}
