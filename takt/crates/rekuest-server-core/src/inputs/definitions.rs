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
    ActionKind, AssetKind, CatalogValueKind, ComponentContext, Effects, Execution, PortKind,
    WindowFunction,
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
    /// Where the action means to hook in (`on_scene_annotation`): hints a caller
    /// filters on, stored beside the inferred protocols. Not a contract, and not
    /// part of `unique_hash`.
    #[serde(default)]
    pub protocols: Vec<String>,
    /// The action's own bloks, at most one per context: how a client shows a run of it. Not
    /// part of `unique_hash`. Left out of its dump when empty, as before actions had bloks.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub bloks: Vec<ActionBlokInputModel>,
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
        for blok in &mut self.bloks {
            blok.validate()?;
        }
        self.check_bloks()?;
        self.check_keys()?;
        self.check_port_groups()?;
        self.check_protocols()?;
        self.check_dependencies()
    }

    /// An action has one blok per context (`check_bloks`).
    fn check_bloks(&self) -> Result<(), ValidationError> {
        let mut seen: HashSet<ComponentContext> = HashSet::new();
        for blok in &self.bloks {
            if !seen.insert(blok.context) {
                return Err(ValidationError(format!(
                    "Definition {}: the {} blok is given twice; an action has one blok per context",
                    self.key,
                    blok.context.value().to_lowercase()
                )));
            }
        }
        Ok(())
    }

    /// A protocol hint is a name to filter on: one word, said once.
    fn check_protocols(&self) -> Result<(), ValidationError> {
        let mut seen: HashSet<&str> = HashSet::new();
        for protocol in &self.protocols {
            // `facade_protocol.name` is a varchar(1000).
            if protocol.is_empty()
                || protocol.chars().any(char::is_whitespace)
                || protocol.chars().count() > 1000
            {
                return Err(ValidationError(format!(
                    "Definition {}: a protocol hint is a name without whitespace, got {}",
                    self.key,
                    repr_str(protocol)
                )));
            }
            if !seen.insert(protocol.as_str()) {
                return Err(ValidationError(format!(
                    "Definition {}: the protocol hint {} is given twice",
                    self.key,
                    repr_str(protocol)
                )));
            }
        }
        Ok(())
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
    /// The key of the agent's action logo blok (context ACTION_LOGO) that draws this action.
    #[serde(default)]
    pub logo: Option<String>,
}

impl ImplementationInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        self.definition.validate()?;
        for optimistic in self.optimistics.iter_mut().flatten() {
            optimistic.validate()?;
        }
        self.check_widget_targets()?;
        // The action's bloks name devices by the dependency keys this implementation declares.
        let dependency_keys: HashSet<String> =
            self.dependencies.iter().map(|d| d.key.clone()).collect();
        for blok in &self.definition.bloks {
            check_action_blok(blok, &dependency_keys)?;
        }
        Ok(())
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

/// The root a handler's arguments read the value that triggered them from (`$event/value`).
pub const BLOK_EVENT_ROOT: &str = "$event";

/// What follows a dependency's key in the path of one of its assets (`self.assets.scope`).
pub const BLOK_ASSETS_SEGMENT: &str = "assets";

/// The `(dependency key, asset name)` of `<dependency>.assets.<name>`, or `None` if `path` is
/// not exactly that (`blok_asset_path`).
pub fn blok_asset_path(path: &str) -> Option<(&str, &str)> {
    let mut parts = path.split('.');
    match (parts.next(), parts.next(), parts.next(), parts.next()) {
        (Some(dependency), Some(BLOK_ASSETS_SEGMENT), Some(name), None)
            if !dependency.is_empty() && !name.is_empty() =>
        {
            Some((dependency, name))
        }
        _ => None,
    }
}

/// A file an agent brings with it: the name its bloks call it by, and what the bytes are
/// (`AssetInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AssetInputModel {
    pub name: String,
    pub kind: AssetKind,
    #[serde(rename = "ref")]
    pub reference: String,
    /// Required unless the asset names a `source`: an agent knows the size of a file it holds.
    #[serde(default)]
    pub size: Option<i64>,
    /// The https URL the file can be fetched from, given instead of holding the file.
    #[serde(default)]
    pub source: Option<String>,
}

/// The longest `source` an asset may name (`ASSET_SOURCE_MAX_LENGTH`).
pub const ASSET_SOURCE_MAX_LENGTH: usize = 2000;

