//! Canonical args encoding and hash (`facade/provenance/canonical.py`).
//!
//! The provenance token carries `ahs` rather than the inline args, which binds the cleartext
//! args travelling alongside it to the signature. The canonical form is a versioned contract:
//! a downstream verifier must reproduce exactly these bytes, so a change here is breaking and
//! must bump [`CANONICALIZATION_VERSION`].
//!
//! v1 is `json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, then the
//! SHA-256 of its UTF-8 bytes, hex-encoded. Not `pyjson::dumps`: no spaces after the
//! separators, and non-ASCII stays as it is. Floats print as `repr(float)`.

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use rekuest_core::pyjson::float_repr;

pub const CANONICALIZATION_VERSION: u32 = 1;

/// The `aha` claim: how `ahs` was computed.
pub fn algorithm() -> String {
    format!("sha256-canonical-v{CANONICALIZATION_VERSION}")
}

/// A string as `json.dumps(..., ensure_ascii=False)` writes it: only the quote, the backslash
/// and the control characters below 0x20 are escaped.
fn string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) if n.is_f64() => out.push_str(&float_repr(n.as_f64().expect("f64"))),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let mut entries: Vec<_> = map.iter().collect();
            // Python sorts by code point; UTF-8 byte order is the same order.
            entries.sort_by(|a, b| a.0.cmp(b.0));
            for (i, (key, item)) in entries.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                string(key, out);
                out.push(':');
                write(item, out);
            }
            out.push('}');
        }
    }
}

/// The canonical form of the args (`canonicalize_args`).
pub fn canonicalize_args(args: &Map<String, Value>) -> String {
    let mut out = String::new();
    write(&Value::Object(args.clone()), &mut out);
    out
}

/// SHA-256 hex digest of the canonical args (`args_hash`).
pub fn args_hash(args: &Map<String, Value>) -> String {
    hex::encode(Sha256::digest(canonicalize_args(args).as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn canonical_form_matches_json_dumps() {
        // python: json.dumps({"b": [1, 2.5, None, 1e-07], "a": "é\n \x7f", "c": True},
        //                    sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        let args = json!({"b": [1, 2.5, null, 1e-7], "a": "é\n\u{2028}\u{7f}", "c": true});
        assert_eq!(
            canonicalize_args(args.as_object().unwrap()),
            "{\"a\":\"é\\n\u{2028}\u{7f}\",\"b\":[1,2.5,null,1e-07],\"c\":true}"
        );
    }

    #[test]
    fn empty_args_hash_like_python() {
        // python: hashlib.sha256(b"{}").hexdigest()
        assert_eq!(
            args_hash(&Map::new()),
            "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        );
    }
}
