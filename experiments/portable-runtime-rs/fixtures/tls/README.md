# TLS test fixtures (test-only)

These files exist only for `tests/tls.rs` (`cargo test --features tls`). They are not used at runtime and never trusted by
the shipped binary, which trusts only the bundled `webpki-roots`.

- `ca.pem`: self-signed test CA (ECDSA P-256), trusted only through a test-built `RootCertStore`.
- `server.pem`, `server.key.pem`: leaf signed by `ca.pem` (CA:FALSE, SAN `IP:127.0.0.1`, `DNS:localhost`, serverAuth) and its
  PKCS#8 key. The key is public and protects nothing; secret scanners may flag it.
- `other-ca.pem`: an unrelated self-signed CA for the "untrusted certificate" test.

Validity is 100 years. Regenerate with `./gen.sh` (needs `openssl`); the CA private keys are discarded.
