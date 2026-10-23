//! T9 and T10: init dry run and guarded write, token file mode.

mod common;

#[test]
fn t9_init_dry_run() {
    common::scenarios::t9_init_dry_run();
}

#[test]
fn t10_token_file_mode() {
    common::scenarios::t10_token_file_mode();
}