impl AssetInputModel {
    pub fn validate(&self) -> Result<(), ValidationError> {
        let mut letters = self.name.chars();
        let identifier = letters
            .next()
            .is_some_and(|first| first == '_' || first.is_alphabetic())
            && letters.all(|letter| letter == '_' || letter.is_alphanumeric());
        if !identifier {
            return Err(ValidationError(format!(
                "Asset: the name {} is not an identifier",
                repr_str(&self.name)
            )));
        }
        if !is_model_ref(&self.reference) {
            return Err(ValidationError(format!(
                "Asset {}: the reference must be 'sha256:' and 64 lowercase hex characters, not {}",
                repr_str(&self.name),
                repr_str(&self.reference)
            )));
        }
        if let Some(source) = &self.source {
            let length = source.chars().count();
            if !(source.starts_with("https://")
                && length > "https://".len()
                && length <= ASSET_SOURCE_MAX_LENGTH)
            {
                return Err(ValidationError(format!(
                    "Asset {}: a source is an https URL of at most {} characters",
                    repr_str(&self.name),
                    ASSET_SOURCE_MAX_LENGTH
                )));
            }
        }
        if self.size.map_or(self.source.is_none(), |size| size <= 0) {
            return Err(ValidationError(format!(
                "Asset {}: an asset has a size",
                repr_str(&self.name)
            )));
        }
        Ok(())
    }
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
            .chain(std::iter::once(BLOK_EVENT_ROOT.to_owned()))
            .collect()
    });
    let check_root = |path: Option<&str>, owner: &str| -> Result<(), ValidationError> {
        let Some(path) = path else {
            return Ok(());
        };
        // A file of a declared dependency: there is no value at this path to resolve.
        if blok_asset_path(path).is_some_and(|(dependency, _)| dependency_keys.contains(dependency))
        {
            return Ok(());
        }
        let Some(roots) = roots.as_ref() else {
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
    /// The tree, unless the blok is sent as its `source`; `validate` parses that into this.
    #[serde(default)]
    pub components: Option<Vec<ComponentNodeInputModel>>,
    /// The blok as BSX, three times fewer bytes than its tree. Exactly one of the two is sent.
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub catalog: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub demo_state: Option<serde_json::Map<String, Value>>,
    /// The renderer for the blok's root: HTML, or SCENE for a full 3D view.
    #[serde(default = "html_")]
    pub context: ComponentContext,
    /// Whether this is its agent's default blok for its context.
    #[serde(default)]
    pub default: bool,
    /// The model (`sha256:...`) this blok decorates. HTML bloks only.
    #[serde(default)]
    pub for_model: Option<String>,
    /// The node of `for_model` this blok attaches to; the model's root when absent.
    #[serde(default)]
    pub for_node: Option<String>,
    /// The items this blok is opened for, read under `@args.<key>`. HTML bloks only.
    #[serde(default)]
    pub args: Vec<ArgPortInputModel>,
    /// What a person answers through this blok when a task asks with it: the values its tree
    /// hands back with `blok.answer`. Left out of its dump when empty, as before bloks had returns.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub returns: Vec<ReturnPortInputModel>,
    /// The backend services its `query` components read from, each frozen at the version it
    /// was written against.
    #[serde(default)]
    pub services: Vec<BlokServiceInputModel>,
    /// The locals a renderer may keep between openings, on the device: each a top-level
    /// `demo_state` key, whose demo value is the default.
    #[serde(default)]
    pub persist: Vec<BlokPersistInputModel>,
}

/// A local a renderer may keep between openings, on the device (`BlokPersistInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlokPersistInputModel {
    /// The local: a top-level `demo_state` key that is neither a dependency nor a reserved root.
    pub key: String,
    /// What the kept value is, for whoever resets it.
    #[serde(default)]
    pub description: Option<String>,
}

/// A backend service a blok reads from, frozen at the version it was written against
/// (`BlokServiceInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlokServiceInputModel {
    /// The service's key, as a `query`'s `schema` names it: `mikro`.
    pub key: String,
    /// The service type, in reverse-domain form: `live.arkitekt.mikro`.
    #[serde(default)]
    pub service: Option<String>,
    /// The server version the blok was written against, as the service declaration names it.
    #[serde(default)]
    pub version: Option<String>,
    /// `sha256:<hex>` of the canonical GraphQL schema the blok's queries were checked against.
    #[serde(default)]
    pub schema_hash: Option<String>,
}

