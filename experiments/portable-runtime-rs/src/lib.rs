//! EXPERIMENTAL spike (#746). Std-only building blocks for the portable runtime.

pub mod allowlist;
pub mod bridge;
pub mod correlation;
pub mod credential;
pub mod hostcfg;
pub mod http;
pub mod json;
pub mod sha256;
pub mod spawn;
#[cfg(feature = "tls")]
pub mod tls;
pub mod upstream;
pub mod uuid;

/// Crate version, reported in `serverInfo` and `doctor`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
