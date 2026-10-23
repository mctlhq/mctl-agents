// Shared by every integration test crate; each one uses a different subset.
#![allow(dead_code)]

pub mod fake_upstream;
pub mod host;
pub mod scenarios;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

/// Sentinel token used to prove the credential never leaks.
pub const SENTINEL: &str = "mctl-runtime-sentinel-746";

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// Path of the built `mctl-runtime` binary.
pub fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_mctl-runtime")
}

pub fn manifest_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// A uniquely named temp dir removed on drop.
pub struct TempDir {
    path: PathBuf,
}

impl TempDir {
    pub fn new(tag: &str) -> TempDir {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = std::env::temp_dir().join(format!(
            "mctl-rt-{tag}-{}-{}-{nanos}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&path).expect("create temp dir");
        // Canonical so comparisons survive /var -> /private/var on macOS.
        let path = std::fs::canonicalize(&path).expect("canonicalize temp dir");
        TempDir { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn join(&self, name: &str) -> PathBuf {
        self.path.join(name)
    }

    /// Sorted entry names.
    pub fn listing(&self) -> Vec<String> {
        let mut names: Vec<String> = std::fs::read_dir(&self.path)
            .expect("read temp dir")
            .map(|e| e.expect("dir entry").file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

/// Write a token file with the given unix mode.
pub fn write_token_file(dir: &TempDir, name: &str, mode: u32, token: &str) -> PathBuf {
    use std::os::unix::fs::PermissionsExt;
    let path = dir.join(name);
    std::fs::write(&path, token).expect("write token file");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).expect("chmod");
    path
}

/// Allowlist used by most scenarios; `ghost_tool` is allowlisted but absent upstream.
pub const ALLOWLIST_TEXT: &str =
    "tools = [\"mctl_whoami\", \"deny_tool\", \"approval_tool\", \"slow_tool\", \"ghost_tool\"]\n";

/// Allowlist where every tool exists upstream.
pub const ALLOWLIST_PRESENT_TEXT: &str =
    "tools = [\"mctl_whoami\", \"deny_tool\", \"approval_tool\", \"slow_tool\"]\n";

pub fn write_file(dir: &TempDir, name: &str, text: &str) -> PathBuf {
    let path = dir.join(name);
    std::fs::write(&path, text).expect("write file");
    path
}
