//! One stderr JSON line per `tools/call`. The record type has no field that
//! can hold an argument value, a result value or the token. Nothing is written
//! to a file or database.

use crate::json::Value;
use crate::sha256::sha256_hex;

pub struct Record {
    pub invocation_id: String,
    pub host_request_id: Value,
    pub tool: String,
    pub args_sha256: String,
    pub upstream_session_id: Option<String>,
    pub upstream_request_id: Option<String>,
    pub outcome: &'static str,
    pub is_error: bool,
    pub latency_ms: u128,
}

/// `sha256:` + hex of the canonical JSON of `args`, the same rule as
/// `orchestrator/context_snapshot.py::_canonical_json` and `_hash_bytes`.
pub fn args_sha256(args: &Value) -> String {
    format!("sha256:{}", sha256_hex(args.to_canonical().as_bytes()))
}

fn opt(value: &Option<String>) -> Value {
    match value {
        Some(s) => Value::text(s),
        None => Value::Null,
    }
}

impl Record {
    pub fn to_json(&self) -> String {
        Value::obj(vec![
            ("invocation_id", Value::text(&self.invocation_id)),
            ("host_request_id", self.host_request_id.clone()),
            ("tool", Value::text(&self.tool)),
            ("args_sha256", Value::text(&self.args_sha256)),
            ("upstream_session_id", opt(&self.upstream_session_id)),
            ("upstream_request_id", opt(&self.upstream_request_id)),
            ("outcome", Value::text(self.outcome)),
            ("is_error", Value::Bool(self.is_error)),
            ("latency_ms", Value::Num(self.latency_ms.to_string())),
        ])
        .to_compact()
    }

    pub fn emit(&self) {
        eprintln!("{}", self.to_json());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json;

    #[test]
    fn digest_matches_python_for_fixed_object() {
        let args = json::parse(r#"{"b":2,"a":{"z":1,"y":"argvalue-é"}}"#).unwrap();
        assert_eq!(
            args_sha256(&args),
            "sha256:842613ef2de7aac81c9aff148236f275a7d3e85537809cb0b6876e256e3f96fc"
        );
        assert_eq!(
            args_sha256(&Value::obj(vec![])),
            "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        );
    }

    #[test]
    fn record_has_all_fields_in_order() {
        let r = Record {
            invocation_id: "i".into(),
            host_request_id: Value::num(7),
            tool: "t".into(),
            args_sha256: "sha256:x".into(),
            upstream_session_id: None,
            upstream_request_id: Some("r".into()),
            outcome: "ok",
            is_error: false,
            latency_ms: 12,
        };
        assert_eq!(
            r.to_json(),
            r#"{"invocation_id":"i","host_request_id":7,"tool":"t","args_sha256":"sha256:x","upstream_session_id":null,"upstream_request_id":"r","outcome":"ok","is_error":false,"latency_ms":12}"#
        );
    }
}
