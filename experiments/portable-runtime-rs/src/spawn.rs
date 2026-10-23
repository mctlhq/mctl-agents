//! The only place a child process is built. Credentials never reach a child:
//! both credential variables are removed from its environment and callers add
//! non-secret arguments only.

use std::path::Path;
use std::process::Command;

/// Environment variables scrubbed from every child.
pub const SCRUBBED_ENV: [&str; 2] = ["MCTL_TOKEN", "MCTL_RUNTIME_TOKEN_FILE"];

pub fn command(program: &Path) -> Command {
    let mut cmd = Command::new(program);
    for key in SCRUBBED_ENV {
        cmd.env_remove(key);
    }
    cmd
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credential_vars_are_removed() {
        let cmd = command(Path::new("true"));
        let removed: Vec<String> = cmd
            .get_envs()
            .filter(|(_, v)| v.is_none())
            .map(|(k, _)| k.to_string_lossy().into_owned())
            .collect();
        for key in SCRUBBED_ENV {
            assert!(removed.iter().any(|r| r == key), "{key} not scrubbed");
        }
    }
}
