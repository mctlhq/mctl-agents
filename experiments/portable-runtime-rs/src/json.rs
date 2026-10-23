//! Minimal JSON value, parser and serializers.
//!
//! Objects keep insertion order and numbers keep their source text so that a
//! parse followed by `to_compact` reproduces an already-compact document
//! byte for byte.

use std::fmt::Write as _;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    /// Number kept as its source text.
    Num(String),
    Str(String),
    Arr(Vec<Value>),
    Obj(Vec<(String, Value)>),
}

impl Value {
    pub fn text(s: &str) -> Value {
        Value::Str(s.to_string())
    }

    pub fn num(n: u64) -> Value {
        Value::Num(n.to_string())
    }

    pub fn obj(pairs: Vec<(&str, Value)>) -> Value {
        Value::Obj(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Obj(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Value]> {
        match self {
            Value::Arr(a) => Some(a),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }

    /// Compact form: insertion key order, raw number text, non-ASCII kept.
    pub fn to_compact(&self) -> String {
        let mut out = String::new();
        self.write(&mut out, false);
        out
    }

    /// Canonical form matching Python
    /// `json.dumps(v, sort_keys=True, separators=(",", ":"))` (ASCII-escaped).
    pub fn to_canonical(&self) -> String {
        let mut out = String::new();
        self.write(&mut out, true);
        out
    }

    fn write(&self, out: &mut String, canonical: bool) {
        match self {
            Value::Null => out.push_str("null"),
            Value::Bool(true) => out.push_str("true"),
            Value::Bool(false) => out.push_str("false"),
            Value::Num(t) => out.push_str(t),
            Value::Str(s) => write_string(out, s, canonical),
            Value::Arr(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    item.write(out, canonical);
                }
                out.push(']');
            }
            Value::Obj(pairs) => {
                let mut refs: Vec<&(String, Value)> = pairs.iter().collect();
                if canonical {
                    refs.sort_by(|a, b| a.0.cmp(&b.0));
                }
                out.push('{');
                for (i, (k, v)) in refs.into_iter().map(|p| (&p.0, &p.1)).enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_string(out, k, canonical);
                    out.push(':');
                    v.write(out, canonical);
                }
                out.push('}');
            }
        }
    }
}

fn write_string(out: &mut String, s: &str, ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c if ascii && !(0x20..=0x7e).contains(&(c as u32)) => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf).iter() {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Parse a JSON document.
pub fn parse(text: &str) -> Result<Value, String> {
    let mut p = Parser {
        b: text.as_bytes(),
        i: 0,
    };
    p.ws();
    let v = p.value(0)?;
    p.ws();
    if p.i != p.b.len() {
        return Err(format!("trailing characters at byte {}", p.i));
    }
    Ok(v)
}

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}

impl Parser<'_> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn value(&mut self, depth: usize) -> Result<Value, String> {
        if depth > 128 {
            return Err("nesting too deep".to_string());
        }
        match self.peek() {
            None => Err("unexpected end of input".to_string()),
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
            Some(b'"') => Ok(Value::Str(self.string()?)),
            Some(b't') => self.literal("true", Value::Bool(true)),
            Some(b'f') => self.literal("false", Value::Bool(false)),
            Some(b'n') => self.literal("null", Value::Null),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            Some(c) => Err(format!("unexpected byte {c} at {}", self.i)),
        }
    }

    fn literal(&mut self, word: &str, v: Value) -> Result<Value, String> {
        if self.b[self.i..].starts_with(word.as_bytes()) {
            self.i += word.len();
            Ok(v)
        } else {
            Err(format!("invalid literal at {}", self.i))
        }
    }

    fn number(&mut self) -> Result<Value, String> {
        let start = self.i;
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        if !self.peek().is_some_and(|c| c.is_ascii_digit()) {
            return Err(format!("invalid number at {start}"));
        }
        while self
            .peek()
            .is_some_and(|c| c.is_ascii_digit() || matches!(c, b'.' | b'e' | b'E' | b'+' | b'-'))
        {
            self.i += 1;
        }
        let text = std::str::from_utf8(&self.b[start..self.i]).map_err(|e| e.to_string())?;
        if text.parse::<f64>().is_err() {
            return Err(format!("invalid number {text}"));
        }
        Ok(Value::Num(text.to_string()))
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let slice = self
            .b
            .get(self.i..self.i + 4)
            .ok_or_else(|| "truncated \\u escape".to_string())?;
        let text = std::str::from_utf8(slice).map_err(|e| e.to_string())?;
        if !text.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Err("invalid \\u escape".to_string());
        }
        let v = u32::from_str_radix(text, 16).map_err(|e| e.to_string())?;
        self.i += 4;
        Ok(v)
    }

    fn string(&mut self) -> Result<String, String> {
        // Caller guarantees the current byte is the opening quote.
        self.i += 1;
        let mut out: Vec<u8> = Vec::new();
        loop {
            let c = self
                .peek()
                .ok_or_else(|| "unterminated string".to_string())?;
            self.i += 1;
            match c {
                b'"' => break,
                b'\\' => {
                    let e = self
                        .peek()
                        .ok_or_else(|| "unterminated escape".to_string())?;
                    self.i += 1;
                    match e {
                        b'"' => out.push(b'"'),
                        b'\\' => out.push(b'\\'),
                        b'/' => out.push(b'/'),
                        b'b' => out.push(8),
                        b'f' => out.push(12),
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'u' => {
                            let hi = self.hex4()?;
                            let cp = if (0xD800..=0xDBFF).contains(&hi) {
                                if self.peek() != Some(b'\\') || self.b.get(self.i + 1) != Some(&b'u')
                                {
                                    return Err("lone high surrogate".to_string());
                                }
                                self.i += 2;
                                let lo = self.hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&lo) {
                                    return Err("invalid low surrogate".to_string());
                                }
                                0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)
                            } else if (0xDC00..=0xDFFF).contains(&hi) {
                                return Err("lone low surrogate".to_string());
                            } else {
                                hi
                            };
                            let ch = char::from_u32(cp)
                                .ok_or_else(|| "invalid code point".to_string())?;
                            let mut buf = [0u8; 4];
                            out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                        }
                        other => return Err(format!("invalid escape \\{}", other as char)),
                    }
                }
                c if c < 0x20 => return Err("control character in string".to_string()),
                c => out.push(c),
            }
        }
        String::from_utf8(out).map_err(|e| e.to_string())
    }

    fn array(&mut self, depth: usize) -> Result<Value, String> {
        self.i += 1;
        let mut items = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.i += 1;
            return Ok(Value::Arr(items));
        }
        loop {
            self.ws();
            items.push(self.value(depth + 1)?);
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b']') => {
                    self.i += 1;
                    break;
                }
                _ => return Err(format!("expected , or ] at {}", self.i)),
            }
        }
        Ok(Value::Arr(items))
    }

    fn object(&mut self, depth: usize) -> Result<Value, String> {
        self.i += 1;
        let mut pairs = Vec::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.i += 1;
            return Ok(Value::Obj(pairs));
        }
        loop {
            self.ws();
            if self.peek() != Some(b'"') {
                return Err(format!("expected object key at {}", self.i));
            }
            let key = self.string()?;
            self.ws();
            if self.peek() != Some(b':') {
                return Err(format!("expected : at {}", self.i));
            }
            self.i += 1;
            self.ws();
            let v = self.value(depth + 1)?;
            pairs.push((key, v));
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b'}') => {
                    self.i += 1;
                    break;
                }
                _ => return Err(format!("expected , or }} at {}", self.i)),
            }
        }
        Ok(Value::Obj(pairs))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_preserves_order_and_number_text() {
        let doc = r#"{"z":1.50,"a":[1e3,-0.0,true,null],"m":{"y":"x","b":2}}"#;
        let v = parse(doc).unwrap();
        assert_eq!(v.to_compact(), doc);
    }

    #[test]
    fn whitespace_is_dropped() {
        let v = parse(" { \"a\" : [ 1 , 2 ] } ").unwrap();
        assert_eq!(v.to_compact(), r#"{"a":[1,2]}"#);
    }

    #[test]
    fn canonical_sorts_and_escapes_ascii() {
        let v = parse(r#"{"b":2,"a":{"z":1,"y":"argvalue-é"}}"#).unwrap();
        assert_eq!(
            v.to_canonical(),
            r#"{"a":{"y":"argvalue-\u00e9","z":1},"b":2}"#
        );
        let astral = parse("\"\\ud83d\\ude00\"").unwrap();
        assert_eq!(astral.to_canonical(), "\"\\ud83d\\ude00\"");
        assert_eq!(astral.to_compact(), "\"\u{1F600}\"");
    }

    #[test]
    fn control_and_escape_forms() {
        let v = parse(r#""a\n\t\"\\\u0001\/""#).unwrap();
        assert_eq!(v.as_str().unwrap(), "a\n\t\"\\\u{1}/");
        assert_eq!(v.to_compact(), r#""a\n\t\"\\\u0001/""#);
    }

    #[test]
    fn errors() {
        for bad in [
            "",
            "{",
            "[1,]",
            "{\"a\"}",
            "\"abc",
            "tru",
            "1 2",
            "\"\\ud800\"",
            "\"\\u12\"",
            "-",
        ] {
            assert!(parse(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn empty_containers() {
        assert_eq!(parse("{}").unwrap(), Value::Obj(vec![]));
        assert_eq!(parse("[ ]").unwrap(), Value::Arr(vec![]));
    }

    #[test]
    fn args_digest_constant_matches_python() {
        let v = parse(r#"{"b":2,"a":{"z":1,"y":"argvalue-é"}}"#).unwrap();
        assert_eq!(
            format!(
                "sha256:{}",
                crate::sha256::sha256_hex(v.to_canonical().as_bytes())
            ),
            "sha256:842613ef2de7aac81c9aff148236f275a7d3e85537809cb0b6876e256e3f96fc"
        );
    }
}
