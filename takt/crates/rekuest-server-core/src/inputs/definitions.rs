//! Definitions, dependencies and implementations (`rekuest_core/inputs/models.py`, the
//! declaration half).

use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::bloks::{
    check_blok_declarations, check_blok_sent_once, wrong_context, BlokDeclarations,
    BlokDemoInputModel, BlokLocalInputModel, BlokQueryInputModel, BlokSubscriptionInputModel,
};
use super::calls::{
    check_pure_call, iter_component_nodes, ComponentNodeInputModel, UtilCallInputModel,
    PORT_PATH_SEPARATOR,
};
use super::ports::{
    check_unique_keys, ArgPortInputModel, AssignWidgetInputModel, EffectInputModel,
    ReturnPortInputModel, ReturnWidgetInputModel,
};
use super::{min_length, ValidationError};
use crate::blok::check::validate_blok;
use crate::blok::{BlokScope, Finding};
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
    /// The bloks a task asks a person through, declared up front: it names one by its key.
    #[serde(default)]
    pub asks: Option<Vec<AskBlokInputModel>>,
    /// The bloks a task shows a person while it goes on, declared up front: it names one by its key.
    #[serde(default)]
    pub informs: Option<Vec<InformBlokInputModel>>,
    /// Whether a task may also ask through a blok made at ask time, or any of the organization.
    #[serde(default)]
    pub dynamic_asks: Option<bool>,
    /// Whether a task may also call actions it found at run time. A WORKFLOW only.
    #[serde(default)]
    pub dynamic_calls: Option<bool>,
    /// Whether a task may show a blok made at inform time, or any of the organization.
    #[serde(default)]
    pub dynamic_informs: Option<bool>,
}

