//! T7: no credential in argv, env, config or stderr.

mod common;

#[test]
fn t7_no_credential_in_argv_env_or_config() {
    common::scenarios::t7_no_credential_in_argv_env_or_config();
}
