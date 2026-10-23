//! TLS client configuration, compiled only with the `tls` feature.
//!
//! The https upstream path is not wired yet (slice 3b); this module pins the
//! provider and trust roots that path will use, so the dependency is built,
//! linted and audited before any code depends on it.

use std::sync::Arc;

use rustls::{ClientConfig, RootCertStore};

/// Client config with the ring provider, rustls' safe default protocol
/// versions and the bundled Mozilla roots from `webpki-roots`.
pub fn client_config() -> Result<Arc<ClientConfig>, rustls::Error> {
    let roots = RootCertStore {
        roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
    };
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()?
        .with_root_certificates(roots)
        .with_no_client_auth();
    Ok(Arc::new(config))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_with_bundled_roots_offline() {
        assert!(!webpki_roots::TLS_SERVER_ROOTS.is_empty());
        let config = client_config().expect("client config");
        assert!(config.alpn_protocols.is_empty());
    }
}
