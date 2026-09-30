//! Definitions, dependencies and implementations (`rekuest_core/inputs/models.py`, the
//! declaration half).

use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::calls::{
    blok_path_root, check_pure_call, iter_component_nodes, ActionArgumentInputModel,
    AgentProbeInputModel, ComponentNodeInputModel, UtilCallInputModel, PORT_PATH_SEPARATOR,
};
use super::ports::{
    check_unique_keys, ArgPortInputModel, AssignWidgetInputModel, EffectInputModel,
    ReturnPortInputModel, ReturnWidgetInputModel,
};
use super::{min_length, ValidationError};
use crate::enums::{
    ActionKind, CatalogValueKind, Effects, Execution, PortKind, WindowFunction,
};
use crate::pyjson::{dumps, repr_str};

/// A check of a list of port-path dependencies, named for its owner.
type DependencyCheck<'a> = dyn Fn(Option<&[String]>, &str) -> Result<(), ValidationError> + 'a;
/// A check of a port's effects, named for its path.
type EffectsCheck<'a> =
    dyn Fn(Option<&[EffectInputModel]>, &str) -> Result<(), ValidationError> + 'a;
/// A check of a blok path's root, named for its owner.
type RootCheck<'a> = dyn Fn(Option<&str>, &str) -> Result<(), ValidationError> + 'a;

/// A group of root arg ports (`PortGroupInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PortGroupInputModel {
    pub key: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub effects: Option<Vec<EffectInputModel>>,
    #[serde(default)]
    pub ports: Vec<String>,
}

impl PortGroupInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        for effect in self.effects.iter_mut().flatten() {
            effect.validate()?;
        }
        min_length(&self.key, "key")
    }
}

/// A descriptor of a candidate object (`DescriptorInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DescriptorInputModel {
    pub key: String,
    pub value: Value,
}

/// What a demanded port must look like (`PortMatchInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PortMatchInputModel {
    #[serde(default)]
    pub at: Option<i64>,
    #[serde(default)]
    pub key: Option<String>,
    #[serde(default)]
    pub kind: Option<PortKind>,
    #[serde(default)]
    pub identifier: Option<String>,
    #[serde(default)]
    pub nullable: Option<bool>,
    #[serde(default)]
    pub dimension: Option<String>,
    #[serde(default)]
    pub descriptors: Option<Vec<DescriptorInputModel>>,
    #[serde(default)]
    pub children: Option<Vec<PortMatchInputModel>>,
}

/// The criteria a demanded action satisfies (`ActionDemandInputModel`).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ActionDemandInputModel {
    #[serde(default)]
    pub hash: Option<String>,
    #[serde(default)]
    pub key: Option<String>,
    #[serde(default)]
    pub app: Option<String>,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub arg_matches: Option<Vec<PortMatchInputModel>>,
    #[serde(default)]
    pub return_matches: Option<Vec<PortMatchInputModel>>,
    #[serde(default)]
    pub protocols: Option<Vec<String>>,
    #[serde(default)]
    pub force_arg_length: Option<i64>,
    #[serde(default)]
    pub force_return_length: Option<i64>,
    #[serde(default)]
    pub pure: Option<bool>,
    #[serde(default)]
    pub idempotent: Option<bool>,
    #[serde(default)]
    pub stateful: Option<bool>,
}

/// The criteria a demanded state satisfies (`StateDemandInputModel`).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct StateDemandInputModel {
    #[serde(default)]
    pub hash: Option<String>,
    #[serde(default)]
    pub key: Option<String>,
    #[serde(default)]
    pub app: Option<String>,
    #[serde(default)]
    pub matches: Option<Vec<PortMatchInputModel>>,
    #[serde(default)]
    pub protocols: Option<Vec<String>>,
}

fn true_() -> bool {
    true
}

/// A named action requirement of a dependency (`ActionDependencyInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActionDependencyInputModel {
    pub key: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub demand: Option<ActionDemandInputModel>,
    #[serde(default)]
    pub optional: bool,
    #[serde(default = "true_")]
    pub allow_inactive: bool,
}

/// A named state requirement of a dependency (`StateDependencyInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StateDependencyInputModel {
    pub key: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub demand: Option<StateDemandInputModel>,
    #[serde(default)]
    pub optional: bool,
    #[serde(default = "true_")]
    pub allow_inactive: bool,
}

