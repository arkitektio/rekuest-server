//! Higher-order implementations: project a wrapper's call onto the implementation it wraps
//! (`facade/higher_order.py`).
//!
//! A higher-order implementation `H` wraps a lower one `L` on the same agent. Its config says
//! how: `bound` (fixed args of `L`), `arg_map` (`{lower_key: {"from": "caller", "key": …}}`),
//! `args_key` (pack the remaining caller args under one dict port), `dependency_map`, and
//! `return_map` (the way back, `persist::transitions::project_returns`). Errors are worded as
//! the Python `ValueError`s.

use serde_json::{Map, Value};

/// `L`'s args from `H`'s config and the caller's args (`build_lower_args`). Later wins on a key
/// clash: `bound`, then the explicit `arg_map` entries, then the remaining caller args (packed
/// under `args_key`, or spread).
pub fn build_lower_args(
    config: &Value,
    caller_args: &Map<String, Value>,
) -> Result<Map<String, Value>, String> {
    let mut lower = config
        .get("bound")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut consumed = vec![];
    if let Some(arg_map) = config.get("arg_map").and_then(Value::as_object) {
        for (lower_key, spec) in arg_map {
            if spec.get("from").and_then(Value::as_str) != Some("caller") {
                continue;
            }
            let caller_key = spec.get("key").and_then(Value::as_str).unwrap_or_default();
            let Some(value) = caller_args.get(caller_key) else {
                return Err(format!(
                    "Higher-order arg map references caller arg '{caller_key}' which was not supplied"
                ));
            };
            lower.insert(lower_key.clone(), value.clone());
            consumed.push(caller_key);
        }
    }
    let remaining: Map<String, Value> = caller_args
        .iter()
        .filter(|(key, _)| !consumed.contains(&key.as_str()))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    match config
        .get("args_key")
        .and_then(Value::as_str)
        .filter(|k| !k.is_empty())
    {
        Some(args_key) => {
            lower.insert(args_key.to_owned(), Value::Object(remaining));
        }
        None => lower.extend(remaining),
    }
    Ok(lower)
}

/// `L`'s dependencies from `H`'s resolved ones (`build_lower_dependencies`): with an empty
/// `dependency_map`, pass-through by key; otherwise each entry is `bound` (a value) or `caller`
/// (one of `H`'s by key).
pub fn build_lower_dependencies(
    config: &Value,
    resolved: &Map<String, Value>,
) -> Result<Map<String, Value>, String> {
    let Some(dependency_map) = config
        .get("dependency_map")
        .and_then(Value::as_object)
        .filter(|m| !m.is_empty())
    else {
        return Ok(resolved.clone());
    };
    let mut lower = Map::new();
    for (lower_key, spec) in dependency_map {
        match spec.get("from").and_then(Value::as_str) {
            Some("bound") => {
                lower.insert(
                    lower_key.clone(),
                    spec.get("value").cloned().unwrap_or(Value::Null),
                );
            }
            Some("caller") => {
                let key = spec.get("key").and_then(Value::as_str).unwrap_or_default();
                let Some(value) = resolved.get(key) else {
                    return Err(format!(
                        "Higher-order dependency map references declared dependency '{key}' which the caller did not supply"
                    ));
                };
                lower.insert(lower_key.clone(), value.clone());
            }
            _ => {
                return Err(format!(
                    "Higher-order dependency map entry for '{lower_key}' must set 'from' to 'bound' or 'caller'"
                ))
            }
        }
    }
    Ok(lower)
}

/// The wrapper's declared dependencies the `dependency_map` sources `from: caller`
/// (`required_lower_dependency_sources`).
fn caller_sources(config: &Value) -> Vec<&str> {
    config
        .get("dependency_map")
        .and_then(Value::as_object)
        .into_iter()
        .flat_map(|map| map.values())
        .filter(|spec| spec.get("from").and_then(Value::as_str) == Some("caller"))
        .filter_map(|spec| spec.get("key").and_then(Value::as_str))
        .collect()
}

