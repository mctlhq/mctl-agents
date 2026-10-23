//! UUIDv4 generation without external crates.

use std::sync::atomic::{AtomicU64, Ordering};

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// Format 16 random bytes as a UUIDv4 string (sets version and variant bits).
pub fn from_bytes(mut b: [u8; 16]) -> String {
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let hex: Vec<String> = b.iter().map(|x| format!("{x:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        hex[0..4].concat(),
        hex[4..6].concat(),
        hex[6..8].concat(),
        hex[8..10].concat(),
        hex[10..16].concat()
    )
}

#[cfg(unix)]
fn urandom() -> Option<[u8; 16]> {
    use std::io::Read;
    let mut buf = [0u8; 16];
    let mut f = std::fs::File::open("/dev/urandom").ok()?;
    f.read_exact(&mut buf).ok()?;
    Some(buf)
}

#[cfg(not(unix))]
fn urandom() -> Option<[u8; 16]> {
    None
}

fn random_bytes() -> [u8; 16] {
    if let Some(b) = urandom() {
        return b;
    }
    // Fallback (non-unix or unreadable /dev/urandom): hash time, pid and a counter.
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let seed = format!(
        "{nanos}-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    );
    let digest = crate::sha256::sha256(seed.as_bytes());
    let mut buf = [0u8; 16];
    buf.copy_from_slice(&digest[..16]);
    buf
}

/// A fresh UUIDv4.
pub fn v4() -> String {
    from_bytes(random_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_and_variant_bits() {
        let u = from_bytes([0xff; 16]);
        assert_eq!(u, "ffffffff-ffff-4fff-bfff-ffffffffffff");
        let u = from_bytes([0x00; 16]);
        assert_eq!(u, "00000000-0000-4000-8000-000000000000");
    }

    #[test]
    fn v4_shape_and_uniqueness() {
        let a = v4();
        let b = v4();
        assert_ne!(a, b);
        assert_eq!(a.len(), 36);
        assert_eq!(a.as_bytes()[14], b'4');
        assert!(matches!(a.as_bytes()[19], b'8' | b'9' | b'a' | b'b'));
    }
}