/// An agent an implementation or blok depends on (`AgentDependencyInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AgentDependencyInputModel {
    pub key: String,
    #[serde(default)]
    pub app: Option<String>,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub optional: bool,
    #[serde(default)]
    pub action_dependencies: Option<Vec<ActionDependencyInputModel>>,
    #[serde(default)]
    pub state_dependencies: Option<Vec<StateDependencyInputModel>>,
    #[serde(default)]
    pub auto_resolvable: bool,
    #[serde(default)]
    pub mutually_exclusive_keys: Option<Vec<String>>,
    #[serde(default)]
    pub min_viable_instances: Option<i64>,
    #[serde(default)]
    pub max_viable_instances: Option<i64>,
    #[serde(default)]
    pub prefered_instances: Option<i64>,
}

/// The action(s) a test action tests (`TestTargetInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TestTargetInputModel {
    #[serde(default)]
    pub hash: Option<String>,
    #[serde(default)]
    pub app: Option<String>,
    #[serde(default)]
    pub key: Option<String>,
    #[serde(default)]
    pub version: Option<String>,
}

impl TestTargetInputModel {
    pub fn validate(&self) -> Result<(), ValidationError> {
        if self.hash.as_deref().is_none_or(str::is_empty)
            && self.key.as_deref().is_none_or(str::is_empty)
        {
            return Err(ValidationError(
                "A test target must provide either a hash or a key".into(),
            ));
        }
        Ok(())
    }
}

fn one() -> String {
    "1".into()
}

fn empty_catalogs() -> Option<Vec<String>> {
    Some(vec![])
}

/// An action's public contract (`DefinitionInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DefinitionInputModel {
    pub key: String,
    #[serde(default = "one")]
    pub version: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub collections: Vec<String>,
    pub name: String,
    #[serde(default)]
    pub stateful: bool,
    #[serde(default)]
    pub pure: bool,
    #[serde(default)]
    pub idempotent: bool,
    #[serde(default)]
    pub allow_probe: bool,
    #[serde(default)]
    pub port_groups: Vec<PortGroupInputModel>,
    #[serde(default)]
    pub args: Vec<ArgPortInputModel>,
    #[serde(default)]
    pub returns: Vec<ReturnPortInputModel>,
    pub kind: ActionKind,
    #[serde(default)]
    pub is_test_for: Vec<TestTargetInputModel>,
    #[serde(default)]
    pub is_dev: bool,
    #[serde(default = "empty_catalogs")]
    pub catalogs: Option<Vec<String>>,
}

/// A port path (`a..b..c`) resolves through `children` from the root ports.
fn resolve_port_path(
    path: &str,
    args: &[ArgPortInputModel],
    returns: &[ReturnPortInputModel],
) -> bool {
    let mut segments = path.split(PORT_PATH_SEPARATOR);
    let Some(first) = segments.next() else {
        return false;
    };
    let rest: Vec<&str> = segments.collect();
    fn walk_arg(ports: &[ArgPortInputModel], first: &str, rest: &[&str]) -> Option<bool> {
        let port = ports.iter().find(|p| p.key == first)?;
        Some(match rest.split_first() {
            None => true,
            Some((next, rest)) => {
                walk_arg(port.children.as_deref().unwrap_or_default(), next, rest).unwrap_or(false)
            }
        })
    }
    fn walk_ret(ports: &[ReturnPortInputModel], first: &str, rest: &[&str]) -> Option<bool> {
        let port = ports.iter().find(|p| p.key == first)?;
        Some(match rest.split_first() {
            None => true,
            Some((next, rest)) => {
                walk_ret(port.children.as_deref().unwrap_or_default(), next, rest).unwrap_or(false)
            }
        })
    }
    // Python walks the concatenated roots [*args, *returns] and takes the first key match.
    walk_arg(args, first, &rest)
        .or_else(|| walk_ret(returns, first, &rest))
        .unwrap_or(false)
}

