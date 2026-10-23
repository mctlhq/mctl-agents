//! T2-T6: allowlist, fail closed, remote refusals, upstream failures, credentials.

mod common;

use common::scenarios;

#[test]
fn t2_allowlist() {
    scenarios::t2_allowlist();
}

#[test]
fn t3_fail_closed() {
    scenarios::t3_fail_closed();
}

#[test]
fn t4_remote_refusal_preserved() {
    scenarios::t4_remote_refusal_preserved();
}

#[test]
fn t5_unavailable_and_timeout() {
    scenarios::t5_unavailable_and_timeout();
}

#[test]
fn t6_token_absent_or_rejected() {
    scenarios::t6_token_absent_or_rejected();
}
