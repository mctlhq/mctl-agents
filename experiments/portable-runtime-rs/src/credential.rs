//! Credential sources. The bearer token is read from a `0600` file or from
//! `MCTL_TOKEN`; there is deliberately no CLI flag that accepts a token.

use std::fmt;
use std::io;
use std::path::{Path, PathBuf};

pub const TOKEN_FILE_VAR: &str = "MCTL_RUNTIME_TOKEN_FILE";
pub const TOKEN_ENV_VAR: &str = "MCTL_TOKEN";

/// A bearer token. `Debug` redacts it and there is no `Display`.
pub struct Secret(String);

impl Secret {
    pub fn new(value: String) -> Secret {
        Secret(value)
    }

    /// The raw token. Only the upstream client should call this.
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Secret(<redacted>)")
    }
}

#[derive(Debug)]
pub enum CredentialError {
    Absent,
    Unsupported(&'static str),
    InsecureMode { path: PathBuf, mode: u32 },
    Unreadable(PathBuf, io::Error),
    Empty(PathBuf),
    InvalidToken,
}

impl fmt::Display for CredentialError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CredentialError::Absent => write!(f, "credential absent"),
            CredentialError::Unsupported(what) => write!(f, "credential source unsupported: {what}"),
            CredentialError::InsecureMode { path, mode } => write!(
                f,
                "token file {} has mode {:04o}; group/other permission bits must be clear (expected 0600), refusing to read it",
                path.display(),
                mode
            ),
            CredentialError::Unreadable(path, e) => {
                write!(f, "token file {} is unreadable: {e}", path.display())
            }
            CredentialError::Empty(path) => write!(f, "token file {} is empty", path.display()),
            CredentialError::InvalidToken => {
                write!(f, "token contains characters outside visible ASCII (0x21..0x7e)")
            }
        }
    }
}

impl std::error::Error for CredentialError {}

pub trait CredentialSource {
    fn name(&self) -> &'static str;
    fn load(&self) -> Result<Secret, CredentialError>;
    /// Token file path, when the source is a file.
    fn path(&self) -> Option<&Path> {
        None
    }
}

/// Token read from the file named by `MCTL_RUNTIME_TOKEN_FILE`.
pub struct TokenFile(PathBuf);

impl TokenFile {
    pub fn new(path: PathBuf) -> TokenFile {
        TokenFile(path)
    }
}

#[cfg(unix)]
fn read_checked(path: &Path) -> Result<Secret, CredentialError> {
    use std::os::unix::fs::PermissionsExt;
    let meta =
        std::fs::metadata(path).map_err(|e| CredentialError::Unreadable(path.to_path_buf(), e))?;
    let mode = meta.permissions().mode() & 0o777;
    if mode & 0o077 != 0 {
        return Err(CredentialError::InsecureMode {
            path: path.to_path_buf(),
            mode,
        });
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| CredentialError::Unreadable(path.to_path_buf(), e))?;
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return Err(CredentialError::Empty(path.to_path_buf()));
    }
    check_header_safe(trimmed)?;
    Ok(Secret::new(trimmed.to_string()))
}

#[cfg(not(unix))]
fn read_checked(_path: &Path) -> Result<Secret, CredentialError> {
    Err(CredentialError::Unsupported(
        "token file permission check requires a unix host",
    ))
}

/// A token is interpolated into an HTTP header: only visible ASCII is allowed,
/// which rules out CR/LF header injection.
fn check_header_safe(token: &str) -> Result<(), CredentialError> {
    if token.bytes().all(|b| (0x21..=0x7e).contains(&b)) {
        Ok(())
    } else {
        Err(CredentialError::InvalidToken)
    }
}

/// Permission bits of a file (unix only).
#[cfg(unix)]
pub fn file_mode(path: &Path) -> Option<u32> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .ok()
        .map(|m| m.permissions().mode() & 0o777)
}

#[cfg(not(unix))]
pub fn file_mode(_path: &Path) -> Option<u32> {
    None
}

impl CredentialSource for TokenFile {
    fn name(&self) -> &'static str {
        "token-file"
    }

    fn load(&self) -> Result<Secret, CredentialError> {
        read_checked(&self.0)
    }

    fn path(&self) -> Option<&Path> {
        Some(&self.0)
    }
}

/// Token taken from `MCTL_TOKEN`.
pub struct EnvVar(String);

impl EnvVar {
    pub fn new(value: String) -> EnvVar {
        EnvVar(value)
    }
}

impl CredentialSource for EnvVar {
    fn name(&self) -> &'static str {
        "env"
    }

    fn load(&self) -> Result<Secret, CredentialError> {
        if self.0.is_empty() {
            Err(CredentialError::Absent)
        } else {
            check_header_safe(&self.0)?;
            Ok(Secret::new(self.0.clone()))
        }
    }
}

/// OS keychain. Not implemented in this spike.
pub struct Keychain;

