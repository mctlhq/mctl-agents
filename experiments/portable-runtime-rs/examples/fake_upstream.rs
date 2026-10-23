//! Standalone fake upstream for the parity and bench scripts (#746 slice 3).
//!
//! Reuses the test fake verbatim so the tool fixtures are never forked. Prints
//! its URL on one stdout line, then blocks until stdin reaches EOF (SIGTERM
//! also ends the process).

use std::io::Read;

#[path = "../tests/common/fake_upstream.rs"]
#[allow(dead_code)]
mod fake_upstream;

fn main() {
    let upstream = fake_upstream::FakeUpstream::start(fake_upstream::Knobs::default());
    println!("{}", upstream.url());
    let mut sink = Vec::new();
    let _ = std::io::stdin().read_to_end(&mut sink);
}