impl ImplementationInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        self.definition.validate()?;
        if self.dynamic_calls == Some(true) && self.execution != Execution::WORKFLOW {
            return Err(ValidationError(format!(
                "Implementation {:?} declares dynamic_calls, which only a WORKFLOW may",
                self.interface
            )));
        }
        let asks = self.asks.as_deref().unwrap_or_default();
        let informs = self.informs.as_deref().unwrap_or_default();
        check_unique_keys(
            asks.iter()
                .map(|a| a.key.as_str())
                .chain(informs.iter().map(|i| i.key.as_str())),
            "Implementation asks and informs",
        )?;
        for ask in asks {
            // Checked as a task's blok, as it is made at registration; the clone keeps the
            // ask as sent, which is what its template is addressed by.
            ask.to_blok().validate_as(TASK_BLOK_KIND)?;
        }
        for inform in informs {
            inform.as_ask().to_blok().validate_as(TASK_BLOK_KIND)?;
        }
        for optimistic in self.optimistics.iter_mut().flatten() {
            optimistic.validate()?;
        }
        self.check_widget_targets()?;
        // The action's bloks name devices by the dependency keys this implementation declares.
        let arg_keys: Vec<String> = self.definition.args.iter().map(|a| a.key.clone()).collect();
        for blok in &self.definition.bloks {
            check_action_blok(blok, &self.dependencies, Some(arg_keys.clone()))?;
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

/// What follows an agent in the path of one of its assets (`self.assets.scope`).
pub const BLOK_ASSETS_SEGMENT: &str = "assets";

/// The `(dependency key, asset name)` of `self.assets.<name>` or `deps.<key>.assets.<name>`,
/// or `None` if `path` is not exactly that (`blok_asset_path`).
pub fn blok_asset_path(path: &str) -> Option<(&str, &str)> {
    let parts: Vec<&str> = path.split('.').collect();
    match parts.as_slice() {
        ["self", BLOK_ASSETS_SEGMENT, name] if !name.is_empty() => Some(("self", name)),
        ["deps", key, BLOK_ASSETS_SEGMENT, name] if !key.is_empty() && !name.is_empty() => {
            Some((key, name))
        }
        _ => None,
    }
}

/// `sha256:` and 64 lowercase hex characters (`MODEL_REF_PATTERN`).
pub fn is_model_ref(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
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

/// The kind of a blok an agent declares (`blok_kind`): an action logo when drawn as one.
pub fn blok_kind(context: ComponentContext) -> &'static str {
    if context == ComponentContext::ACTION_LOGO {
        "action_logo"
    } else {
        "agent"
    }
}

/// Sent one way: the tree, or the source, which is parsed (v2 BSX) into it.
fn parse_sent(
    components: &mut Option<Vec<ComponentNodeInputModel>>,
    source: &mut Option<String>,
) -> Result<(), ValidationError> {
    check_blok_sent_once(components.is_some(), source.is_some(), true)?;
    match (components.is_some(), source.take()) {
        (false, Some(source)) => {
            let tree = crate::bsx::parse_bsx_default(&source)
                .map_err(|e| ValidationError(e.to_string()))?;
            *components = Some(vec![tree]);
            Ok(())
        }
        _ => Ok(()),
    }
}

fn check_vocab(vocab: u32) -> Result<(), ValidationError> {
    if vocab != 2 {
        return Err(ValidationError(format!(
            "Blok manifest: vocab {vocab} is not a blok language this server speaks (2)"
        )));
    }
    Ok(())
}

/// A v2 tree against what its blok declares and its kind, without a catalog
/// (`check_blok_manifest`): refused on its first error, its warnings returned. A tree is one root.
pub fn check_blok_manifest(
    components: Option<&[ComponentNodeInputModel]>,
    scope: &BlokScope,
) -> Result<Vec<Finding>, ValidationError> {
    let Some(components) = components else {
        return Ok(vec![]);
    };
    let [tree] = components else {
        return Err(ValidationError(format!(
            "Blok manifest: a blok has one root, not {}",
            components.len()
        )));
    };
    // A tree sent as such was never parsed: its ids are held to what the parser holds them to.
    let mut seen = HashSet::new();
    for node in iter_component_nodes(Some(components)) {
        if !seen.insert(node.id.as_str()) {
            let finding = Finding::of(
                "bsx.duplicate_id",
                None,
                &[("id", Value::String(node.id.clone()))],
            );
            return Err(ValidationError(finding.to_string()));
        }
    }
    validate_blok(tree, scope, None).map_err(|finding| ValidationError(finding.to_string()))
}

/// A blok's args are the items it is opened for: an html blok's, keyed once (`check_blok_args`).
pub fn check_blok_args(
    context: ComponentContext,
    args: &[ArgPortInputModel],
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
    check_unique_keys(args.iter().map(|a| a.key.as_str()), "Blok manifest args")
}

/// A blok an agent declares (`BlokImplementationInputModel`, blok language v2).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BlokImplementationInputModel {
    pub key: String,
    #[serde(default)]
    pub dependencies: Vec<AgentDependencyInputModel>,
    /// The tree (one root), unless the blok is sent as its `source`; `validate` parses that into this.
    #[serde(default)]
    pub components: Option<Vec<ComponentNodeInputModel>>,
    /// The blok as v2 BSX. Exactly one of the two is sent.
    #[serde(default)]
    pub source: Option<String>,
    /// The module (an app's own unversioned extras) it renders against besides its catalogs;
    /// the Orkestrator's when absent.
    #[serde(default)]
    pub module: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    /// The renderer for the blok's root: HTML, SCENE, LOGO or ACTION_LOGO.
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
    /// The items this blok is opened for, read under `args.<key>`. HTML bloks only.
    #[serde(default)]
    pub args: Vec<ArgPortInputModel>,
    /// What a person answers through this blok when a task asks with it (`task.answer`).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub returns: Vec<ReturnPortInputModel>,
    /// The blok language version; 2 is the only one.
    pub vocab: u32,
    /// The catalogs it needs, as name@version (`ui@1`).
    #[serde(default)]
    pub catalogs: Vec<String>,
    #[serde(default)]
    pub locals: Vec<BlokLocalInputModel>,
    #[serde(default)]
    pub demo: Option<BlokDemoInputModel>,
    #[serde(default)]
    pub queries: Vec<BlokQueryInputModel>,
    #[serde(default)]
    pub subscriptions: Vec<BlokSubscriptionInputModel>,
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

fn reserved(name: &str) -> bool {
    crate::blok::vocabulary().is_reserved(name)
}

impl BlokImplementationInputModel {
    /// What the tree is checked against, as `kind` (`agent`, `action_logo`, or `task` for a
    /// task's blok).
    pub fn scope(&self, kind: &str) -> BlokScope {
        BlokScope {
            kind: kind.to_owned(),
            context: self.context.value().to_owned(),
            dependencies: self.dependencies.clone(),
            locals: self.locals.clone(),
            queries: self.queries.clone(),
            subscriptions: self.subscriptions.clone(),
            args: (!self.args.is_empty())
                .then(|| self.args.iter().map(|a| a.key.clone()).collect()),
            catalogs: self.catalogs.clone(),
            demo: self.demo.clone(),
            assets: None,
        }
    }

    /// Validated as the blok of an agent (or an action logo).
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        self.validate_as(blok_kind(self.context))
    }

    /// Sent one way (its source parsed into its tree); what it declares well-formed; its tree
    /// coherent with that and with its kind's roots (the language rules; the catalog rules are
    /// the catalog check's).
    pub fn validate_as(&mut self, kind: &str) -> Result<(), ValidationError> {
        parse_sent(&mut self.components, &mut self.source)?;
        check_vocab(self.vocab)?;
        for component in self.components.iter_mut().flatten() {
            component.validate()?;
        }
        for port in &mut self.args {
            port.validate()?;
        }
        for port in &mut self.returns {
            port.validate()?;
        }
        for local in &mut self.locals {
            local.validate()?;
        }
        if self.context == ComponentContext::OVERLAY {
            return Err(wrong_context("agent", self.context));
        }
        let keys: Vec<&str> = self.dependencies.iter().map(|d| d.key.as_str()).collect();
        check_blok_declarations(
            BlokDeclarations {
                catalogs: &self.catalogs,
                locals: &self.locals,
                queries: &self.queries,
                subscriptions: &self.subscriptions,
                dependency_keys: &keys,
                demo: self.demo.as_ref(),
            },
            kind,
            &reserved,
        )?;
        check_blok_target(
            self.context,
            self.for_model.as_deref(),
            self.for_node.as_deref(),
        )?;
        check_blok_args(self.context, &self.args)?;
        check_blok_manifest(self.components.as_deref(), &self.scope(kind))?;
        Ok(())
    }
}