/// The base component that reads a service (`QUERY_COMPONENT`).
pub const QUERY_COMPONENT: &str = "query";

/// A blok names each service once, and every `query` in it runs on one it names
/// (`check_blok_services`).
pub fn check_blok_services(
    components: Option<&[ComponentNodeInputModel]>,
    services: &[BlokServiceInputModel],
) -> Result<(), ValidationError> {
    for service in services {
        min_length(&service.key, "key")?;
    }
    check_unique_keys(
        services.iter().map(|s| s.key.as_str()),
        "Blok manifest services",
    )?;
    let declared: HashSet<&str> = services.iter().map(|s| s.key.as_str()).collect();
    for node in iter_component_nodes(components) {
        if node.component != QUERY_COMPONENT {
            continue;
        }
        let schema = node
            .props
            .iter()
            .flatten()
            .find(|prop| prop.key == "schema")
            .and_then(|prop| prop.static_value.as_ref())
            .and_then(Value::as_str);
        if let Some(schema) = schema.filter(|s| !declared.contains(s)) {
            let mut known: Vec<&str> = declared.iter().copied().collect();
            known.sort_unstable();
            let known = if known.is_empty() {
                "none".to_owned()
            } else {
                format!(
                    "[{}]",
                    known
                        .iter()
                        .map(|k| repr_str(k))
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            };
            return Err(ValidationError(format!(
                "Blok manifest: component {} queries the {} service, which the blok does not declare in its services (declared: {known})",
                repr_str(&node.id),
                repr_str(schema)
            )));
        }
    }
    Ok(())
}

/// The root a blok reads its args under (`BLOK_ARGS_ROOT`).
pub const BLOK_ARGS_ROOT: &str = "args";

/// A blok's args are an html blok's, keyed once, and no dependency takes their root (`check_blok_args`).
pub fn check_blok_args(
    context: ComponentContext,
    args: &[ArgPortInputModel],
    dependency_keys: &HashSet<String>,
) -> Result<(), ValidationError> {
    if args.is_empty() {
        return Ok(());
    }
    if context != ComponentContext::HTML {
        return Err(ValidationError(format!(
            "Blok manifest: only an html blok takes args, not a {} blok",
            context.value()
        )));
    }
    check_unique_keys(args.iter().map(|p| p.key.as_str()), "Blok manifest args")?;
    if dependency_keys.contains(BLOK_ARGS_ROOT) {
        return Err(ValidationError(format!(
            "Blok manifest: a blok with args reads them under {}, so no dependency can be called that",
            repr_str(BLOK_ARGS_ROOT)
        )));
    }
    Ok(())
}

/// A blok persists only its locals, each once (`check_blok_persist`): a top-level `demo_state`
/// key, which is not a dependency's key nor a root the renderer fills (`args`, `$event`).
pub fn check_blok_persist(
    persist: &[BlokPersistInputModel],
    dependency_keys: &HashSet<String>,
    state_keys: Option<&HashSet<String>>,
) -> Result<(), ValidationError> {
    for local in persist {
        min_length(&local.key, "key")?;
    }
    check_unique_keys(
        persist.iter().map(|p| p.key.as_str()),
        "Blok manifest persist",
    )?;
    for local in persist {
        let key = local.key.as_str();
        if key == BLOK_ARGS_ROOT || key == BLOK_EVENT_ROOT || dependency_keys.contains(key) {
            return Err(ValidationError(format!(
                "Blok manifest: persist names {}, which is not a local but a root the renderer fills",
                repr_str(key)
            )));
        }
        if !state_keys.is_some_and(|keys| keys.contains(key)) {
            return Err(ValidationError(format!(
                "Blok manifest: persist names {}, which is not a demo_state key: a persisted local takes its demo value as its default",
                repr_str(key)
            )));
        }
    }
    Ok(())
}

/// Whether `value` is `sha256:` and 64 lowercase hex characters (`MODEL_REF_PATTERN`).
pub fn is_model_ref(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

/// A blok that decorates a model names it by reference, and is an html blok (`check_blok_target`).
pub fn check_blok_target(
    context: ComponentContext,
    for_model: Option<&str>,
    for_node: Option<&str>,
) -> Result<(), ValidationError> {
    let Some(for_model) = for_model else {
        if for_node.is_some() {
            return Err(ValidationError(
                "Blok manifest: for_node needs a for_model".into(),
            ));
        }
        return Ok(());
    };
    if !is_model_ref(for_model) {
        return Err(ValidationError(format!(
            "Blok manifest: for_model must be 'sha256:' and 64 lowercase hex characters, not {}",
            repr_str(for_model)
        )));
    }
    if context != ComponentContext::HTML {
        return Err(ValidationError(
            "Blok manifest: only an html blok can decorate a model".into(),
        ));
    }
    Ok(())
}

impl BlokImplementationInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        match (self.components.is_some(), self.source.take()) {
            (true, Some(_)) => {
                return Err(ValidationError(
                    "a blok is sent as its components or as its BSX source, not both".to_string(),
                ))
            }
            (false, None) => {
                return Err(ValidationError(
                    "a blok is sent as its components or as its BSX source".to_string(),
                ))
            }
            (false, Some(source)) => {
                let tree = crate::bsx::parse_bsx(&source).map_err(ValidationError)?;
                self.components = Some(vec![tree]);
            }
            (true, None) => {}
        }
        for component in self.components.iter_mut().flatten() {
            component.validate()?;
        }
        for port in &mut self.args {
            port.validate()?;
        }
        for port in &mut self.returns {
            port.validate()?;
        }
        let dependency_keys: HashSet<String> =
            self.dependencies.iter().map(|d| d.key.clone()).collect();
        let state_keys: Option<HashSet<String>> = self
            .demo_state
            .as_ref()
            .map(|s| s.keys().cloned().collect());
        check_blok_manifest(
            self.components.as_deref(),
            &dependency_keys,
            state_keys.as_ref(),
        )?;
        check_blok_target(
            self.context,
            self.for_model.as_deref(),
            self.for_node.as_deref(),
        )?;
        check_blok_args(self.context, &self.args, &dependency_keys)?;
        check_blok_persist(&self.persist, &dependency_keys, state_keys.as_ref())?;
        check_blok_services(self.components.as_deref(), &self.services)?;
        if self.context == ComponentContext::OVERLAY {
            return Err(ValidationError(
                "Blok manifest: an overlay blok is an action's own (its definition's `bloks`), drawn into the space that hosts a run of it; an agent does not declare one".into(),
            ));
        }
        Ok(())
    }
}

