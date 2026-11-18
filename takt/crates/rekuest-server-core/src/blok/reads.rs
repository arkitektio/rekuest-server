//! What a blok's tree reads of the agent that declares it, and the `self` dependency that says
//! so (the Rust twin of arkitekt-spec's `declare/blok/registry.py`: `self_reads` and
//! `self_dependency`, with `declare/definition/dependencies.py` and `match.py`).
//!
//! An SDK that parses BSX builds this dependency before it sends a blok. One that sends a blok
//! as its source alone leaves it to the server, which has the whole declaration at hand: the
//! states and the actions the tree names are the ones registered with it.
//!
//! The walk's scoping is the checker's (`blok::check`): a path whose root is a name in scope (an
//! `into` name, a `<local>`, a `foreach`'s `let` or `index`) reads that name, not the agent.

use std::collections::{BTreeSet, HashSet};

use super::check::{action_key_for, foreach_index, foreach_let, into_sites, FOREACH_ITEMS_PROP};
use super::declared::{local_declared, local_like};
use super::expr::s;
use super::vocabulary::{vocabulary, Finding};
use crate::inputs::{
    ActionArgumentInputModel, ActionDemandInputModel, ActionDependencyInputModel,
    AgentDependencyInputModel, AgentProbeInputModel, ArgPortInputModel, ComponentNodeInputModel,
    ComponentPropInputModel, ImplementationInputModel, PortMatchInputModel, ReturnPortInputModel,
    StateDemandInputModel, StateDependencyInputModel, StateImplementationInputModel,
    UtilCallInputModel, SELF_DEPENDENCY,
};

/// What a tree (and the variables of its queries) reads of `self`: states, actions, assets.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SelfReads {
    pub states: BTreeSet<String>,
    pub actions: BTreeSet<String>,
    pub assets: BTreeSet<String>,
    /// Whether it reads anything of `self` at all, the agent's own fields (`self.connected`)
    /// included.
    pub agent: bool,
}

impl SelfReads {
    /// Whether the tree reads nothing of its agent.
    pub fn is_empty(&self) -> bool {
        self.states.is_empty() && self.actions.is_empty() && self.assets.is_empty()
    }

    fn read(&mut self, path: &str) {
        let vocabulary = vocabulary();
        let mut parts: Vec<&str> = path.split('.').collect();
        if parts.len() > 1 && parts[0] == "deps" && parts[1] == SELF_DEPENDENCY {
            parts.remove(0);
        }
        if parts[0] != SELF_DEPENDENCY {
            return;
        }
        self.agent = true;
        if parts.len() < 2 {
            return;
        }
        let head = parts[1];
        if head == vocabulary.tasks_segment {
            if let Some(action) = parts.get(2) {
                self.actions.insert((*action).to_owned());
            }
        } else if head == vocabulary.assets_segment {
            if let Some(asset) = parts.get(2) {
                self.assets.insert((*asset).to_owned());
            }
        } else if !vocabulary.agent_fields.iter().any(|field| field == head) {
            self.states.insert(head.to_owned());
        }
    }

    fn visit_path(&mut self, path: &str, scope: &HashSet<String>) {
        let root = path.split('.').next().unwrap_or_default();
        if !scope.contains(root) {
            self.read(path);
        }
    }

    fn visit_agent_call(&mut self, call: &AgentProbeInputModel) {
        if call.dependency == SELF_DEPENDENCY {
            self.actions.insert(action_key_for(call).to_owned());
        }
    }

    /// A local that borrows an action's argument (`like="self.<action>.<arg>"`) demands that action.
    fn bind_local(&mut self, node: &ComponentNodeInputModel) {
        let parts: Vec<&str> = local_like(node).unwrap_or_default().split('.').collect();
        if let [SELF_DEPENDENCY, action, _] = parts.as_slice() {
            self.actions.insert((*action).to_owned());
        }
    }

    // -- the walk (`walk_tree`)