impl DefinitionInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        for group in &mut self.port_groups {
            group.validate()?;
        }
        for port in &mut self.args {
            port.validate()?;
        }
        for port in &mut self.returns {
            port.validate()?;
        }
        for target in &self.is_test_for {
            target.validate()?;
        }
        self.check_keys()?;
        self.check_port_groups()?;
        self.check_dependencies()
    }

    /// Root arg keys and root return keys are unique, and the two sets do not overlap.
    fn check_keys(&self) -> Result<(), ValidationError> {
        check_unique_keys(
            self.args.iter().map(|p| p.key.as_str()),
            &format!("Definition {} args", self.key),
        )?;
        check_unique_keys(
            self.returns.iter().map(|p| p.key.as_str()),
            &format!("Definition {} returns", self.key),
        )?;
        let args: HashSet<&str> = self.args.iter().map(|p| p.key.as_str()).collect();
        let mut shared: Vec<&str> = self
            .returns
            .iter()
            .map(|p| p.key.as_str())
            .filter(|k| args.contains(k))
            .collect();
        shared.sort();
        shared.dedup();
        if !shared.is_empty() {
            return Err(ValidationError(format!(
                "Definition {}: args and returns share the keys [{}]; dependency paths could not tell them apart",
                self.key,
                shared.iter().map(|k| repr_str(k)).collect::<Vec<_>>().join(", ")
            )));
        }
        Ok(())
    }

    /// Group keys are unique, every member is a root arg, and no port sits in two groups.
    fn check_port_groups(&self) -> Result<(), ValidationError> {
        check_unique_keys(
            self.port_groups.iter().map(|g| g.key.as_str()),
            &format!("Definition {} port_groups", self.key),
        )?;
        let arg_keys: HashSet<&str> = self.args.iter().map(|p| p.key.as_str()).collect();
        let mut seen: HashMap<&str, &str> = HashMap::new();
        for group in &self.port_groups {
            for key in &group.ports {
                if !arg_keys.contains(key.as_str()) {
                    return Err(ValidationError(format!(
                        "Definition {}: port group {} lists unknown arg {}",
                        self.key,
                        repr_str(&group.key),
                        repr_str(key)
                    )));
                }
                if let Some(other) = seen.get(key.as_str()) {
                    return Err(ValidationError(format!(
                        "Definition {}: arg {} is in both port groups {} and {}",
                        self.key,
                        repr_str(key),
                        repr_str(other),
                        repr_str(&group.key)
                    )));
                }
                seen.insert(key, &group.key);
            }
        }
        Ok(())
    }

    /// Every dependency of every validator, effect and widget is a resolvable port path.
    fn check_dependencies(&self) -> Result<(), ValidationError> {
        let check = |dependencies: Option<&[String]>, owner: &str| -> Result<(), ValidationError> {
            for dep in dependencies.unwrap_or_default() {
                if !resolve_port_path(dep, &self.args, &self.returns) {
                    return Err(ValidationError(format!(
                        "{owner} has invalid dependency: {dep}"
                    )));
                }
            }
            Ok(())
        };
        let check_effects =
            |effects: Option<&[EffectInputModel]>, path: &str| -> Result<(), ValidationError> {
                for effect in effects.unwrap_or_default() {
                    check(
                        effect.dependencies.as_deref(),
                        &format!(
                            "Effect {} ({}) in port {path}",
                            effect.kind, effect.call.operation
                        ),
                    )?;
                }
                Ok(())
            };

        fn walk_args(
            ports: &[ArgPortInputModel],
            prefix: &str,
            this: &DefinitionInputModel,
            check: &DependencyCheck<'_>,
            check_effects: &EffectsCheck<'_>,
        ) -> Result<(), ValidationError> {
            for port in ports {
                let path = format!("{prefix}{}", port.key);
                for validator in port.validators.iter().flatten() {
                    let label = validator
                        .label
                        .clone()
                        .filter(|l| !l.is_empty())
                        .unwrap_or_else(|| validator.call.operation.clone());
                    check(
                        validator.dependencies.as_deref(),
                        &format!("Validator {label} in port {path}"),
                    )?;
                }
                check_effects(port.effects.as_deref(), &path)?;
                if let Some(widget) = &port.widget {
                    for (depth, widget) in widget.chain().into_iter().enumerate() {
                        let owner = format!(
                            "Widget {} in port {path}{}",
                            widget.kind(),
                            if depth > 0 {
                                format!(" (fallback {depth})")
                            } else {
                                String::new()
                            }
                        );
                        check(widget.dependencies(), &owner)?;
                        if let Some(follow) = widget.follow_value() {
                            if !resolve_port_path(follow, &this.args, &this.returns) {
                                return Err(ValidationError(format!(
                                    "{owner} follows an unknown port: {follow}"
                                )));
                            }
                        }
                    }
                }
                walk_args(
                    port.children.as_deref().unwrap_or_default(),
                    &format!("{path}{PORT_PATH_SEPARATOR}"),
                    this,
                    check,
                    check_effects,
                )?;
            }
            Ok(())
        }
        fn walk_returns(
            ports: &[ReturnPortInputModel],
            prefix: &str,
            check_effects: &EffectsCheck<'_>,
        ) -> Result<(), ValidationError> {
            for port in ports {
                let path = format!("{prefix}{}", port.key);
                check_effects(port.effects.as_deref(), &path)?;
                walk_returns(
                    port.children.as_deref().unwrap_or_default(),
                    &format!("{path}{PORT_PATH_SEPARATOR}"),
                    check_effects,
                )?;
            }
            Ok(())
        }
        walk_args(&self.args, "", self, &check, &check_effects)?;
        walk_returns(&self.returns, "", &check_effects)?;
        for group in &self.port_groups {
            for effect in group.effects.iter().flatten() {
                check(
                    effect.dependencies.as_deref(),
                    &format!(
                        "Effect {} ({}) in port group {}",
                        effect.kind, effect.call.operation, group.key
                    ),
                )?;
            }
        }
        Ok(())
    }

    /// sha256 over the identity-bearing subset of `model_dump()`, as `json.dumps(…,
    /// sort_keys=True)` prints it (`unique_hash`, stored as `Action.hash`).
    pub fn unique_hash(&self) -> String {
        const IDENTITY: [&str; 11] = [
            "name",
            "description",
            "args",
            "returns",
            "stateful",
            "is_test_for",
            "collections",
            "key",
            "version",
            "kind",
            "port_groups",
        ];
        let dump = serde_json::to_value(self).expect("a definition serializes");
        let subset: serde_json::Map<String, Value> = dump
            .as_object()
            .expect("an object")
            .iter()
            .filter(|(key, _)| IDENTITY.contains(&key.as_str()))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        hex::encode(Sha256::digest(
            dumps(&Value::Object(subset), true).as_bytes(),
        ))
    }
}