/// The root an action's blok reads the run it is shown for under (`ACTION_BLOK_TASK_ROOT`).
pub const ACTION_BLOK_TASK_ROOT: &str = "task";
/// The key an action's blok reads the agent running the action under (`ACTION_BLOK_SELF_KEY`).
pub const ACTION_BLOK_SELF_KEY: &str = "self";
/// The component anchoring its children at the agent bound to a dependency (`AT_COMPONENT`).
pub const AT_COMPONENT: &str = "At";
/// The component drawing a line through the agents bound to dependencies (`ROUTE_COMPONENT`).
pub const ROUTE_COMPONENT: &str = "Route";

/// An action's own blok for one context (`ActionBlokInputModel`): no name, never listed on its
/// own, its dependencies the action's, named by key.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ActionBlokInputModel {
    /// HTML (a task panel), SCENE (a 3D view of its own) or OVERLAY (drawn into the hosting space).
    #[serde(default = "html_")]
    pub context: ComponentContext,
    /// The tree, unless the blok is sent as its `source`; `validate` parses that into this.
    #[serde(default)]
    pub components: Option<Vec<ComponentNodeInputModel>>,
    /// The blok as BSX. Exactly one of the two is sent.
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub catalog: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    /// Its own locals and their initial values.
    #[serde(default)]
    pub demo_state: Option<serde_json::Map<String, Value>>,
    /// The locals a renderer may keep between openings, on the device.
    #[serde(default)]
    pub persist: Vec<BlokPersistInputModel>,
}

