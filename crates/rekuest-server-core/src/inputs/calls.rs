//! Calls, arguments and component props (`rekuest_core/inputs/models.py`, the blok half):
//! what effects, validators, widgets and bloks evaluate.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::ValidationError;
use crate::pyjson::repr_str;

/// Path grammar shared by port dependencies and call arguments: a port path is a
/// `..`-separated walk through `children`; a value_path's first `/` segment is its root.
pub const PORT_PATH_SEPARATOR: &str = "..";
/// The conventional key of a LIST or DICT item port; the only key that may contain the separator.
pub const ITEM_KEY: &str = "...";

/// First `/` segment of a value_path (`'/other/x'` → `other`, `'foo..bar/x'` → `foo..bar`).
pub fn value_path_root(value_path: &str) -> &str {
    let trimmed = value_path.trim_start_matches('/');
    trimmed.split('/').next().unwrap_or("")
}

/// First segment of a blok path, split on `/` and `.` like the renderer does.
pub fn blok_path_root(path: &str) -> &str {
    path.split(['/', '.'])
        .find(|segment| !segment.is_empty())
        .unwrap_or("")
}

fn py_opt_str(value: &Option<String>) -> String {
    value
        .as_deref()
        .map(repr_str)
        .unwrap_or_else(|| "None".into())
}

/// Map-shaped argument lists (call arguments, value_dict) need unique, non-empty keys.
pub fn check_keyed(
    arguments: Option<&[ActionArgumentInputModel]>,
    owner: &str,
) -> Result<(), ValidationError> {
    let mut seen = std::collections::HashSet::new();
    for argument in arguments.unwrap_or_default() {
        let Some(key) = argument.key.as_deref().filter(|k| !k.is_empty()) else {
            return Err(ValidationError(format!(
                "{owner}: every entry must carry a key"
            )));
        };
        if !seen.insert(key) {
            return Err(ValidationError(format!(
                "{owner}: duplicate key {}",
                repr_str(key)
            )));
        }
    }
    Ok(())
}

/// A static value, or a JSON pointer into the blok state.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DynamicValueInputModel {
    #[serde(default)]
    pub literal: Option<String>,
    #[serde(default)]
    pub path: Option<String>,
}

/// A callback routed to an agent (`AgentProbeInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AgentProbeInputModel {
    pub dependency: String,
    pub operation: String,
    #[serde(default)]
    pub arguments: Option<Vec<ActionArgumentInputModel>>,
}

impl AgentProbeInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        for argument in self.arguments.iter_mut().flatten() {
            argument.validate()?;
        }
        super::min_length(&self.dependency, "dependency")?;
        super::min_length(&self.operation, "operation")?;
        check_keyed(
            self.arguments.as_deref(),
            &format!("arguments of agent call {}", self.operation),
        )
    }
}

/// A pure utility call, evaluated client-side against the catalog (`UtilCallInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct UtilCallInputModel {
    pub operation: String,
    #[serde(default)]
    pub arguments: Option<Vec<ActionArgumentInputModel>>,
}

impl UtilCallInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        for argument in self.arguments.iter_mut().flatten() {
            argument.validate()?;
        }
        super::min_length(&self.operation, "operation")?;
        check_keyed(
            self.arguments.as_deref(),
            &format!("arguments of {}", self.operation),
        )
    }
}

/// One argument of a call: bound exactly one way (`ActionArgumentInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActionArgumentInputModel {
    #[serde(default)]
    pub key: Option<String>,
    #[serde(default)]
    pub value_literal: Option<Value>,
    #[serde(default)]
    pub value_path: Option<String>,
    #[serde(default)]
    pub agent_call: Option<AgentProbeInputModel>,
    #[serde(default)]
    pub util_call: Option<UtilCallInputModel>,
    #[serde(default)]
    pub value_list: Option<Vec<ActionArgumentInputModel>>,
    #[serde(default)]
    pub value_dict: Option<Vec<ActionArgumentInputModel>>,
}

impl ActionArgumentInputModel {
    const BINDINGS: [&'static str; 6] = [
        "value_literal",
        "value_path",
        "agent_call",
        "util_call",
        "value_list",
        "value_dict",
    ];