impl CredentialSource for Keychain {
    fn name(&self) -> &'static str {
        "keychain"
    }

    fn load(&self) -> Result<Secret, CredentialError> {
        Err(CredentialError::Unsupported("keychain"))
    }
}

/// Loopback OAuth flow. Not implemented in this spike.
pub struct Loopback;

impl CredentialSource for Loopback {
    fn name(&self) -> &'static str {
        "loopback"
    }

    fn load(&self) -> Result<Secret, CredentialError> {
        Err(CredentialError::Unsupported("loopback"))
    }
}

/// No credential source configured.
pub struct NoCredential;

impl CredentialSource for NoCredential {
    fn name(&self) -> &'static str {
        "none"
    }

    fn load(&self) -> Result<Secret, CredentialError> {
        Err(CredentialError::Absent)
    }
}

/// Pick a source: `MCTL_RUNTIME_TOKEN_FILE` wins, then `MCTL_TOKEN`.
/// Environment access is injected so tests never mutate the process env.
pub fn resolve(env: &dyn Fn(&str) -> Option<String>) -> Box<dyn CredentialSource> {
    if let Some(path) = env(TOKEN_FILE_VAR).filter(|v| !v.is_empty()) {
        return Box::new(TokenFile::new(PathBuf::from(path)));
    }
    if let Some(token) = env(TOKEN_ENV_VAR).filter(|v| !v.is_empty()) {
        return Box::new(EnvVar::new(token));
    }
    Box::new(NoCredential)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |k| map.get(k).cloned()
    }

    #[test]
    fn env_source() {
        let env = env_of(&[("MCTL_TOKEN", "tok")]);
        let src = resolve(&env);
        assert_eq!(src.name(), "env");
        assert_eq!(src.load().unwrap().expose(), "tok");
    }

    #[test]
    fn absent_when_nothing_set_or_empty() {
        for pairs in [vec![], vec![("MCTL_TOKEN", "")]] {
            let env = env_of(&pairs);
            let src = resolve(&env);
            assert!(matches!(src.load(), Err(CredentialError::Absent)));
        }
    }

    #[test]
    fn stubs_are_unsupported() {
        assert!(matches!(
            Keychain.load(),
            Err(CredentialError::Unsupported(_))
        ));
        assert!(matches!(
            Loopback.load(),
            Err(CredentialError::Unsupported(_))
        ));
    }

    #[test]
    fn secret_debug_is_redacted() {
        let s = Secret::new("hunter2".to_string());
        assert_eq!(format!("{s:?}"), "Secret(<redacted>)");
    }

    #[cfg(unix)]
    mod unix {
        use super::*;
        use std::os::unix::fs::PermissionsExt;

        fn temp_file(tag: &str, mode: u32, body: &str) -> PathBuf {
            let path = std::env::temp_dir().join(format!(
                "mctl-runtime-cred-{}-{tag}",
                std::process::id()
            ));
            std::fs::write(&path, body).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
            path
        }

        #[test]
        fn file_0600_is_accepted_and_trimmed() {
            let path = temp_file("ok", 0o600, "  file-token\n");
            let src = TokenFile::new(path.clone());
            assert_eq!(src.load().unwrap().expose(), "file-token");
            std::fs::remove_file(path).unwrap();
        }

        #[test]
        fn file_0644_is_refused_without_reading() {
            let path = temp_file("bad", 0o644, "never-read");
            let src = TokenFile::new(path.clone());
            let err = src.load().unwrap_err();
            let text = err.to_string();
            assert!(matches!(err, CredentialError::InsecureMode { .. }));
            assert!(text.contains("0644"));
            assert!(text.contains(&path.display().to_string()));
            assert!(!text.contains("never-read"));
            std::fs::remove_file(path).unwrap();
        }

        #[test]
        fn file_wins_over_env() {
            let path = temp_file("prec", 0o600, "from-file");
            let env = env_of(&[
                ("MCTL_RUNTIME_TOKEN_FILE", path.to_str().unwrap()),
                ("MCTL_TOKEN", "from-env"),
            ]);
            let src = resolve(&env);
            assert_eq!(src.name(), "token-file");
            assert_eq!(src.load().unwrap().expose(), "from-file");
            std::fs::remove_file(path).unwrap();
        }

        #[test]
        fn empty_file_is_an_error() {
            let path = temp_file("empty", 0o600, "\n");
            let src = TokenFile::new(path.clone());
            assert!(matches!(src.load(), Err(CredentialError::Empty(_))));
            std::fs::remove_file(path).unwrap();
        }
    }
}

#[cfg(test)]
mod header_safety_tests {
    use super::*;

    #[test]
    fn env_token_with_newline_is_rejected() {
        let e = EnvVar::new("abc\r\nX-Foo: bar".to_string());
        assert!(matches!(e.load(), Err(CredentialError::InvalidToken)));
    }

    #[test]
    fn plain_token_is_accepted() {
        assert!(EnvVar::new("abc.DEF-123".to_string()).load().is_ok());
    }
}
