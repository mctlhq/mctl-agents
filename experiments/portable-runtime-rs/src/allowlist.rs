//! Allowlist loader for the TOML subset `tools = ["a", "b"]`.

use std::fmt;
use std::path::Path;

#[derive(Debug)]
pub enum AllowlistError {
    Missing(String),
    Unreadable(String),
    Empty,
    Invalid(String),
}

impl fmt::Display for AllowlistError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AllowlistError::Missing(p) => write!(f, "allowlist file not found: {p}"),
            AllowlistError::Unreadable(m) => write!(f, "allowlist file unreadable: {m}"),
            AllowlistError::Empty => write!(f, "allowlist is empty"),
            AllowlistError::Invalid(m) => write!(f, "allowlist is invalid: {m}"),
        }
    }
}

impl std::error::Error for AllowlistError {}

/// Remove `#` comments that are outside of quoted strings.
fn strip_comments(text: &str) -> String {
    let mut out = String::new();
    let mut in_str = false;
    let mut escaped = false;
    let mut in_comment = false;
    for c in text.chars() {
        if in_comment {
            if c == '\n' {
                in_comment = false;
                out.push(c);
            }
            continue;
        }
        if in_str {
            out.push(c);
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                in_str = false;
            }
            continue;
        }
        match c {
            '#' => in_comment = true,
            '"' => {
                in_str = true;
                out.push(c);
            }
            _ => out.push(c),
        }
    }
    out
}

fn invalid(msg: &str) -> AllowlistError {
    AllowlistError::Invalid(msg.to_string())
}

/// Parse allowlist text into tool names.
pub fn parse(text: &str) -> Result<Vec<String>, AllowlistError> {
    let cleaned = strip_comments(text);
    let rest = cleaned.trim();
    let rest = rest
        .strip_prefix("tools")
        .ok_or_else(|| invalid("expected `tools = [...]`"))?
        .trim_start();
    let rest = rest
        .strip_prefix('=')
        .ok_or_else(|| invalid("expected `=` after tools"))?
        .trim_start();
    let rest = rest
        .strip_prefix('[')
        .ok_or_else(|| invalid("expected `[` after `=`"))?;
    let mut tools = Vec::new();
    let mut chars = rest.chars().peekable();
    loop {
        while chars.peek().is_some_and(|c| c.is_whitespace()) {
            chars.next();
        }
        match chars.next() {
            Some(']') => break,
            Some('"') => {
                let mut name = String::new();
                loop {
                    match chars.next() {
                        Some('"') => break,
                        Some('\\') => match chars.next() {
                            Some(c @ ('"' | '\\')) => name.push(c),
                            _ => return Err(invalid("bad escape in string")),
                        },
                        Some(c) => name.push(c),
                        None => return Err(invalid("unterminated string")),
                    }
                }
                tools.push(name);
                while chars.peek().is_some_and(|c| c.is_whitespace()) {
                    chars.next();
                }
                match chars.next() {
                    Some(',') => {}
                    Some(']') => break,
                    _ => return Err(invalid("expected `,` or `]`")),
                }
            }
            _ => return Err(invalid("expected a quoted tool name")),
        }
    }
    if chars.any(|c| !c.is_whitespace()) {
        return Err(invalid("unexpected content after `]`"));
    }
    if tools.is_empty() || tools.iter().all(|t| t.trim().is_empty()) {
        return Err(AllowlistError::Empty);
    }
    Ok(tools)
}

/// Load and parse an allowlist file.
pub fn load(path: &Path) -> Result<Vec<String>, AllowlistError> {
    match std::fs::read_to_string(path) {
        Ok(text) => parse(&text),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            Err(AllowlistError::Missing(path.display().to_string()))
        }
        Err(e) => Err(AllowlistError::Unreadable(format!(
            "{}: {e}",
            path.display()
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_single_and_multi_line() {
        assert_eq!(parse("tools = [\"a\", \"b\"]").unwrap(), vec!["a", "b"]);
        let text = "# header\ntools = [\n  \"a\", # first\n  \"b#c\",\n]\n";
        assert_eq!(parse(text).unwrap(), vec!["a", "b#c"]);
    }

    #[test]
    fn empty_is_an_error() {
        assert!(matches!(parse("tools = []"), Err(AllowlistError::Empty)));
        assert!(matches!(parse(""), Err(AllowlistError::Invalid(_))));
    }

    #[test]
    fn invalid_is_an_error() {
        for bad in [
            "tools = [\"a\"",
            "tools = \"a\"",
            "tool = [\"a\"]",
            "tools = [a]",
            "tools = [\"a\"] extra",
            "tools = [\"a\" \"b\"]",
        ] {
            assert!(
                matches!(parse(bad), Err(AllowlistError::Invalid(_))),
                "{bad}"
            );
        }
    }

    #[test]
    fn missing_file() {
        let r = load(Path::new("/nonexistent/allowlist.toml"));
        assert!(matches!(r, Err(AllowlistError::Missing(_))));
    }
}