/// The kind of a task's blok in the vocabulary's root matrix: it reads `self`, `deps`, `args`
/// and `task`, and answers with `task.answer`.
pub const TASK_BLOK_KIND: &str = "task";

/// The dependency every task blok has: the agent whose task shows it.
pub const SELF_DEPENDENCY: &str = "self";

/// A blok an action asks a person through, declared with it (`AskBlokInputModel`): made once
/// at registration as a task blok, its args and returns ports.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AskBlokInputModel {
    /// What a task names it by: `<parameter>.<method>` of the action's interactions protocol.
    pub key: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub components: Option<Vec<ComponentNodeInputModel>>,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub module: Option<String>,
    #[serde(default)]
    pub args: Vec<ArgPortInputModel>,
    #[serde(default)]
    pub returns: Vec<ReturnPortInputModel>,
    /// The agents it reads or calls besides `self`, as `deps.<key>`.
    #[serde(default)]
    pub dependencies: Vec<AgentDependencyInputModel>,
    pub vocab: u32,
    #[serde(default)]
    pub catalogs: Vec<String>,
    #[serde(default)]
    pub locals: Vec<BlokLocalInputModel>,
    #[serde(default)]
    pub demo: Option<BlokDemoInputModel>,
    #[serde(default)]
    pub queries: Vec<BlokQueryInputModel>,
    #[serde(default)]
    pub subscriptions: Vec<BlokSubscriptionInputModel>,
}

impl AskBlokInputModel {
    /// The ask as the html task blok it is made as: its own dependencies and `self`.
    pub fn to_blok(&self) -> BlokImplementationInputModel {
        let mut dependencies = self.dependencies.clone();
        if !dependencies.iter().any(|d| d.key == SELF_DEPENDENCY) {
            dependencies.push(
                serde_json::from_value(serde_json::json!({
                    "key": SELF_DEPENDENCY,
                    "description": "The agent whose action asks.",
                }))
                .expect("the self dependency deserializes"),
            );
        }
        BlokImplementationInputModel {
            key: self.key.clone(),
            dependencies,
            components: self.components.clone(),
            source: self.source.clone(),
            module: self.module.clone(),
            description: self.description.clone(),
            context: ComponentContext::HTML,
            default: false,
            for_model: None,
            for_node: None,
            args: self.args.clone(),
            returns: self.returns.clone(),
            vocab: self.vocab,
            catalogs: self.catalogs.clone(),
            locals: self.locals.clone(),
            demo: self.demo.clone(),
            queries: self.queries.clone(),
            subscriptions: self.subscriptions.clone(),
        }
    }
}

