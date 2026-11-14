//! Whether an action or a state meets what a dependency demands of it.
//!
//! A dependency names the action it wants by key, and may say what its ports must be: a port
//! match is met by a port equal in every field the match sets (`at`, `key`, `kind`,
//! `identifier`, `dimension`, `nullable`), its `children` the same way below it. A state is a
//! record, so its matches carry no position and are met by a field of that key. `descriptors`
//! are not looked at here: they are evaluated against a concrete object, and binding has none.
//!
//! The rule is `_build_match_exists` (the action search) and `arkitekt_spec.declare.demands`
//! (the offline check): the three say the same.

use serde_json::Value;

use rekuest_core::inputs::{ActionDemandInputModel, PortMatchInputModel, StateDemandInputModel};

fn text<'a>(port: &'a Value, field: &str) -> Option<&'a str> {
    port.get(field).and_then(Value::as_str)
}

fn nullable(port: &Value) -> bool {
    port.get("nullable")
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

fn children(port: &Value) -> &[Value] {
    port.get("children")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or_default()
}

/// The kind of a match as a port spells it (`"STRING"`).
fn kind_of(port_match: &PortMatchInputModel) -> Option<String> {
    let kind = serde_json::to_value(port_match.kind.as_ref()?).ok()?;
    kind.as_str().map(str::to_owned)
}

/// A port or a match as a refusal names it: `'name' (STRING, nullable)`.
fn shown(
    key: Option<&str>,
    kind: Option<&str>,
    identifier: Option<&str>,
    nullable: bool,
) -> String {
    let mut details: Vec<&str> = kind.into_iter().collect();
    details.extend(identifier.filter(|i| !i.is_empty()));
    if nullable {
        details.push("nullable");
    }
    let name = key.map_or_else(|| "a port".to_owned(), |key| format!("'{key}'"));
    if details.is_empty() {
        name
    } else {
        format!("{name} ({})", details.join(", "))
    }
}

fn fits(port_match: &PortMatchInputModel, port: &Value) -> bool {
    if port_match
        .key
        .as_deref()
        .is_some_and(|key| Some(key) != text(port, "key"))
    {
        return false;
    }
    if kind_of(port_match).is_some_and(|kind| Some(kind.as_str()) != text(port, "kind")) {
        return false;
    }
    if port_match
        .identifier
        .as_deref()
        .is_some_and(|identifier| Some(identifier) != text(port, "identifier"))
    {
        return false;
    }
    if port_match
        .dimension
        .as_deref()
        .is_some_and(|dimension| Some(dimension) != text(port, "dimension"))
    {
        return false;
    }
    if port_match
        .nullable
        .is_some_and(|wanted| wanted != nullable(port))
    {
        return false;
    }
    unmet_match(
        port_match.children.as_deref().unwrap_or_default(),
        children(port),
    )
    .is_none()
}

/// Why `ports` do not meet `matches`: the first match no port meets, or `None` when all are met.
pub fn unmet_match(matches: &[PortMatchInputModel], ports: &[Value]) -> Option<String> {
    for port_match in matches {
        let kind = kind_of(port_match);
        let wanted = shown(
            port_match.key.as_deref(),
            kind.as_deref(),
            port_match.identifier.as_deref(),
            port_match.nullable.unwrap_or(false),
        );
        let Some(at) = port_match.at else {
            if !ports.iter().any(|port| fits(port_match, port)) {
                return Some(format!("nothing like {wanted} is there"));
            }
            continue;
        };
        let Some(port) = usize::try_from(at).ok().and_then(|at| ports.get(at)) else {
            return Some(format!(
                "{wanted} is expected at {at}, where there is nothing"
            ));
        };
        if !fits(port_match, port) {
            let found = shown(
                text(port, "key"),
                text(port, "kind"),
                text(port, "identifier"),
                nullable(port),
            );
            return Some(format!(
                "{wanted} is expected at {at}, where there is {found}"
            ));
        }
    }
    None
}

fn ports(value: &Value) -> &[Value] {
    value.as_array().map(Vec::as_slice).unwrap_or_default()
}

/// Why the action (`name`, `args`, `returns` as it is stored) does not meet `demand`, or `None`.
/// A demand by `hash` names its action outright and demands nothing more of it.
pub fn unmet_action_demand(
    demand: &ActionDemandInputModel,
    name: &str,
    args: &Value,
    returns: &Value,
) -> Option<String> {
    if demand.hash.as_deref().is_some_and(|hash| !hash.is_empty()) {
        return None;
    }
    if let Some(wanted) = demand.name.as_deref().filter(|wanted| *wanted != name) {
        return Some(format!("it is named {name:?}, not {wanted:?}"));
    }
    for (what, matches, ports, forced) in [
        (
            "arguments",
            &demand.arg_matches,
            ports(args),
            demand.force_arg_length,
        ),
        (
            "returns",
            &demand.return_matches,
            ports(returns),
            demand.force_return_length,
        ),
    ] {
        if let Some(forced) = forced.filter(|forced| *forced != ports.len() as i64) {
            return Some(format!("it has {} {what}, not {forced}", ports.len()));
        }
        if let Some(reason) = unmet_match(matches.as_deref().unwrap_or_default(), ports) {
            return Some(format!("{what}: {reason}"));
        }
    }
    None
}

/// Why a state with these `fields` does not meet `demand`, or `None`.
pub fn unmet_state_demand(demand: &StateDemandInputModel, fields: &Value) -> Option<String> {
    if demand.hash.as_deref().is_some_and(|hash| !hash.is_empty()) {
        return None;
    }
    // By key alone, whatever position a client sent: a state is a record, and a demand may
    // name some of its fields in any order.
    let matches: Vec<PortMatchInputModel> = demand
        .matches
        .iter()
        .flatten()
        .cloned()
        .map(|field| PortMatchInputModel { at: None, ..field })
        .collect();
    unmet_match(&matches, ports(fields))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn demand(value: Value) -> ActionDemandInputModel {
        serde_json::from_value(value).expect("a demand")
    }

    #[test]
    fn a_demand_is_met_by_an_action_that_takes_more_than_it_names() {
        let wanted = demand(
            json!({"key": "move", "arg_matches": [{"at": 0, "key": "trajectory", "kind": "STRING", "nullable": false}],
                                   "return_matches": [{"at": 0, "key": "return0", "kind": "BOOL", "nullable": false}]}),
        );
        let args = json!([{"key": "trajectory", "kind": "STRING", "nullable": false},
                          {"key": "speed", "kind": "FLOAT", "nullable": true}]);
        let returns = json!([{"key": "return0", "kind": "BOOL", "nullable": false}]);
        assert_eq!(unmet_action_demand(&wanted, "Move", &args, &returns), None);
    }

    #[test]
    fn a_renamed_argument_is_said_with_what_is_there_instead() {
        let wanted = demand(
            json!({"key": "move", "arg_matches": [{"at": 0, "key": "name", "kind": "STRING", "nullable": false}]}),
        );
        let args = json!([{"key": "trajectory", "kind": "STRING", "nullable": false}]);
        assert_eq!(
            unmet_action_demand(&wanted, "Move", &args, &json!([])).as_deref(),
            Some(
                "arguments: 'name' (STRING) is expected at 0, where there is 'trajectory' (STRING)"
            )
        );
    }
}