    pub fn validate(&mut self) -> Result<(), ValidationError> {
        if let Some(call) = self.agent_call.as_mut() {
            call.validate()?;
        }
        if let Some(call) = self.util_call.as_mut() {
            call.validate()?;
        }
        for argument in self
            .value_list
            .iter_mut()
            .flatten()
            .chain(self.value_dict.iter_mut().flatten())
        {
            argument.validate()?;
        }
        // `value_literal` is `str | int | float | dict | list`; pydantic's lax mode turns a bool
        // into the int it is (True -> 1), so it does too.
        if let Some(Value::Bool(b)) = self.value_literal {
            self.value_literal = Some(Value::from(i64::from(b)));
        }
        let set = [
            self.value_literal.is_some(),
            self.value_path.is_some(),
            self.agent_call.is_some(),
            self.util_call.is_some(),
            self.value_list.is_some(),
            self.value_dict.is_some(),
        ];
        let bound: Vec<&str> = Self::BINDINGS
            .iter()
            .zip(set)
            .filter(|(_, s)| *s)
            .map(|(n, _)| *n)
            .collect();
        if bound.len() != 1 {
            let got = if bound.is_empty() {
                "none".to_owned()
            } else {
                format!(
                    "[{}]",
                    bound
                        .iter()
                        .map(|b| repr_str(b))
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            };
            return Err(ValidationError(format!(
                "ActionArgument {} must set exactly one of {} (got {got})",
                py_opt_str(&self.key),
                Self::BINDINGS.join(", ")
            )));
        }
        check_keyed(
            self.value_dict.as_deref(),
            &format!("value_dict of argument {}", py_opt_str(&self.key)),
        )?;
        if self
            .value_list
            .iter()
            .flatten()
            .any(|entry| entry.key.is_some())
        {
            return Err(ValidationError(format!(
                "value_list entries of argument {} must not carry a key",
                py_opt_str(&self.key)
            )));
        }
        Ok(())
    }
}

/// Every UtilCall nested anywhere inside an argument tree (depth first).
pub fn iter_util_calls(arguments: Option<&[ActionArgumentInputModel]>) -> Vec<&UtilCallInputModel> {
    let mut out = vec![];
    for argument in arguments.unwrap_or_default() {
        if let Some(call) = &argument.util_call {
            out.push(call);
            out.extend(iter_util_calls(call.arguments.as_deref()));
        }
        if let Some(call) = &argument.agent_call {
            out.extend(iter_util_calls(call.arguments.as_deref()));
        }
        out.extend(iter_util_calls(argument.value_list.as_deref()));
        out.extend(iter_util_calls(argument.value_dict.as_deref()));
    }
    out
}

/// A call is pure (no agent calls) and its value_paths only reach `dependencies`, `value` and
/// `extra_roots` (`_check_pure_call`).
pub fn check_pure_call(
    call: &UtilCallInputModel,
    dependencies: Option<&[String]>,
    owner: &str,
    extra_roots: &[&str],
) -> Result<(), ValidationError> {
    let allowed = |root: &str| {
        root == "value"
            || extra_roots.contains(&root)
            || dependencies.unwrap_or_default().iter().any(|d| d == root)
    };
    fn walk(
        arguments: Option<&[ActionArgumentInputModel]>,
        allowed: &dyn Fn(&str) -> bool,
        owner: &str,
    ) -> Result<(), ValidationError> {
        for argument in arguments.unwrap_or_default() {
            if argument.agent_call.is_some() {
                return Err(ValidationError(format!(
                    "{owner} must be pure: nested agent calls are not allowed"
                )));
            }
            if let Some(path) = &argument.value_path {
                let root = value_path_root(path);
                if !allowed(root) {
                    return Err(ValidationError(format!(
                        "{owner} references '{root}' via value_path but it is not in dependencies"
                    )));
                }
            }
            if let Some(call) = &argument.util_call {
                walk(call.arguments.as_deref(), allowed, owner)?;
            }
            walk(argument.value_list.as_deref(), allowed, owner)?;
            walk(argument.value_dict.as_deref(), allowed, owner)?;
        }
        Ok(())
    }
    walk(call.arguments.as_deref(), &allowed, owner)
}

/// One prop of a component node, bound at most one way (`ComponentPropInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComponentPropInputModel {
    pub key: String,
    #[serde(default)]
    pub static_value: Option<Value>,
    #[serde(default)]
    pub dynamic_value: Option<DynamicValueInputModel>,
    #[serde(default)]
    pub declares_value: Option<String>,
    #[serde(default)]
    pub agent_call: Option<AgentProbeInputModel>,
    #[serde(default)]
    pub util_call: Option<UtilCallInputModel>,
}