/// A blok an action shows a person while its task goes on, declared with it
/// (`InformBlokInputModel`): made once at registration as a task blok, its args ports. Nothing
/// waits on it, so it has no returns.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InformBlokInputModel {
    /// What a task names it by: `<parameter>.<method>` of the action's interactions protocol.
    pub key: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub components: Option<Vec<ComponentNodeInputModel>>,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub module: Option<String>,
    #[serde(default)]
    pub args: Vec<ArgPortInputModel>,
    /// The agents it reads or calls besides `self`, as `deps.<key>`.
    #[serde(default)]
    pub dependencies: Vec<AgentDependencyInputModel>,
    pub vocab: u32,
    #[serde(default)]
    pub catalogs: Vec<String>,
    #[serde(default)]
    pub locals: Vec<BlokLocalInputModel>,
    #[serde(default)]
    pub demo: Option<BlokDemoInputModel>,
    #[serde(default)]
    pub queries: Vec<BlokQueryInputModel>,
    #[serde(default)]
    pub subscriptions: Vec<BlokSubscriptionInputModel>,
}

impl InformBlokInputModel {
    /// The inform as an ask that answers nothing: what it is made and stored as.
    pub fn as_ask(&self) -> AskBlokInputModel {
        AskBlokInputModel {
            key: self.key.clone(),
            description: self.description.clone(),
            components: self.components.clone(),
            source: self.source.clone(),
            module: self.module.clone(),
            args: self.args.clone(),
            returns: vec![],
            dependencies: self.dependencies.clone(),
            vocab: self.vocab,
            catalogs: self.catalogs.clone(),
            locals: self.locals.clone(),
            demo: self.demo.clone(),
            queries: self.queries.clone(),
            subscriptions: self.subscriptions.clone(),
        }
    }
}

/// The component anchoring its children at the agent bound to a dependency (`dep`).
pub const ANCHOR_COMPONENT: &str = "Anchor";
/// The component drawing a line through the agents bound to dependencies (`through`).
pub const ROUTE_COMPONENT: &str = "Route";
/// The run's open ask form.
pub const ASK_COMPONENT: &str = "ask";

/// An action's own blok for one context (`ActionBlokInputModel`, blok language v2): no name,
/// never listed on its own, its dependencies the action's, named by key.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ActionBlokInputModel {
    /// HTML (a task panel), SCENE (a 3D view of its own) or OVERLAY (drawn into the hosting space).
    #[serde(default = "html_")]
    pub context: ComponentContext,
    /// The tree, unless the blok is sent as its `source`; `validate` parses that into this.
    #[serde(default)]
    pub components: Option<Vec<ComponentNodeInputModel>>,
    /// The blok as v2 BSX. Exactly one of the two is sent.
    #[serde(default)]
    pub source: Option<String>,
    /// The module it renders against besides its catalogs; the Orkestrator's when absent.
    #[serde(default)]
    pub module: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    pub vocab: u32,
    /// The catalogs it needs, as name@version.
    #[serde(default)]
    pub catalogs: Vec<String>,
    #[serde(default)]
    pub locals: Vec<BlokLocalInputModel>,
    #[serde(default)]
    pub demo: Option<BlokDemoInputModel>,
    #[serde(default)]
    pub queries: Vec<BlokQueryInputModel>,
    #[serde(default)]
    pub subscriptions: Vec<BlokSubscriptionInputModel>,
}