    fn walk_node(&mut self, node: &ComponentNodeInputModel, scope: &HashSet<String>) {
        let mut inner = scope.clone();
        let let_ = foreach_let(node);
        if let Some(let_) = let_ {
            inner.insert(let_.to_owned());
        }
        if let Some(index) = foreach_index(node) {
            inner.insert(index.to_owned());
        }
        for prop in node.props.iter().flatten() {
            // A foreach reads its items outside the loop, its key inside it.
            let prop_scope = if let_.is_some() && prop.key == FOREACH_ITEMS_PROP {
                scope
            } else {
                &inner
            };
            self.walk_prop(prop, prop_scope);
        }
        let children = node.children.as_deref().unwrap_or_default();
        for child in children {
            if let Some(name) = local_declared(child) {
                self.bind_local(child);
                inner.insert(name.to_owned());
            }
        }
        for child in children {
            self.walk_node(child, &inner);
        }
    }

    fn walk_prop(&mut self, prop: &ComponentPropInputModel, scope: &HashSet<String>) {
        if let Some(path) = prop.dynamic_value.as_ref().and_then(|d| d.path.as_deref()) {
            self.visit_path(path, scope);
        }
        if let Some(call) = &prop.agent_call {
            self.visit_agent_call(call);
            for argument in call.arguments.iter().flatten() {
                self.walk_argument(argument, scope);
            }
        }
        if let Some(call) = &prop.util_call {
            self.walk_util(call, scope);
        }
    }

    fn walk_util(&mut self, call: &UtilCallInputModel, scope: &HashSet<String>) {
        for argument in call.arguments.iter().flatten() {
            self.walk_argument(argument, scope);
        }
    }

    fn walk_argument(&mut self, argument: &ActionArgumentInputModel, scope: &HashSet<String>) {
        if let Some(path) = argument.value_path.as_deref() {
            self.visit_path(path, scope);
        }
        if let Some(call) = &argument.agent_call {
            self.visit_agent_call(call);
            for nested in call.arguments.iter().flatten() {
                self.walk_argument(nested, scope);
            }
        }
        if let Some(call) = &argument.util_call {
            self.walk_util(call, scope);
        }
        for nested in argument
            .value_list
            .iter()
            .flatten()
            .chain(argument.value_dict.iter().flatten())
        {
            self.walk_argument(nested, scope);
        }
    }
}

/// What `tree` reads of `self`, the variables of its queries (`<var>` nodes) included.
pub fn self_reads(tree: &ComponentNodeInputModel) -> SelfReads {
    let mut reads = SelfReads::default();
    let into: HashSet<String> = into_sites(tree).into_iter().map(|(name, _)| name).collect();
    reads.walk_node(tree, &into);
    reads
}

/// A port as a demand matches it (`build_port_match`): where it is, its key and its type.
trait Matched: Sized {
    fn key(&self) -> &str;
    fn kind(&self) -> crate::enums::PortKind;
    fn identifier(&self) -> Option<&str>;
    fn nullable(&self) -> bool;
    fn children(&self) -> &[Self];
}

macro_rules! matched {
    ($port:ty) => {
        impl Matched for $port {
            fn key(&self) -> &str {
                &self.key
            }
            fn kind(&self) -> crate::enums::PortKind {
                self.kind
            }
            fn identifier(&self) -> Option<&str> {
                self.identifier.as_deref()
            }
            fn nullable(&self) -> bool {
                self.nullable
            }
            fn children(&self) -> &[Self] {
                self.children.as_deref().unwrap_or_default()
            }
        }
    };
}
matched!(ArgPortInputModel);
matched!(ReturnPortInputModel);

/// The matches of `ports`, each at its position; none for no ports (`build_port_matches`).
fn port_matches<P: Matched>(ports: &[P]) -> Option<Vec<PortMatchInputModel>> {
    let matches: Vec<PortMatchInputModel> = ports
        .iter()
        .enumerate()
        .map(|(at, port)| PortMatchInputModel {
            at: Some(at as i64),
            key: Some(port.key().to_owned()),
            kind: Some(port.kind()),
            identifier: port.identifier().map(str::to_owned),
            nullable: Some(port.nullable()),
            dimension: None,
            descriptors: None,
            children: port_matches(port.children()),
        })
        .collect();
    (!matches.is_empty()).then_some(matches)
}

/// One port as a match with no position: what `port.value` of its own blok is typed as.
pub fn port_match(port: &ArgPortInputModel) -> PortMatchInputModel {
    PortMatchInputModel {
        at: None,
        key: Some(port.key.clone()),
        kind: Some(port.kind),
        identifier: port.identifier.clone(),
        nullable: Some(port.nullable),
        dimension: None,
        descriptors: None,
        children: port_matches(Matched::children(port)),
    }
}