/// An optimistic state write on assignment (`OptimisticInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OptimisticInputModel {
    pub state: String,
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub path_call: Option<UtilCallInputModel>,
    #[serde(default)]
    pub accessor: Option<String>,
}

impl OptimisticInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        if let Some(call) = self.path_call.as_mut() {
            call.validate()?;
        }
        if self.path.is_none() == self.path_call.is_none() {
            return Err(ValidationError(
                "Optimistic needs exactly one of path or path_call".into(),
            ));
        }
        if let Some(call) = &self.path_call {
            check_pure_call(
                call,
                Some(&[]),
                &format!("Optimistic {}", self.state),
                &["args"],
            )?;
        }
        Ok(())
    }
}

/// An aggregation over a tracked value (`WindowInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WindowInputModel {
    pub window_function: WindowFunction,
    #[serde(default)]
    pub label: Option<String>,
}

/// A state value tracked while an action runs (`TrackInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TrackInputModel {
    #[serde(default)]
    pub dependency_key: Option<String>,
    pub state_key: String,
    pub value_key: String,
    #[serde(default)]
    pub label: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub windows: Option<Vec<WindowInputModel>>,
}

fn unknown_effects() -> Effects {
    Effects::UNKNOWN
}

fn plain() -> Execution {
    Execution::PLAIN
}

