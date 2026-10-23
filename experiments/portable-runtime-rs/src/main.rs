fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

fn main() {
    println!("mctl-runtime {}", version());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_is_not_empty() {
        assert!(!version().is_empty());
    }
}