impl ActionBlokInputModel {
    /// Sent one way (its source parsed into its tree), in a context an action has a blok for,
    /// its locals none of the roots the renderer fills (`check_action_blok` of the model).
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        match (self.components.is_some(), self.source.take()) {
            (true, Some(_)) => {
                return Err(ValidationError(
                    "a blok is sent as its components or as its BSX source, not both".to_string(),
                ))
            }
            (false, None) => {
                return Err(ValidationError(
                    "a blok is sent as its components or as its BSX source".to_string(),
                ))
            }
            (false, Some(source)) => {
                let tree = crate::bsx::parse_bsx(&source).map_err(ValidationError)?;
                self.components = Some(vec![tree]);
            }
            (true, None) => {}
        }
        for component in self.components.iter_mut().flatten() {
            component.validate()?;
        }
        for local in &self.persist {
            min_length(&local.key, "key")?;
        }
        if !matches!(
            self.context,
            ComponentContext::HTML | ComponentContext::SCENE | ComponentContext::OVERLAY
        ) {
            return Err(ValidationError(format!(
                "Blok manifest: an action's blok is html, scene or overlay, not {}",
                self.context.value().to_lowercase()
            )));
        }
        let mut filled: Vec<&str> = self
            .demo_state
            .iter()
            .flat_map(|state| state.keys())
            .map(String::as_str)
            .filter(|key| {
                [
                    ACTION_BLOK_TASK_ROOT,
                    BLOK_ARGS_ROOT,
                    ACTION_BLOK_SELF_KEY,
                    BLOK_EVENT_ROOT,
                ]
                .contains(key)
            })
            .collect();
        filled.sort_unstable();
        if let Some(first) = filled.first() {
            return Err(ValidationError(format!(
                "Blok manifest: demo_state names {}, which the renderer fills for an action's blok",
                repr_str(first)
            )));
        }
        Ok(())
    }
}

/// The dependency keys a `through` prop names (`through_keys`): `"['robot', 'frame']"` (as BSX
/// writes a list), `'["robot"]'` or `"robot, frame"`.
pub fn through_keys(value: &str) -> Vec<String> {
    let mut inner = value.trim();
    if inner.starts_with('[') && inner.ends_with(']') && inner.len() >= 2 {
        inner = &inner[1..inner.len() - 1];
    }
    inner
        .split(',')
        .filter(|item| !item.trim().is_empty())
        .map(|item| {
            item.trim()
                .trim_matches(|c| c == '\'' || c == '"')
                .trim()
                .to_owned()
        })
        .collect()
}

/// Every `<At dependency>` and `<Route through>` names dependencies of the action
/// (`check_action_blok_anchors`). Bound dynamically, they are left to the renderer.
pub fn check_action_blok_anchors(
    components: Option<&[ComponentNodeInputModel]>,
    dependency_keys: &HashSet<String>,
) -> Result<(), ValidationError> {
    for node in iter_component_nodes(components) {
        for prop in node.props.iter().flatten() {
            let Some(Value::String(value)) = &prop.static_value else {
                continue;
            };
            let named = if node.component == AT_COMPONENT && prop.key == "dependency" {
                vec![value.clone()]
            } else if node.component == ROUTE_COMPONENT && prop.key == "through" {
                through_keys(value)
            } else {
                continue;
            };
            for key in named {
                if !dependency_keys.contains(&key) {
                    let mut known: Vec<&String> = dependency_keys.iter().collect();
                    known.sort_unstable();
                    let known = if known.is_empty() {
                        "none".to_owned()
                    } else {
                        known
                            .iter()
                            .map(|k| repr_str(k))
                            .collect::<Vec<_>>()
                            .join(", ")
                    };
                    return Err(ValidationError(format!(
                        "Blok manifest: component {} names {}, which is not a dependency of the action (its dependencies: {known})",
                        repr_str(&node.id),
                        repr_str(&key)
                    )));
                }
            }
        }
    }
    Ok(())
}