/// An action as one agent implements it (`ImplementationInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ImplementationInputModel {
    pub definition: DefinitionInputModel,
    #[serde(default)]
    pub dependencies: Vec<AgentDependencyInputModel>,
    #[serde(default)]
    pub tracks: Option<Vec<TrackInputModel>>,
    pub interface: String,
    #[serde(default)]
    pub params: Option<serde_json::Map<String, Value>>,
    #[serde(default)]
    pub instance_id: Option<String>,
    #[serde(default)]
    pub locks: Option<Vec<String>>,
    #[serde(default)]
    pub optimistics: Option<Vec<OptimisticInputModel>>,
    #[serde(default)]
    pub manipulates: Option<Vec<String>>,
    #[serde(default = "true_")]
    pub needs_token: bool,
    #[serde(default)]
    pub provenance_audience: Option<Vec<String>>,
    #[serde(default = "unknown_effects")]
    pub effects: Effects,
    #[serde(default = "plain")]
    pub execution: Execution,
    #[serde(default)]
    pub code_hash: Option<String>,
}

impl ImplementationInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        self.definition.validate()?;
        for optimistic in self.optimistics.iter_mut().flatten() {
            optimistic.validate()?;
        }
        self.check_widget_targets()
    }

    /// PROXY and STATE_CHOICE widgets may only name agent dependencies this implementation declares.
    fn check_widget_targets(&self) -> Result<(), ValidationError> {
        let dependencies: HashMap<&str, &AgentDependencyInputModel> = self
            .dependencies
            .iter()
            .map(|d| (d.key.as_str(), d))
            .collect();
        fn walk(
            ports: &[ArgPortInputModel],
            prefix: &str,
            dependencies: &HashMap<&str, &AgentDependencyInputModel>,
        ) -> Result<(), ValidationError> {
            for port in ports {
                let path = format!("{prefix}{}", port.key);
                for widget in port.widget.iter().flat_map(AssignWidgetInputModel::chain) {
                    match widget {
                        AssignWidgetInputModel::StateChoice(w) => {
                            if let Some(dependency) = &w.dependency {
                                if !dependencies.contains_key(dependency.as_str()) {
                                    return Err(ValidationError(format!(
                                        "Widget STATE_CHOICE in port {path} names undeclared agent dependency {}",
                                        repr_str(dependency)
                                    )));
                                }
                            }
                        }
                        AssignWidgetInputModel::Proxy(w) => {
                            if let Some(target) = &w.target_dependency {
                                let Some(dependency) = dependencies.get(target.as_str()) else {
                                    return Err(ValidationError(format!(
                                        "Widget PROXY in port {path} names undeclared agent dependency {}",
                                        repr_str(target)
                                    )));
                                };
                                let mut actions: Vec<&str> = dependency
                                    .action_dependencies
                                    .iter()
                                    .flatten()
                                    .map(|a| a.key.as_str())
                                    .collect();
                                actions.sort();
                                actions.dedup();
                                if !actions.is_empty()
                                    && !actions.contains(&w.target_action.as_str())
                                {
                                    return Err(ValidationError(format!(
                                        "Widget PROXY in port {path} targets action {}, which dependency {} does not declare (it declares [{}])",
                                        repr_str(&w.target_action),
                                        repr_str(target),
                                        actions.iter().map(|a| repr_str(a)).collect::<Vec<_>>().join(", ")
                                    )));
                                }
                            }
                        }
                        _ => {}
                    }
                }
                walk(
                    port.children.as_deref().unwrap_or_default(),
                    &format!("{path}{PORT_PATH_SEPARATOR}"),
                    dependencies,
                )?;
            }
            Ok(())
        }
        walk(&self.definition.args, "", &dependencies)
    }
}

/// A state's schema (`StateDefinitionInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StateDefinitionInputModel {
    pub ports: Vec<ReturnPortInputModel>,
    pub name: String,
}

/// A state as one agent holds it (`StateImplementationInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StateImplementationInputModel {
    pub interface: String,
    #[serde(default)]
    pub key: Option<String>,
    #[serde(default)]
    pub app: Option<String>,
    pub definition: StateDefinitionInputModel,
}

impl StateImplementationInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        for port in &mut self.definition.ports {
            port.validate()?;
        }
        Ok(())
    }
}

/// A lock's description (`LockDefinitionInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LockDefinitionInputModel {
    pub key: String,
    #[serde(default)]
    pub description: Option<String>,
}

/// A lock an agent holds (`LockImplementationInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LockImplementationInputModel {
    pub key: String,
    pub definition: LockDefinitionInputModel,
}