/// Every lower dependency slot is satisfiable and every caller source is declared
/// (`validate_dependency_coverage`).
pub fn validate_dependency_coverage(
    config: &Value,
    lower_dependency_keys: &[String],
    declared: &[String],
) -> Result<(), String> {
    for key in caller_sources(config) {
        if !declared.iter().any(|d| d == key) {
            return Err(format!(
                "Higher-order dependency map references undeclared dependency '{key}'. Declare it on the wrapper so the caller knows to pass it."
            ));
        }
    }
    let map = config
        .get("dependency_map")
        .and_then(Value::as_object)
        .filter(|m| !m.is_empty());
    for lower in lower_dependency_keys {
        match map {
            None if !declared.contains(lower) => {
                return Err(format!(
                    "Lower dependency '{lower}' has no mapping and no matching declared dependency on the wrapper."
                ))
            }
            Some(map) if !map.contains_key(lower) => {
                return Err(format!("Lower dependency '{lower}' is neither bound nor mapped to a declared dependency."))
            }
            _ => {}
        }
    }
    Ok(())
}

/// No wrapping a wrapper, and the kinds must agree (`validate_higher_order_pairing`).
pub fn validate_higher_order_pairing(
    higher_kind: &str,
    lower_kind: &str,
    lower_is_higher_order: bool,
) -> Result<(), String> {
    if lower_is_higher_order {
        return Err(
            "Nested higher-order implementations are not supported yet — wrap a concrete implementation, not another wrapper.".into(),
        );
    }
    if higher_kind != lower_kind {
        return Err(format!(
            "Higher-order kind mismatch: wrapper is {higher_kind} but the wrapped action is {lower_kind}; they must agree."
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn map(value: Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }

    #[test]
    fn args_are_bound_mapped_and_packed() {
        let config = json!({
            "bound": {"model": "resnet", "x": 0},
            "arg_map": {"x": {"from": "caller", "key": "input"}},
            "args_key": "kwargs",
        });
        let lower = build_lower_args(&config, &map(json!({"input": 5, "extra": true}))).unwrap();
        assert_eq!(
            Value::Object(lower),
            json!({"model": "resnet", "x": 5, "kwargs": {"extra": true}})
        );
        let spread = build_lower_args(&json!({}), &map(json!({"a": 1}))).unwrap();
        assert_eq!(Value::Object(spread), json!({"a": 1}));
        assert_eq!(
            build_lower_args(&config, &Map::new()).unwrap_err(),
            "Higher-order arg map references caller arg 'input' which was not supplied"
        );
    }

    #[test]
    fn dependencies_pass_through_or_map() {
        let resolved = map(json!({"gpu": [{"agent": "1"}]}));
        assert_eq!(
            build_lower_dependencies(&json!({}), &resolved).unwrap(),
            resolved
        );
        let config = json!({"dependency_map": {
            "device": {"from": "caller", "key": "gpu"},
            "fixed": {"from": "bound", "value": [{"agent": "2"}]},
        }});
        let lower = build_lower_dependencies(&config, &resolved).unwrap();
        assert_eq!(
            Value::Object(lower),
            json!({"device": [{"agent": "1"}], "fixed": [{"agent": "2"}]})
        );
        let bad = json!({"dependency_map": {"x": {"from": "elsewhere"}}});
        assert_eq!(
            build_lower_dependencies(&bad, &resolved).unwrap_err(),
            "Higher-order dependency map entry for 'x' must set 'from' to 'bound' or 'caller'"
        );
    }

    #[test]
    fn creation_checks_speak_like_python() {
        let lower = vec!["db".to_owned()];
        assert!(validate_dependency_coverage(&json!({}), &lower, &["db".to_owned()]).is_ok());
        assert_eq!(
            validate_dependency_coverage(&json!({}), &lower, &[]).unwrap_err(),
            "Lower dependency 'db' has no mapping and no matching declared dependency on the wrapper."
        );
        let mapped = json!({"dependency_map": {"db": {"from": "caller", "key": "store"}}});
        assert_eq!(
            validate_dependency_coverage(&mapped, &lower, &[]).unwrap_err(),
            "Higher-order dependency map references undeclared dependency 'store'. Declare it on the wrapper so the caller knows to pass it."
        );
        assert!(validate_dependency_coverage(&mapped, &lower, &["store".to_owned()]).is_ok());
        let bound = json!({"dependency_map": {"other": {"from": "bound", "value": 1}}});
        assert_eq!(
            validate_dependency_coverage(&bound, &lower, &[]).unwrap_err(),
            "Lower dependency 'db' is neither bound nor mapped to a declared dependency."
        );
        assert!(validate_higher_order_pairing("FUNCTION", "FUNCTION", false).is_ok());
        assert!(
            validate_higher_order_pairing("FUNCTION", "GENERATOR", false)
                .unwrap_err()
                .starts_with("Higher-order kind mismatch")
        );
        assert!(validate_higher_order_pairing("FUNCTION", "FUNCTION", true)
            .unwrap_err()
            .starts_with("Nested higher-order"));
    }
}
