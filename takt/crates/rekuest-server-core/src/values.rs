//! Does a value fit a port? (`rekuest_core/values.py`). Shared by definition-time default checks
//! and assign-time argument checks. Messages are worded as the Python ones.
//!
//! INT int (not bool), FLOAT int|float, STRING str, BOOL bool, DATE ISO-8601 string, LIST list of
//! the child kind, DICT dict of the child kind, MODEL dict keyed by child ports, ENUM one of the
//! choices, STRUCTURE/MEMORY_STRUCTURE/INTERFACE an id (a default) or a `{"__identifier",
//! "object"}` reference (an argument), QUANTITY a number or `{"value": number, "unit": str}`,
//! UNION anything one of its variants accepts.

use serde_json::Value;

use crate::enums::PortKind;
use crate::pyjson::{repr, repr_str, type_name};

/// What both the input and the output port models expose.
pub trait PortLike {
    fn key(&self) -> &str;
    fn kind(&self) -> PortKind;
    fn nullable(&self) -> bool;
    fn children(&self) -> &[Self]
    where
        Self: Sized;
    fn choice_values(&self) -> Vec<&Value>;
    fn identifier(&self) -> Option<&str>;
    /// The declared default, for ports that have one.
    fn default_value(&self) -> Option<&Value>;
}

fn is_number(value: &Value) -> bool {
    value.is_number()
}

fn is_int(value: &Value) -> bool {
    value.as_i64().is_some() || value.as_u64().is_some()
}

/// `datetime.fromisoformat` or `date.fromisoformat` (Python 3.11+ accepts full ISO 8601).
fn is_iso_date(value: &str) -> bool {
    use chrono::{DateTime, NaiveDate, NaiveDateTime};
    let value = value.trim();
    if value.is_empty() {
        return false;
    }
    let with_t = value.replacen(' ', "T", 1);
    DateTime::parse_from_rfc3339(&with_t).is_ok()
        || DateTime::parse_from_rfc3339(&with_t.replace('z', "Z")).is_ok()
        || [
            "%Y-%m-%dT%H:%M:%S%.f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%dT%H",
            "%Y%m%dT%H%M%S",
        ]
        .iter()
        .any(|format| NaiveDateTime::parse_from_str(&with_t, format).is_ok())
        || [
            "%Y-%m-%dT%H:%M:%S%.f%:z",
            "%Y-%m-%dT%H:%M%:z",
            "%Y-%m-%dT%H:%M:%S%z",
        ]
        .iter()
        .any(|format| DateTime::parse_from_str(&with_t, format).is_ok())
        || ["%Y-%m-%d", "%Y%m%d"]
            .iter()
            .any(|format| NaiveDate::parse_from_str(value, format).is_ok())
}