/// An action's blok against the dependencies its implementation declares (`check_action_blok`):
/// its path roots are those dependencies, `self`, `task`, `args`, its own locals and `$event`;
/// `At`/`Route` name only the dependencies.
pub fn check_action_blok(
    blok: &ActionBlokInputModel,
    dependency_keys: &HashSet<String>,
) -> Result<(), ValidationError> {
    let mut reserved: Vec<&str> = [ACTION_BLOK_TASK_ROOT, BLOK_ARGS_ROOT]
        .into_iter()
        .filter(|root| dependency_keys.contains(*root))
        .collect();
    reserved.sort_unstable();
    if let Some(first) = reserved.first() {
        return Err(ValidationError(format!(
            "Blok manifest: an action's blok reads its run under {} and the run's args under {}, so no dependency can be called {}",
            repr_str(ACTION_BLOK_TASK_ROOT),
            repr_str(BLOK_ARGS_ROOT),
            repr_str(first)
        )));
    }
    let mut keys = dependency_keys.clone();
    keys.insert(ACTION_BLOK_SELF_KEY.to_owned());
    let state_keys: HashSet<String> = blok
        .demo_state
        .iter()
        .flat_map(|state| state.keys().cloned())
        .collect();
    let mut shadowed: Vec<&String> = state_keys.intersection(&keys).collect();
    shadowed.sort_unstable();
    if let Some(first) = shadowed.first() {
        return Err(ValidationError(format!(
            "Blok manifest: demo_state names {}, which is a dependency of the action, not a local",
            repr_str(first)
        )));
    }
    check_blok_persist(&blok.persist, &keys, Some(&state_keys))?;
    if blok.components.is_none() {
        return Ok(());
    }
    let mut roots = state_keys.clone();
    roots.insert(ACTION_BLOK_TASK_ROOT.to_owned());
    roots.insert(BLOK_ARGS_ROOT.to_owned());
    check_blok_manifest(blok.components.as_deref(), &keys, Some(&roots))?;
    check_action_blok_anchors(blok.components.as_deref(), dependency_keys)
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
    /// The context this component may be placed in.
    #[serde(default = "html_")]
    pub context: ComponentContext,
    /// The context of its children when it differs from its own: the component bridges two renderers.
    #[serde(default)]
    pub children_context: Option<ComponentContext>,
    /// Placed in any context, taking its parent's for itself and its children (control flow).
    #[serde(default)]
    pub inherits_context: bool,
    /// At least one of these must be an ancestor, at any depth. Empty means no requirement.
    #[serde(default)]
    pub downstream_of: Vec<String>,
    /// None of these may be an ancestor.
    #[serde(default)]
    pub not_downstream_of: Vec<String>,
}

fn html_() -> ComponentContext {
    ComponentContext::HTML
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
        )?;
        // ACTION_LOGO and OVERLAY are a blok's contexts, never a component's
        // (`check_no_action_logo_context`).
        if self.context == ComponentContext::ACTION_LOGO
            || self.children_context == Some(ComponentContext::ACTION_LOGO)
        {
            return Err(ValidationError(format!(
                "catalog component {}: ACTION_LOGO is a blok context only; its shapes are LOGO components",
                self.name
            )));
        }
        if self.context == ComponentContext::OVERLAY
            || self.children_context == Some(ComponentContext::OVERLAY)
        {
            return Err(ValidationError(format!(
                "catalog component {}: OVERLAY is a blok context only; what an overlay draws are SCENE components",
                self.name
            )));
        }
        Ok(())
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
    /// The files the agent brings, by name. It uploads the ones the server does not hold.
    #[serde(default)]
    pub assets: Option<Vec<AssetInputModel>>,
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
        // An implementation's logo is an action logo blok the agent declares (`check_logos`).
        let logos: HashSet<&str> = self
            .bloks
            .iter()
            .flatten()
            .filter(|blok| blok.context == ComponentContext::ACTION_LOGO)
            .map(|blok| blok.key.as_str())
            .collect();
        for implementation in self.implementations.iter().flatten() {
            if let Some(logo) = implementation.logo.as_deref() {
                if !logos.contains(logo) {
                    return Err(ValidationError(format!(
                        "implementation {} is drawn by the logo {}, which is not an action logo blok this agent declares",
                        repr_str(&implementation.interface),
                        repr_str(logo)
                    )));
                }
            }
        }
        let mut names = HashSet::new();
        for asset in self.assets.iter().flatten() {
            asset.validate()?;
            if !names.insert(asset.name.as_str()) {
                return Err(ValidationError(format!(
                    "Asset {} is declared twice",
                    repr_str(&asset.name)
                )));
            }
        }
        // Every dependency key of a declared blok is mapped to the declaring agent, so an
        // asset a blok names is one of this declaration's, whatever key it is named under.
        for blok in self.bloks.iter().flatten() {
            for node in iter_component_nodes(blok.components.as_deref()) {
                for prop in node.props.iter().flatten() {
                    let Some(path) = prop.dynamic_value.as_ref().and_then(|d| d.path.as_deref())
                    else {
                        continue;
                    };
                    let Some((dependency, name)) = blok_asset_path(path) else {
                        continue;
                    };
                    if blok.dependencies.iter().any(|d| d.key == dependency)
                        && !names.contains(name)
                    {
                        let mut declared: Vec<&str> = names.iter().copied().collect();
                        declared.sort_unstable();
                        return Err(ValidationError(format!(
                            "Blok {}: prop {} of component {} names the asset {}, which the agent does not declare (declares {declared:?})",
                            repr_str(&blok.key),
                            repr_str(&prop.key),
                            repr_str(&node.id),
                            repr_str(name)
                        )));
                    }
                }
            }
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
            || self.assets.is_some()
    }
}
