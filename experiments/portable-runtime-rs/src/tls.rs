//! TLS client configuration, compiled only with the `tls` feature.
//!
//! `http::post_with` wraps https upstreams in these configs. This module pins
//! the provider and the trust roots.

use std::sync::Arc;

use rustls::{ClientConfig, RootCertStore};

/// Client config with the ring provider, rustls' safe default protocol
/// versions and the bundled Mozilla roots from `webpki-roots`.
pub fn client_config() -> Result<Arc<ClientConfig>, rustls::Error> {
    client_config_with_roots(RootCertStore {
        roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
    })
}

/// Same builder as `client_config()` over an explicit root store (tests pass a
/// store holding only a test CA).
pub fn client_config_with_roots(roots: RootCertStore) -> Result<Arc<ClientConfig>, rustls::Error> {
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