/// Why `value` does not fit `port`, or `None` when it does. A `null` value is the caller's
/// business. `reference_envelope`: a structure is spelled as the client's `{"__identifier",
/// "object"}` envelope (an argument) rather than a bare id (a default).
pub fn value_mismatch<P: PortLike>(
    port: &P,
    value: &Value,
    path: Option<&str>,
    reference_envelope: bool,
) -> Option<String> {
    let path = path.unwrap_or(port.key()).to_owned();
    let kind = port.kind();
    let children = port.children();
    let kind_name = kind.value();
    match kind {
        PortKind::INT if !is_int(value) => {
            return Some(format!("{path}: expected an INT, got {}", type_name(value)))
        }
        PortKind::FLOAT if !is_number(value) => {
            return Some(format!(
                "{path}: expected a FLOAT, got {}",
                type_name(value)
            ))
        }
        PortKind::STRING if !value.is_string() => {
            return Some(format!(
                "{path}: expected a STRING, got {}",
                type_name(value)
            ))
        }
        PortKind::BOOL if !value.is_boolean() => {
            return Some(format!("{path}: expected a BOOL, got {}", type_name(value)))
        }
        PortKind::DATE => match value.as_str() {
            None => {
                return Some(format!(
                    "{path}: expected an ISO-8601 DATE string, got {}",
                    type_name(value)
                ))
            }
            Some(s) if !is_iso_date(s) => {
                return Some(format!("{path}: {} is not an ISO-8601 date", repr_str(s)))
            }
            _ => {}
        },
        PortKind::LIST => {
            let Some(items) = value.as_array() else {
                return Some(format!("{path}: expected a LIST, got {}", type_name(value)));
            };
            let item_port = children.first()?;
            for (index, item) in items.iter().enumerate() {
                let item_path = format!("{path}[{index}]");
                if item.is_null() {
                    if !item_port.nullable() {
                        return Some(format!("{item_path}: null is not allowed"));
                    }
                } else if let Some(m) =
                    value_mismatch(item_port, item, Some(&item_path), reference_envelope)
                {
                    return Some(m);
                }
            }
        }
        PortKind::DICT => {
            let Some(map) = value.as_object() else {
                return Some(format!("{path}: expected a DICT, got {}", type_name(value)));
            };
            let homogeneous = children.len() == 1 && children[0].key() == "...";
            for (name, item) in map {
                let child = if homogeneous {
                    Some(&children[0])
                } else {
                    children.iter().find(|child| child.key() == name)
                };
                let Some(child) = child else { continue };
                let item_path = format!("{path}[{}]", repr_str(name));
                if item.is_null() {
                    if !child.nullable() {
                        return Some(format!("{item_path}: null is not allowed"));
                    }
                } else if let Some(m) =
                    value_mismatch(child, item, Some(&item_path), reference_envelope)
                {
                    return Some(m);
                }
            }
        }
        PortKind::MODEL => {
            let Some(map) = value.as_object() else {
                return Some(format!(
                    "{path}: expected a MODEL object, got {}",
                    type_name(value)
                ));
            };
            let mut unknown: Vec<&String> = map
                .keys()
                .filter(|name| {
                    name.as_str() != "__identifier"
                        && !children.iter().any(|c| c.key() == name.as_str())
                })
                .collect();
            unknown.sort();
            if !unknown.is_empty() {
                let list = unknown
                    .iter()
                    .map(|k| repr_str(k))
                    .collect::<Vec<_>>()
                    .join(", ");
                return Some(format!("{path}: unknown fields [{list}]"));
            }
            for child in children {
                let item = map.get(child.key()).unwrap_or(&Value::Null);
                if item.is_null() {
                    if !child.nullable() && child.default_value().is_none_or(Value::is_null) {
                        return Some(format!("{path}.{}: required field is missing", child.key()));
                    }
                } else if let Some(m) = value_mismatch(
                    child,
                    item,
                    Some(&format!("{path}.{}", child.key())),
                    reference_envelope,
                ) {
                    return Some(m);
                }
            }
        }
        PortKind::ENUM => {
            let values = port.choice_values();
            if !values.contains(&value) {
                // Python sorts `map(str, values)`; `str` of a JSON scalar is its repr, bar strings.
                let mut names: Vec<String> = values
                    .iter()
                    .map(|v| v.as_str().map(str::to_owned).unwrap_or_else(|| repr(v)))
                    .collect();
                names.sort();
                let list = names
                    .iter()
                    .map(|n| repr_str(n))
                    .collect::<Vec<_>>()
                    .join(", ");
                return Some(format!(
                    "{path}: {} is not one of the choices [{list}]",
                    repr(value)
                ));
            }
        }
        PortKind::STRUCTURE | PortKind::MEMORY_STRUCTURE | PortKind::INTERFACE => {
            if !reference_envelope {
                if !(value.is_string() || is_int(value)) {
                    return Some(format!(
                        "{path}: expected a {kind_name} id (string or int), got {}",
                        type_name(value)
                    ));
                }
            } else {
                let Some(map) = value.as_object() else {
                    return Some(format!(
                        "{path}: expected a {kind_name} reference {{'__identifier', 'object'}}, got {}",
                        type_name(value)
                    ));
                };
                let Some(identifier) = map.get("__identifier") else {
                    return Some(format!(
                        "{path}: {kind_name} reference is missing its `__identifier` key"
                    ));
                };
                let Some(object) = map.get("object") else {
                    return Some(format!(
                        "{path}: {kind_name} reference is missing its `object` key"
                    ));
                };
                if let Some(expected) = port.identifier() {
                    if identifier.as_str() != Some(expected) {
                        return Some(format!(
                            "{path}: {kind_name} reference identifier mismatch: expected {}, got {}",
                            repr_str(expected),
                            repr(identifier)
                        ));
                    }
                }
                if !(object.is_string() || is_int(object)) {
                    return Some(format!(
                        "{path}: {kind_name} reference `object` must be a str or int id, got {}",
                        type_name(object)
                    ));
                }
            }
        }
        PortKind::QUANTITY => match value.as_object() {
            Some(map) => {
                let numeric = map.get("value").is_some_and(is_number);
                let unit_ok = map.get("unit").is_none_or(Value::is_string);
                if !numeric || !unit_ok {
                    return Some(format!(
                        "{path}: a QUANTITY object needs a numeric `value` and an optional string `unit`"
                    ));
                }
            }
            None if !is_number(value) => {
                return Some(format!(
                    "{path}: expected a QUANTITY (number or {{value, unit}}), got {}",
                    type_name(value)
                ))
            }
            None => {}
        },
        PortKind::UNION
            if !children.iter().any(|child| {
                value_mismatch(child, value, Some(&path), reference_envelope).is_none()
            }) =>
        {
            let variants = children
                .iter()
                .map(|c| repr_str(c.kind().value()))
                .collect::<Vec<_>>()
                .join(", ");
            return Some(format!(
                "{path}: {} matches none of the UNION variants [{variants}]",
                repr(value)
            ));
        }
        _ => {}
    }
    None
}

/// Assignment arguments against the action's root arg ports (`validate_assignment_args`):
/// unknown keys are refused; a non-nullable port without a default must be present; every value
/// must fit its port.
pub fn validate_assignment_args<P: PortLike>(
    ports: &[P],
    args: &serde_json::Map<String, Value>,
) -> Result<(), String> {
    let mut unknown: Vec<&String> = args
        .keys()
        .filter(|k| !ports.iter().any(|p| p.key() == k.as_str()))
        .collect();
    unknown.sort();
    if !unknown.is_empty() {
        let mut known: Vec<&str> = ports.iter().map(PortLike::key).collect();
        known.sort();
        return Err(format!(
            "Unknown arguments [{}]; this action accepts [{}]",
            unknown
                .iter()
                .map(|k| repr_str(k))
                .collect::<Vec<_>>()
                .join(", "),
            known
                .iter()
                .map(|k| repr_str(k))
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    for port in ports {
        let value = args.get(port.key()).unwrap_or(&Value::Null);
        if value.is_null() {
            if port.nullable() || port.default_value().is_some_and(|d| !d.is_null()) {
                continue;
            }
            return Err(format!(
                "Argument {} is required and has no default",
                repr_str(port.key())
            ));
        }
        if let Some(mismatch) = value_mismatch(port, value, None, true) {
            return Err(format!("Argument {mismatch}"));
        }
    }
    Ok(())
}