/// Coherence of a blok component tree (`check_blok_manifest`): unique component ids and
/// declared values, agent calls only to declared dependencies, and (with a demo state) every
/// path rooted in a demo-state key, a declared value or a dependency key.
pub fn check_blok_manifest(
    components: Option<&[ComponentNodeInputModel]>,
    dependency_keys: &HashSet<String>,
    state_keys: Option<&HashSet<String>>,
) -> Result<(), ValidationError> {
    let mut ids = HashSet::new();
    let mut declared = HashSet::new();
    for node in iter_component_nodes(components) {
        if !ids.insert(node.id.as_str()) {
            return Err(ValidationError(format!(
                "Blok manifest: duplicate component id {}",
                repr_str(&node.id)
            )));
        }
        for prop in node.props.iter().flatten() {
            if let Some(value) = prop.declares_value.as_deref().filter(|v| !v.is_empty()) {
                if !declared.insert(value.to_owned()) {
                    return Err(ValidationError(format!(
                        "Blok manifest: value {} declared twice",
                        repr_str(value)
                    )));
                }
            }
        }
    }
    let roots: Option<HashSet<String>> = state_keys.map(|keys| {
        keys.iter()
            .chain(declared.iter())
            .chain(dependency_keys.iter())
            .cloned()
            .collect()
    });
    let check_root = |path: Option<&str>, owner: &str| -> Result<(), ValidationError> {
        let (Some(path), Some(roots)) = (path, roots.as_ref()) else {
            return Ok(());
        };
        let root = blok_path_root(path);
        if !roots.contains(root) {
            return Err(ValidationError(format!(
                "{owner} references {} but it is neither a demo_state key, a declared value nor a dependency key",
                repr_str(root)
            )));
        }
        Ok(())
    };
    fn walk_arguments(
        arguments: Option<&[ActionArgumentInputModel]>,
        owner: &str,
        dependency_keys: &HashSet<String>,
        check_root: &RootCheck<'_>,
    ) -> Result<(), ValidationError> {
        for argument in arguments.unwrap_or_default() {
            check_root(argument.value_path.as_deref(), owner)?;
            if let Some(call) = &argument.agent_call {
                check_agent_call(call, owner, dependency_keys, check_root)?;
            }
            if let Some(call) = &argument.util_call {
                walk_arguments(
                    call.arguments.as_deref(),
                    owner,
                    dependency_keys,
                    check_root,
                )?;
            }
            walk_arguments(
                argument.value_list.as_deref(),
                owner,
                dependency_keys,
                check_root,
            )?;
            walk_arguments(
                argument.value_dict.as_deref(),
                owner,
                dependency_keys,
                check_root,
            )?;
        }
        Ok(())
    }
    fn check_agent_call(
        call: &AgentProbeInputModel,
        owner: &str,
        dependency_keys: &HashSet<String>,
        check_root: &RootCheck<'_>,
    ) -> Result<(), ValidationError> {
        if !dependency_keys.contains(&call.dependency) {
            return Err(ValidationError(format!(
                "{owner}: agent_call targets undeclared dependency {}",
                repr_str(&call.dependency)
            )));
        }
        walk_arguments(
            call.arguments.as_deref(),
            owner,
            dependency_keys,
            check_root,
        )
    }
    for node in iter_component_nodes(components) {
        for prop in node.props.iter().flatten() {
            let owner = format!(
                "prop {} of component {}",
                repr_str(&prop.key),
                repr_str(&node.id)
            );
            if let Some(dynamic) = &prop.dynamic_value {
                check_root(dynamic.path.as_deref(), &owner)?;
            }
            if let Some(call) = &prop.agent_call {
                check_agent_call(call, &owner, dependency_keys, &check_root)?;
            }
            if let Some(call) = &prop.util_call {
                walk_arguments(
                    call.arguments.as_deref(),
                    &owner,
                    dependency_keys,
                    &check_root,
                )?;
            }
        }
    }
    Ok(())
}

/// A blok an agent declares (`BlokImplementationInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BlokImplementationInputModel {
    pub key: String,
    #[serde(default)]
    pub dependencies: Vec<AgentDependencyInputModel>,
    pub components: Vec<ComponentNodeInputModel>,
    #[serde(default)]
    pub catalog: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub demo_state: Option<serde_json::Map<String, Value>>,
}

