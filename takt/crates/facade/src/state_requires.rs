//! What a task's agent's state must hold for it to run (`state_requires`).
//!
//! The server cannot test it: it keeps no state's current value, only its patches, and the
//! value may change between any check here and the run. The agent tests it, after taking the
//! task's locks and the locks the state requires. What the server does is refuse what could
//! never be tested: requirements sent to an agent that does not say it tests them (it would
//! drop them and run the task unchecked), on a state the agent does not have, or on a path its
//! declaration does not hold.

use serde_json::Value;
use sqlx::types::Json;
use sqlx::PgPool;

use crate::backend::{BackendError, BackendResult};
use crate::messages::{StateRequirement, FEATURE_STATE_REQUIRES};

/// The operators a requirement may use: those of a port's `requires`.
const OPERATORS: [&str; 9] = [
    "MATCHES",
    "EXISTS",
    "LTE",
    "GTE",
    "EQUALS",
    "CONTAINS",
    "NOT_EQUALS",
    "IN",
    "NOT_IN",
];

/// Refuse requirements the agent `agent` could not test: see the module.
pub async fn check(
    db: &PgPool,
    agent: i64,
    requirements: &[StateRequirement],
) -> BackendResult<()> {
    if requirements.is_empty() {
        return Ok(());
    }
    for requirement in requirements {
        check_shape(requirement)?;
    }

    let features: Option<Json<Value>> =
        sqlx::query_scalar("SELECT features FROM facade_agent WHERE id = $1")
            .bind(agent)
            .fetch_optional(db)
            .await?
            .flatten();
    let tests_them = features
        .as_ref()
        .and_then(|f| f.0.as_array())
        .is_some_and(|features| {
            features
                .iter()
                .any(|feature| feature.as_str() == Some(FEATURE_STATE_REQUIRES))
        });
    if !tests_them {
        return Err(BackendError::Refused(
            "The agent does not test state requirements (it does not say `state_requires` when it \
             registers): it would run the task unchecked. Update its arkitekt, or call without them."
                .into(),
        ));
    }

    for requirement in requirements {
        let ports: Option<Json<Value>> = sqlx::query_scalar(
            "SELECT d.ports FROM facade_state s JOIN facade_statedefinition d ON d.id = s.definition_id \
             WHERE s.agent_id = $1 AND s.interface = $2",
        )
        .bind(agent)
        .bind(&requirement.state)
        .fetch_optional(db)
        .await?;
        let Some(Json(ports)) = ports else {
            return Err(BackendError::Refused(format!(
                "State requirement on {:?}: the agent has no such state",
                requirement.state
            )));
        };
        let member = first_member(&requirement.path);
        if !port_keys(&ports).any(|key| key == member) {
            return Err(BackendError::Refused(format!(
                "State requirement {}{}: the state {:?} declares no {:?}",
                requirement.state, requirement.path, requirement.state, member
            )));
        }
    }
    Ok(())
}

/// The operator is known, the path a pointer to a member, and the value fits the operator (as a
/// port's `requires` is checked).
fn check_shape(requirement: &StateRequirement) -> BackendResult<()> {
    let at = format!(
        "State requirement {}{}",
        requirement.state, requirement.path
    );
    if requirement.state.is_empty() {
        return Err(BackendError::Refused(format!("{at}: no state named")));
    }
    if !requirement.path.starts_with('/') || requirement.path.len() < 2 {
        return Err(BackendError::Refused(format!(
            "{at}: the path is an RFC 6901 pointer to a member of the state, e.g. /busy"
        )));
    }
    let operator = requirement.operator.as_str();
    if !OPERATORS.contains(&operator) {
        return Err(BackendError::Refused(format!(
            "{at}: unknown operator {operator:?}"
        )));
    }
    let value = &requirement.value;
    let refused = match operator {
        "IN" | "NOT_IN" if !value.is_array() => Some("needs a list value"),
        "LTE" | "GTE" if !value.is_number() => Some("needs a numeric value"),
        "EXISTS" if !(value.is_null() || value.is_boolean()) => Some("takes no value or a boolean"),
        _ => None,
    };
    match refused {
        Some(why) => Err(BackendError::Refused(format!("{at}: {operator} {why}"))),
        None => Ok(()),
    }
}

/// The member a pointer starts at (`/stage/x` → `stage`), unescaped (`~1` → `/`, `~0` → `~`).
fn first_member(path: &str) -> String {
    let token = path[1..].split('/').next().unwrap_or("");
    token.replace("~1", "/").replace("~0", "~")
}

/// The port keys of a state definition: a list of ports (each with a `key`), or an object.
fn port_keys(ports: &Value) -> Box<dyn Iterator<Item = &str> + '_> {
    match ports {
        Value::Array(ports) => Box::new(
            ports
                .iter()
                .filter_map(|port| port.get("key").and_then(Value::as_str)),
        ),
        Value::Object(ports) => Box::new(ports.keys().map(String::as_str)),
        _ => Box::new(std::iter::empty()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn requirement(path: &str, operator: &str, value: Value) -> StateRequirement {
        StateRequirement {
            state: "Stage".into(),
            path: path.into(),
            operator: operator.into(),
            value,
        }
    }

    #[test]
    fn a_requirement_fits_its_operator() {
        assert!(check_shape(&requirement("/busy", "EQUALS", json!(false))).is_ok());
        assert!(check_shape(&requirement("/mode", "IN", json!(["a"]))).is_ok());
        assert!(check_shape(&requirement("/x", "GTE", json!(1.5))).is_ok());
        assert!(check_shape(&requirement("/x", "EXISTS", Value::Null)).is_ok());
        assert!(check_shape(&requirement("/mode", "IN", json!("a"))).is_err());
        assert!(check_shape(&requirement("/x", "GTE", json!("1"))).is_err());
        assert!(check_shape(&requirement("/x", "EXISTS", json!(1))).is_err());
        assert!(check_shape(&requirement("/x", "LIKE", json!(1))).is_err());
    }

    #[test]
    fn a_path_is_a_pointer_to_a_member() {
        assert!(check_shape(&requirement("busy", "EQUALS", json!(1))).is_err());
        assert!(check_shape(&requirement("/", "EQUALS", json!(1))).is_err());
        assert_eq!(first_member("/stage/position/0"), "stage");
        assert_eq!(first_member("/a~1b/c"), "a/b");
        assert_eq!(first_member("/~0x"), "~x");
    }

    #[test]
    fn port_keys_are_read_from_a_list_or_an_object() {
        let list = json!([{"key": "busy", "kind": "BOOL"}, {"key": "mode"}]);
        assert_eq!(port_keys(&list).collect::<Vec<_>>(), ["busy", "mode"]);
        let object = json!({"busy": {}, "mode": {}});
        assert_eq!(port_keys(&object).collect::<Vec<_>>(), ["busy", "mode"]);
    }
}
