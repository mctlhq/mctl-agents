//! #746 slice 3b: https upstream over rustls, fully offline. The server
//! certificate is trusted only through a test-built root store holding a
//! checked-in fixture CA. Compiled only with `--features tls`.
#![cfg(feature = "tls")]

mod common;

use std::sync::Arc;
use std::time::Duration;

use mctl_runtime::credential::Secret;
use mctl_runtime::tls::client_config_with_roots;
use mctl_runtime::upstream::Upstream;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use rustls::{ClientConfig, RootCertStore, ServerConfig};

use common::fake_upstream::{FakeUpstream, Knobs, ALL_TOOLS};

const CA_PEM: &[u8] = include_bytes!("../fixtures/tls/ca.pem");
const OTHER_CA_PEM: &[u8] = include_bytes!("../fixtures/tls/other-ca.pem");
const SERVER_PEM: &[u8] = include_bytes!("../fixtures/tls/server.pem");
const SERVER_KEY_PEM: &[u8] = include_bytes!("../fixtures/tls/server.key.pem");

fn server_config() -> Arc<ServerConfig> {
    let chain: Vec<CertificateDer<'static>> = CertificateDer::pem_slice_iter(SERVER_PEM)
        .collect::<Result<Vec<_>, _>>()
        .expect("server chain");
    let key = PrivateKeyDer::from_pem_slice(SERVER_KEY_PEM).expect("server key");
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = ServerConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .expect("protocol versions")
        .with_no_client_auth()
        .with_single_cert(chain, key)
        .expect("server cert");
    Arc::new(config)
}

/// The production builder over a store holding only `ca_pem`.
fn client_with(ca_pem: &[u8]) -> Arc<ClientConfig> {
    let mut roots = RootCertStore::empty();
    for cert in CertificateDer::pem_slice_iter(ca_pem) {
        roots.add(cert.expect("ca pem")).expect("add root");
    }
    client_config_with_roots(roots).expect("client config")
}

fn token() -> Option<Secret> {
    Some(Secret::new("tls-test-token".to_string()))
}

#[test]
fn t2_tls_initialize_and_tools_list() {
    let fake = FakeUpstream::start_tls(Knobs::default(), server_config());
    assert!(fake.url().starts_with("https://127.0.0.1:"));
    let mut up = Upstream::new(&fake.url(), token(), Duration::from_secs(5))
        .with_tls_config(client_with(CA_PEM));
    let tools = up.list_tools().expect("tools/list over tls");
    assert_eq!(tools.len(), ALL_TOOLS.len());
    assert_eq!(up.session_id(), Some(common::fake_upstream::SESSION_ID));
    assert_eq!(fake.count("initialize"), 1);
    assert_eq!(fake.count("tools/list"), 1);
}

#[test]
fn t3_rejects_untrusted_server_cert() {
    let fake = FakeUpstream::start_tls(Knobs::default(), server_config());
    let mut up = Upstream::new(&fake.url(), token(), Duration::from_secs(5))
        .with_tls_config(client_with(OTHER_CA_PEM));
    let err = up.list_tools().unwrap_err();
    assert_eq!(err.reason_code(), "provider-unavailable");
    // No HTTP request reached the server, so no bearer token was sent.
    assert_eq!(fake.total(), 0);
}

#[test]
fn t3b_bundled_roots_reject_test_cert() {
    let fake = FakeUpstream::start_tls(Knobs::default(), server_config());
    let mut up = Upstream::new(&fake.url(), token(), Duration::from_secs(5));
    let err = up.list_tools().unwrap_err();
    assert_eq!(err.reason_code(), "provider-unavailable");
    assert_eq!(fake.total(), 0);
}