/// An action of the agent itself, demanded by its key and its ports
/// (`build_action_dependency_input`).
fn action_dependency(implementation: &ImplementationInputModel) -> ActionDependencyInputModel {
    let definition = &implementation.definition;
    ActionDependencyInputModel {
        key: implementation.interface.clone(),
        description: definition.description.clone(),
        demand: Some(ActionDemandInputModel {
            key: Some(implementation.interface.clone()),
            arg_matches: port_matches(&definition.args),
            return_matches: port_matches(&definition.returns),
            ..ActionDemandInputModel::default()
        }),
        optional: false,
        allow_inactive: true,
    }
}

/// A state of the agent itself, demanded by its key and its fields. A state is a record: its
/// fields are matched by key, so only they lose their position (`build_state_dependency_input`).
fn state_dependency(state: &StateImplementationInputModel) -> StateDependencyInputModel {
    let matches = port_matches(&state.definition.ports).map(|matches| {
        matches
            .into_iter()
            .map(|field| PortMatchInputModel { at: None, ..field })
            .collect()
    });
    StateDependencyInputModel {
        key: state.interface.clone(),
        description: None,
        demand: Some(StateDemandInputModel {
            key: Some(state.interface.clone()),
            matches,
            ..StateDemandInputModel::default()
        }),
        optional: false,
        allow_inactive: true,
    }
}

fn listed<'a>(names: impl Iterator<Item = &'a str>) -> String {
    let names: BTreeSet<&str> = names.collect();
    if names.is_empty() {
        "none".to_owned()
    } else {
        names.into_iter().collect::<Vec<_>>().join(", ")
    }
}

/// The `self` dependency the reads of a blok demand of the agent declaring `implementations`
/// and `states`, or none when it reads none of it. An asset is the declaration's to judge
/// (`ImplementAgentInputModel::validate`); one read alone still makes the dependency.
///
/// The finding when it calls or watches an action, or reads a state, the agent does not declare.
pub fn self_dependency(
    reads: &SelfReads,
    implementations: &[ImplementationInputModel],
    states: &[StateImplementationInputModel],
) -> Result<Option<AgentDependencyInputModel>, Finding> {
    let vocabulary = vocabulary();
    if !reads.states.is_empty() {
        let clashing = states
            .iter()
            .map(|state| state.interface.as_str())
            .filter(|name| vocabulary.reserved_state_names.contains(*name))
            .min();
        if let Some(name) = clashing {
            return Err(Finding::of(
                "deps.reserved_state",
                None,
                &[("name", s(name))],
            ));
        }
    }
    let mut actions = vec![];
    for action in &reads.actions {
        let Some(implementation) = implementations.iter().find(|i| &i.interface == action) else {
            return Err(Finding::of(
                "deps.unknown_action",
                None,
                &[
                    ("path", s(format!("self.{action}"))),
                    ("key", s(SELF_DEPENDENCY)),
                    ("action", s(action.clone())),
                    (
                        "actions",
                        s(listed(implementations.iter().map(|i| i.interface.as_str()))),
                    ),
                ],
            ));
        };
        actions.push(action_dependency(implementation));
    }
    let mut demanded = vec![];
    for state in &reads.states {
        let Some(declared) = states.iter().find(|s| &s.interface == state) else {
            return Err(Finding::of(
                "deps.unknown_state",
                None,
                &[
                    ("path", s(format!("self.{state}"))),
                    ("key", s(SELF_DEPENDENCY)),
                    ("state", s(state.clone())),
                    (
                        "states",
                        s(listed(states.iter().map(|s| s.interface.as_str()))),
                    ),
                ],
            ));
        };
        demanded.push(state_dependency(declared));
    }
    if reads.is_empty() {
        return Ok(None);
    }
    Ok(Some(AgentDependencyInputModel {
        key: SELF_DEPENDENCY.to_owned(),
        app: None,
        version: None,
        name: None,
        description: None,
        optional: false,
        action_dependencies: (!actions.is_empty()).then_some(actions),
        state_dependencies: (!demanded.is_empty()).then_some(demanded),
        auto_resolvable: false,
        mutually_exclusive_keys: None,
        min_viable_instances: None,
        max_viable_instances: None,
        prefered_instances: None,
    }))
}
