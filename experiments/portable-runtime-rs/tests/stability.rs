//! Runs the T2-T8 scenarios three times in sequence, so a single `cargo test`
//! shows they are repeatable without changing the CI workflow.

mod common;

use common::scenarios;

#[test]
fn t2_to_t8_three_consecutive_passes() {
    for _ in 0..3 {
        scenarios::t2_allowlist();
        scenarios::t3_fail_closed();
        scenarios::t4_remote_refusal_preserved();
        scenarios::t5_unavailable_and_timeout();
        scenarios::t6_token_absent_or_rejected();
        scenarios::t7_no_credential_in_argv_env_or_config();
        scenarios::t8_correlation();
    }
}