impl BlokImplementationInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        for component in &mut self.components {
            component.validate()?;
        }
        let dependency_keys: HashSet<String> =
            self.dependencies.iter().map(|d| d.key.clone()).collect();
        let state_keys: Option<HashSet<String>> = self
            .demo_state
            .as_ref()
            .map(|s| s.keys().cloned().collect());
        check_blok_manifest(
            Some(&self.components),
            &dependency_keys,
            state_keys.as_ref(),
        )
    }
}

/// A catalog's default widget for ports of a kind and/or identifier (`WidgetDefaultInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WidgetDefaultInputModel {
    #[serde(default)]
    pub kind: Option<PortKind>,
    #[serde(default)]
    pub identifier: Option<String>,
    #[serde(default)]
    pub widget: Option<AssignWidgetInputModel>,
    #[serde(default)]
    pub return_widget: Option<ReturnWidgetInputModel>,
}

impl WidgetDefaultInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        if let Some(widget) = self.widget.as_mut() {
            widget.validate()?;
        }
        if let Some(widget) = self.return_widget.as_mut() {
            widget.validate()?;
        }
        if self.kind.is_none() && self.identifier.is_none() {
            return Err(ValidationError(
                "WidgetDefault needs a kind and/or an identifier to match ports on".into(),
            ));
        }
        if self.widget.is_none() && self.return_widget.is_none() {
            return Err(ValidationError(
                "WidgetDefault needs a widget and/or a return_widget".into(),
            ));
        }
        Ok(())
    }
}

/// A prop a catalog component accepts (`CatalogPropInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CatalogPropInputModel {
    pub key: String,
    pub kind: CatalogValueKind,
    #[serde(default)]
    pub required: bool,
    #[serde(default)]
    pub description: Option<String>,
}

/// A component a UI can render (`CatalogComponentInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CatalogComponentInputModel {
    pub name: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub props: Vec<CatalogPropInputModel>,
    #[serde(default = "true_")]
    pub accepts_children: bool,
}

impl CatalogComponentInputModel {
    pub fn validate(&self) -> Result<(), ValidationError> {
        min_length(&self.name, "name")?;
        for prop in &self.props {
            min_length(&prop.key, "key")?;
        }
        check_unique_keys(
            self.props.iter().map(|p| p.key.as_str()),
            &format!("catalog component {}", self.name),
        )
    }
}

/// An argument a catalog operation takes (`CatalogArgumentInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CatalogArgumentInputModel {
    pub key: String,
    pub kind: CatalogValueKind,
    #[serde(default = "true_")]
    pub required: bool,
    #[serde(default)]
    pub description: Option<String>,
}

/// An operation a UI can evaluate (`CatalogOperationInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CatalogOperationInputModel {
    pub name: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub arguments: Vec<CatalogArgumentInputModel>,
    pub returns: CatalogValueKind,
}

impl CatalogOperationInputModel {
    pub fn validate(&self) -> Result<(), ValidationError> {
        min_length(&self.name, "name")?;
        for argument in &self.arguments {
            min_length(&argument.key, "key")?;
        }
        check_unique_keys(
            self.arguments.iter().map(|a| a.key.as_str()),
            &format!("catalog operation {}", self.name),
        )
    }
}

/// The implement-agent payload a REGISTER carries (`ImplementAgentInputModel`).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ImplementAgentInputModel {
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub hash: Option<String>,
    #[serde(default)]
    pub implementations: Option<Vec<ImplementationInputModel>>,
    #[serde(default)]
    pub states: Option<Vec<StateImplementationInputModel>>,
    #[serde(default)]
    pub locks: Option<Vec<LockImplementationInputModel>>,
    #[serde(default)]
    pub bloks: Option<Vec<BlokImplementationInputModel>>,
}

impl ImplementAgentInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        for implementation in self.implementations.iter_mut().flatten() {
            implementation.validate()?;
        }
        for state in self.states.iter_mut().flatten() {
            state.validate()?;
        }
        for blok in self.bloks.iter_mut().flatten() {
            blok.validate()?;
        }
        Ok(())
    }

    /// Whether this declares anything to reconcile.
    pub fn declares(&self) -> bool {
        self.hash.is_some()
            || self.implementations.is_some()
            || self.states.is_some()
            || self.locks.is_some()
            || self.bloks.is_some()
    }
}