impl ComponentPropInputModel {
    const BINDINGS: [&'static str; 4] =
        ["static_value", "dynamic_value", "agent_call", "util_call"];

    pub fn validate(&mut self) -> Result<(), ValidationError> {
        if let Some(call) = self.agent_call.as_mut() {
            call.validate()?;
        }
        if let Some(call) = self.util_call.as_mut() {
            call.validate()?;
        }
        // `static_value` is `str | int | float | dict`: a bool becomes its int (pydantic's lax
        // mode), a list fits none of them.
        match self.static_value {
            Some(Value::Bool(b)) => self.static_value = Some(Value::from(i64::from(b))),
            Some(Value::Array(_)) => {
                return Err(ValidationError(format!(
                    "ComponentProp {}: static_value must be a string, number or dict",
                    repr_str(&self.key)
                )))
            }
            _ => {}
        }
        let set = [
            self.static_value.is_some(),
            self.dynamic_value.is_some(),
            self.agent_call.is_some(),
            self.util_call.is_some(),
        ];
        let bound: Vec<&str> = Self::BINDINGS
            .iter()
            .zip(set)
            .filter(|(_, s)| *s)
            .map(|(n, _)| *n)
            .collect();
        if bound.len() > 1 {
            return Err(ValidationError(format!(
                "ComponentProp {} must set at most one of {} (got [{}])",
                repr_str(&self.key),
                Self::BINDINGS.join(", "),
                bound
                    .iter()
                    .map(|b| repr_str(b))
                    .collect::<Vec<_>>()
                    .join(", ")
            )));
        }
        if bound.is_empty() && self.declares_value.as_deref().is_none_or(str::is_empty) {
            return Err(ValidationError(format!(
                "ComponentProp {} is neither bound nor declares a value",
                repr_str(&self.key)
            )));
        }
        Ok(())
    }
}

/// Custom widget props: no agent calls; value_paths only reach `value` and `dependencies`.
pub fn check_widget_props(
    props: Option<&[ComponentPropInputModel]>,
    dependencies: Option<&[String]>,
    owner: &str,
) -> Result<(), ValidationError> {
    for prop in props.unwrap_or_default() {
        let prop_owner = format!("{owner} prop {}", repr_str(&prop.key));
        if prop.agent_call.is_some() {
            return Err(ValidationError(format!(
                "{prop_owner} must be pure: agent calls are not allowed in widgets"
            )));
        }
        if let Some(path) = prop.dynamic_value.as_ref().and_then(|d| d.path.as_deref()) {
            let root = value_path_root(path);
            if root != "value" && !dependencies.unwrap_or_default().iter().any(|d| d == root) {
                return Err(ValidationError(format!(
                    "{prop_owner} references '{root}' via dynamic_value.path but it is not in dependencies"
                )));
            }
        }
        if let Some(call) = &prop.util_call {
            check_pure_call(call, dependencies, &prop_owner, &[])?;
        }
    }
    Ok(())
}

/// A node of a blok's component tree (`ComponentNodeInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComponentNodeInputModel {
    pub id: String,
    pub component: String,
    #[serde(default)]
    pub props: Option<Vec<ComponentPropInputModel>>,
    #[serde(default)]
    pub children: Option<Vec<ComponentNodeInputModel>>,
}

impl ComponentNodeInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        for prop in self.props.iter_mut().flatten() {
            prop.validate()?;
        }
        for child in self.children.iter_mut().flatten() {
            child.validate()?;
        }
        Ok(())
    }
}

/// Every node of a component tree, pre-order.
pub fn iter_component_nodes(
    components: Option<&[ComponentNodeInputModel]>,
) -> Vec<&ComponentNodeInputModel> {
    let mut out = vec![];
    for node in components.unwrap_or_default() {
        out.push(node);
        out.extend(iter_component_nodes(node.children.as_deref()));
    }
    out
}
