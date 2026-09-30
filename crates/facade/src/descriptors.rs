//! Requires/provides descriptors compiled to PostgreSQL JSONPath (`facade/descriptors.py`).
//!
//! Compiled once, at registration, into a predicate stored on the ArgPort/ReturnPort row and
//! evaluated at query time with `jsonb_path_match` against a candidate's descriptor object.
//! Values are printed by Python's `json.dumps`, so the stored strings are Python's.

use rekuest_core::enums::DescriptorOperator;
use rekuest_core::inputs::DescriptorConstraint;
use rekuest_core::pyjson::{dumps, repr};
use serde_json::Value;

/// A descriptor key as a rooted, quoted JSONPath member (`$."key"`).
fn normalize_jsonpath_key(key: &str) -> Result<String, String> {
    let raw = key.strip_prefix("$.").unwrap_or(key);
    if raw.is_empty() {
        return Err(format!("Invalid descriptor key for JSONPath compilation: {}", rekuest_core::pyjson::repr_str(key)));
    }
    Ok(format!("$.{}", dumps(&Value::String(raw.to_owned()), false)))
}

/// One descriptor as one JSONPath predicate (`_compile_descriptor_condition`).
fn compile_condition(descriptor: &DescriptorConstraint) -> Result<String, String> {
    let path = normalize_jsonpath_key(&descriptor.key)?;
    let value = descriptor.value.clone().unwrap_or(Value::Null);
    let formatted = dumps(&value, false);
    let op = descriptor.operator;
    Ok(match op {
        DescriptorOperator::EXISTS => match value {
            Value::Bool(true) => format!("exists({path})"),
            Value::Bool(false) => format!("!(exists({path}))"),
            other => {
                return Err(format!(
                    "Operator DescriptorOperator.{op} requires a boolean value (True = must exist, False = must not exist), got {}",
                    repr(&other)
                ))
            }
        },
        DescriptorOperator::MATCHES | DescriptorOperator::EQUALS => format!("{path} == {formatted}"),
        DescriptorOperator::NOT_EQUALS => format!("{path} != {formatted}"),
        DescriptorOperator::GTE => format!("{path} >= {formatted}"),
        DescriptorOperator::LTE => format!("{path} <= {formatted}"),
        DescriptorOperator::CONTAINS => format!("{path}[*] == {formatted}"),
        DescriptorOperator::IN | DescriptorOperator::NOT_IN => {
            let Value::Array(items) = &value else {
                return Err(format!("Operator DescriptorOperator.{op} requires a list value, got {}", repr(&value)));
            };
            let (cmp, join, empty) = if op == DescriptorOperator::IN {
                ("==", " || ", "(false)")
            } else {
                ("!=", " && ", "(true)")
            };
            if items.is_empty() {
                empty.to_owned()
            } else {
                let parts: Vec<String> = items.iter().map(|v| format!("{path} {cmp} {}", dumps(v, false))).collect();
                format!("({})", parts.join(join))
            }
        }
    })
}

/// A port's descriptors as one JSONPath string, AND-ed; none → `None` (stored NULL).
pub fn compile_descriptors_to_jsonpath(descriptors: Option<&[DescriptorConstraint]>) -> Result<Option<String>, String> {
    let descriptors = descriptors.unwrap_or_default();
    if descriptors.is_empty() {
        return Ok(None);
    }
    let parts = descriptors.iter().map(compile_condition).collect::<Result<Vec<_>, _>>()?;
    Ok(Some(parts.join(" && ")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn descriptor(key: &str, operator: DescriptorOperator, value: Value) -> DescriptorConstraint {
        DescriptorConstraint { key: key.into(), operator, value: Some(value).filter(|v| !v.is_null()) }
    }

    #[test]
    fn descriptors_compile_as_python_compiles_them() {
        let compiled = compile_descriptors_to_jsonpath(Some(&[
            descriptor("axes", DescriptorOperator::IN, json!(["c", "t"])),
            descriptor("$.@mikro/n", DescriptorOperator::GTE, json!(2)),
            descriptor("x", DescriptorOperator::EXISTS, json!(true)),
        ]))
        .unwrap();
        assert_eq!(
            compiled.as_deref(),
            Some(r#"($."axes" == "c" || $."axes" == "t") && $."@mikro/n" >= 2 && exists($."x")"#)
        );
        // The model allows EXISTS without a value; compiling it does not, as in Python.
        assert!(compile_descriptors_to_jsonpath(Some(&[descriptor("x", DescriptorOperator::EXISTS, Value::Null)])).is_err());
        assert_eq!(compile_descriptors_to_jsonpath(Some(&[])).unwrap(), None);
    }
}