impl ActionBlokInputModel {
    /// Sent one way (its source parsed into its tree), in a context an action has a blok for.
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        parse_sent(&mut self.components, &mut self.source)?;
        check_vocab(self.vocab)?;
        for component in self.components.iter_mut().flatten() {
            component.validate()?;
        }
        for local in &mut self.locals {
            local.validate()?;
        }
        if !matches!(
            self.context,
            ComponentContext::HTML | ComponentContext::SCENE | ComponentContext::OVERLAY
        ) {
            return Err(wrong_context("action", self.context));
        }
        Ok(())
    }

    /// What its tree is checked against: kind `action`, the action's dependencies and args.
    pub fn scope(
        &self,
        dependencies: &[AgentDependencyInputModel],
        arg_keys: Option<Vec<String>>,
    ) -> BlokScope {
        BlokScope {
            kind: "action".into(),
            context: self.context.value().to_owned(),
            dependencies: dependencies.to_vec(),
            locals: self.locals.clone(),
            queries: self.queries.clone(),
            subscriptions: self.subscriptions.clone(),
            args: arg_keys,
            catalogs: self.catalogs.clone(),
            demo: self.demo.clone(),
            assets: None,
        }
    }
}

/// Every dependency key an `<Anchor dep>` or `<Route through>` names, with the node's id
/// (`anchored_keys`). Bound dynamically, they are left to the renderer.
pub fn anchored_keys(components: Option<&[ComponentNodeInputModel]>) -> Vec<(String, String)> {
    let mut named = vec![];
    for node in iter_component_nodes(components) {
        for prop in node.props.iter().flatten() {
            match (
                node.component.as_str(),
                prop.key.as_str(),
                &prop.static_value,
            ) {
                (ANCHOR_COMPONENT, "dep", Some(Value::String(key))) => {
                    named.push((key.clone(), node.id.clone()))
                }
                (ROUTE_COMPONENT, "through", Some(Value::Array(keys))) => named.extend(
                    keys.iter()
                        .filter_map(Value::as_str)
                        .map(|key| (key.to_owned(), node.id.clone())),
                ),
                _ => {}
            }
        }
    }
    named
}

/// Every `<Anchor dep>` and `<Route through>` names dependencies of the action
/// (`check_action_blok_anchors`).
pub fn check_action_blok_anchors(
    components: Option<&[ComponentNodeInputModel]>,
    dependency_keys: &HashSet<String>,
) -> Result<(), ValidationError> {
    for (key, node_id) in anchored_keys(components) {
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
                repr_str(&node_id),
                repr_str(&key)
            )));
        }
    }
    Ok(())
}

/// An action's blok against the dependencies its implementation declares and the action's args
/// (`check_action_blok`): it reads the run as `task`, its args as `args`, the agent running it
/// as `self` and the agents the run got as `deps.<key>`; `Anchor`/`Route` name only the
/// dependencies. Its warnings are returned.
pub fn check_action_blok(
    blok: &ActionBlokInputModel,
    dependencies: &[AgentDependencyInputModel],
    arg_keys: Option<Vec<String>>,
) -> Result<Vec<Finding>, ValidationError> {
    let keys: Vec<&str> = dependencies.iter().map(|d| d.key.as_str()).collect();
    check_blok_declarations(
        BlokDeclarations {
            catalogs: &blok.catalogs,
            locals: &blok.locals,
            queries: &blok.queries,
            subscriptions: &blok.subscriptions,
            dependency_keys: &keys,
            demo: blok.demo.as_ref(),
        },
        "action",
        &reserved,
    )?;
    if blok.components.is_none() {
        return Ok(vec![]);
    }
    let key_set: HashSet<String> = keys.iter().map(|k| (*k).to_owned()).collect();
    check_action_blok_anchors(blok.components.as_deref(), &key_set)?;
    check_blok_manifest(
        blok.components.as_deref(),
        &blok.scope(dependencies, arg_keys),
    )
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
    /// For a CALLBACK prop: the fields of the event it hands its callback (`event.<key>`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event: Option<Vec<CatalogEventFieldInputModel>>,
}

/// A field of the event a CALLBACK prop hands its callback (`CatalogEventFieldInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CatalogEventFieldInputModel {
    pub key: String,
    pub kind: CatalogValueKind,
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
    /// The prop text children fill (`<Button>Go</Button>` sets `label`); none takes no text.
    #[serde(default)]
    pub text_prop: Option<String>,
    /// The catalog it belongs to, as name@version (`ui@1`), or the name of the module it is from.
    #[serde(default)]
    pub catalog: Option<String>,
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
                    if (dependency == "self"
                        || blok.dependencies.iter().any(|d| d.key == dependency))
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
