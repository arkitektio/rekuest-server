//! Resolve what a blok leaves to its tree: the kinds of its `<local>` declarations (the Rust
//! twin of arkitekt-spec's `declare/blok/infer.py`). Dependencies are never inferred: every
//! `deps.<key>` a mounted blok reads, whatever its kind, must be declared (`deps.unknown`).
//!
//! **A local's kind**, when it is left out (a given kind, ANY included, is kept), is the first of:
//! 0. what `like="self.<action>.<arg>"` / `like="deps.<key>.<action>.<arg>"` borrows, resolved
//!    only where the blok is mounted;
//! 1. its `default`: a bool BOOL, an integer INT, a number FLOAT, a string STRING, a non-empty
//!    list of one scalar kind a LIST of it, a non-empty object of one scalar kind a DICT of it
//!    (the item port keyed `...`);
//! 2. the port of the task argument it is passed to whole (`self.move(to: position)`), at the
//!    first such call in tree order whose action port is known and makes a valid local;
//! 3. the bind kind of the first control bound to it whole, when that is STRING, INT, FLOAT or BOOL;
//! 4. else ANY.
//!
//! Only uses in the local's scope (its parent's subtree) count. Every field the local sets wins
//! over the inferred type, which only fills what it leaves out; a candidate that makes no valid
//! local with them is skipped.

use std::collections::{BTreeSet, HashMap};

use serde_json::{Map, Value};

use super::check::{
    action_key_for, foreach_index, foreach_let, into_sites, iter_nodes, FOREACH_ITEMS_PROP,
};
use super::declared::{declared_local, local_declared, local_like, local_problem, LOCAL_COMPONENT};
use crate::inputs::{
    ActionArgumentInputModel, AgentDependencyInputModel, AgentProbeInputModel, ArgPortInputModel,
    BlokLocalInputModel, ComponentNodeInputModel, ComponentPropInputModel, PortMatchInputModel,
    UtilCallInputModel,
};

/// Per dependency key (`self`), per action, its arg ports: what is known of the actions beyond
/// a demand's arg matches (an agent's own registered actions).
pub type ArgPorts = HashMap<String, HashMap<String, Vec<ArgPortInputModel>>>;

const ITEM_KEY: &str = "...";
/// The bind kinds a local takes from its control; a LIST, DICT or ANY control tells too little.
const BIND_INFERRED: [&str; 4] = ["STRING", "INT", "FLOAT", "BOOL"];
/// What a local takes of an action's arg port when it is inferred from one: its type, nothing else.
const TYPE_FIELDS: [&str; 8] = [
    "kind",
    "identifier",
    "nullable",
    "choices",
    "reference_unit",
    "proposed_units",
    "dimension",
    "children",
];

type Typed = Map<String, Value>;

fn scalar_kind(value: &Value) -> Option<&'static str> {
    match value {
        Value::Bool(_) => Some("BOOL"),
        Value::Number(number) if number.is_i64() || number.is_u64() => Some("INT"),
        Value::Number(_) => Some("FLOAT"),
        Value::String(_) => Some("STRING"),
        _ => None,
    }
}

fn typed_of(kind: &str) -> Typed {
    let mut typed = Map::new();
    typed.insert("kind".into(), Value::String(kind.to_owned()));
    typed
}

/// The type a default implies (rule 1), or none when it says nothing (`null`, `[]`, `{}`,
/// mixed items).
pub fn default_type(default: Option<&Value>) -> Option<Typed> {
    let default = default?;
    if let Some(kind) = scalar_kind(default) {
        return Some(typed_of(kind));
    }
    let (items, container): (Vec<&Value>, &str) = match default {
        Value::Array(items) => (items.iter().collect(), "LIST"),
        Value::Object(map) => (map.values().collect(), "DICT"),
        _ => return None,
    };
    let kinds: BTreeSet<Option<&str>> = items.iter().map(|item| scalar_kind(item)).collect();
    let mut kinds = kinds.into_iter();
    let (Some(Some(item_kind)), None) = (kinds.next(), kinds.next()) else {
        return None;
    };
    let mut item = typed_of(item_kind);
    item.insert("key".into(), Value::String(ITEM_KEY.into()));
    let mut typed = typed_of(container);
    typed.insert("children".into(), Value::Array(vec![Value::Object(item)]));
    Some(typed)
}

/// The type an arg match says (a kind, recursively for its children), or none when it says too
/// little to be a port.
pub fn match_type(port: &PortMatchInputModel) -> Option<Typed> {
    let kind = port.kind?;
    let mut typed = typed_of(kind.value());
    typed.insert(
        "identifier".into(),
        port.identifier.clone().map_or(Value::Null, Value::String),
    );
    typed.insert(
        "nullable".into(),
        Value::Bool(port.nullable.unwrap_or(false)),
    );
    if let Some(children) = port.children.as_ref().filter(|c| !c.is_empty()) {
        let mut typed_children = vec![];
        for child in children {
            let mut child_type = match_type(child)?;
            let key = child.key.clone()?;
            child_type.insert("key".into(), Value::String(key));
            typed_children.push(Value::Object(child_type));
        }
        typed.insert("children".into(), Value::Array(typed_children));
    }
    Some(typed)
}

/// The type of an action's arg port: [`TYPE_FIELDS`], nothing else.
pub fn port_type(port: &ArgPortInputModel) -> Typed {
    let Ok(Value::Object(dumped)) = serde_json::to_value(port) else {
        return Map::new();
    };
    dumped
        .into_iter()
        .filter(|(key, value)| TYPE_FIELDS.contains(&key.as_str()) && !value.is_null())
        .collect()
}

/// What `like` leaves of the argument's port: validators and effects name the action's other
/// arguments.
const BORROW_EXCLUDED: [&str; 4] = ["key", "default", "validators", "effects"];

/// A `<local>` with its kind resolved, and why its `like` borrows nothing (none when it borrows,
/// has none, or the blok is not mounted).
#[derive(Debug, Clone, PartialEq)]
pub struct ResolvedLocal {
    pub local: BlokLocalInputModel,
    pub like_problem: Option<String>,
}

/// The type of an action's argument, as this side knows it: the full arg port of the actions it
/// knows (`arg_ports`), else the arg match of a dependency's demand; none when neither tells.
pub fn argument_type(
    dependency: &str,
    action: &str,
    argument: &str,
    dependencies: &HashMap<&str, &AgentDependencyInputModel>,
    arg_ports: &ArgPorts,
) -> Option<Typed> {
    let port = arg_ports
        .get(dependency)
        .and_then(|actions| actions.get(action))
        .and_then(|ports| ports.iter().find(|port| port.key == argument));
    if let Some(port) = port {
        return Some(port_type(port));
    }
    let demand = dependencies
        .get(dependency)?
        .action_dependencies
        .as_ref()?
        .iter()
        .find(|each| each.key == action)?
        .demand
        .as_ref()?;
    let port_match = demand
        .arg_matches
        .as_ref()?
        .iter()
        .find(|each| each.key.as_deref() == Some(argument))?;
    match_type(port_match)
}

/// What `like="self.<action>.<arg>"` / `"deps.<key>.<action>.<arg>"` borrows: the argument's
/// port (its default too, which the local's own replaces), or why it borrows nothing.
pub fn borrowed_type(
    like: &str,
    dependencies: &HashMap<&str, &AgentDependencyInputModel>,
    arg_ports: &ArgPorts,
) -> Result<Typed, String> {
    let parts: Vec<&str> = like.split('.').collect();
    let (dependency, action, argument) = match parts.as_slice() {
        ["self", action, argument] => ("self", *action, *argument),
        ["deps", dependency, action, argument] => (*dependency, *action, *argument),
        _ => {
            return Err(
                "is no argument: write self.<action>.<arg> or deps.<key>.<action>.<arg>".into(),
            )
        }
    };
    let port = arg_ports
        .get(dependency)
        .and_then(|actions| actions.get(action))
        .and_then(|ports| ports.iter().find(|port| port.key == argument));
    if let Some(port) = port {
        let Ok(Value::Object(dumped)) = serde_json::to_value(port) else {
            return Ok(Map::new());
        };
        let mut borrowed: Typed = dumped
            .into_iter()
            .filter(|(key, value)| !BORROW_EXCLUDED.contains(&key.as_str()) && !value.is_null())
            .collect();
        if let Some(default) = port.default.as_ref().filter(|d| !d.is_null()) {
            borrowed.insert("default".into(), default.clone());
        }
        return Ok(borrowed);
    }
    if !dependencies.contains_key(dependency) && dependency != "self" {
        let mut keys: Vec<&str> = dependencies.keys().copied().collect();
        keys.sort_unstable();
        let keys = if keys.is_empty() {
            "none".to_owned()
        } else {
            keys.join(", ")
        };
        return Err(format!(
            "names the dependency {}, which the mount does not have ({keys})",
            crate::pyjson::repr_str(dependency)
        ));
    }
    let known = arg_ports.get(dependency);
    let declared = dependencies
        .get(dependency)
        .and_then(|d| d.action_dependencies.as_ref());
    let has_action = known.is_some_and(|k| k.contains_key(action))
        || declared.is_some_and(|actions| actions.iter().any(|a| a.key == action));
    if !has_action {
        let mut actions: BTreeSet<&str> = known
            .map(|k| k.keys().map(String::as_str).collect())
            .unwrap_or_default();
        actions.extend(declared.into_iter().flatten().map(|a| a.key.as_str()));
        let actions = if actions.is_empty() {
            "none".to_owned()
        } else {
            actions.into_iter().collect::<Vec<_>>().join(", ")
        };
        return Err(format!(
            "names the action {}, which {dependency} does not have ({actions})",
            crate::pyjson::repr_str(action)
        ));
    }
    argument_type(dependency, action, argument, dependencies, arg_ports).ok_or_else(|| {
        format!(
            "names the argument {}, which {dependency}.{action} does not have, or does not type",
            crate::pyjson::repr_str(argument)
        )
    })
}

/// A name in scope: a local's node id, or none for a `let` or an `into` name (which shadow
/// nothing here: scoping is the checker's to judge).
type Names = HashMap<String, Option<String>>;

/// Where a tree uses its locals whole (task arguments, binds), per local node id.
struct Uses<'a> {
    dependencies: &'a HashMap<&'a str, &'a AgentDependencyInputModel>,
    arg_ports: &'a ArgPorts,
    arguments: HashMap<String, Vec<Typed>>,
    binds: HashMap<String, Vec<String>>,
}

impl Uses<'_> {
    /// What a control writes through its `bind` is its catalog's to say, and a server reads
    /// none: a local only a bind would type stays ANY here, and the renderer types it.
    fn visit_component(&mut self, _node: &ComponentNodeInputModel, _scope: &Names) {}

    fn visit_agent_call(&mut self, call: &AgentProbeInputModel, scope: &Names) {
        let action = action_key_for(call);
        for argument in call.arguments.iter().flatten() {
            let (Some(path), Some(key)) = (argument.value_path.as_deref(), argument.key.as_deref())
            else {
                continue;
            };
            let Some(Some(local)) = scope.get(path) else {
                continue;
            };
            let typed = argument_type(
                &call.dependency,
                action,
                key,
                self.dependencies,
                self.arg_ports,
            );
            if let Some(typed) = typed {
                self.arguments.entry(local.clone()).or_default().push(typed);
            }
        }
    }

    // -- the walk (`walk_tree`)

    fn walk_node(&mut self, node: &ComponentNodeInputModel, scope: &Names) {
        self.visit_component(node, scope);
        let mut inner = scope.clone();
        let let_ = foreach_let(node);
        if let Some(let_) = let_ {
            inner.insert(let_.to_owned(), None);
        }
        if let Some(index) = foreach_index(node) {
            inner.insert(index.to_owned(), None);
        }
        for prop in node.props.iter().flatten() {
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
                inner.insert(name.to_owned(), Some(child.id.clone()));
            }
        }
        for child in children {
            self.walk_node(child, &inner);
        }
    }

    fn walk_prop(&mut self, prop: &ComponentPropInputModel, scope: &Names) {
        if let Some(call) = &prop.agent_call {
            self.visit_agent_call(call, scope);
            for argument in call.arguments.iter().flatten() {
                self.walk_argument(argument, scope);
            }
        }
        if let Some(call) = &prop.util_call {
            self.walk_util(call, scope);
        }
    }

    fn walk_util(&mut self, call: &UtilCallInputModel, scope: &Names) {
        for argument in call.arguments.iter().flatten() {
            self.walk_argument(argument, scope);
        }
    }

    fn walk_argument(&mut self, argument: &ActionArgumentInputModel, scope: &Names) {
        if let Some(call) = &argument.agent_call {
            self.visit_agent_call(call, scope);
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

/// `local` with its kind resolved by the rules of this module (a given kind is kept).
pub fn resolve_local(
    local: &BlokLocalInputModel,
    arguments: &[Typed],
    binds: &[String],
    borrowed: Option<&Typed>,
) -> BlokLocalInputModel {
    if local.kind_given() {
        return local.clone();
    }
    let mut candidates: Vec<Typed> = vec![];
    candidates.extend(borrowed.cloned());
    candidates.extend(default_type(local.port.default.as_ref()));
    candidates.extend(arguments.iter().cloned());
    candidates.extend(
        binds
            .iter()
            .filter(|kind| BIND_INFERRED.contains(&kind.as_str()))
            .map(|kind| typed_of(kind)),
    );
    candidates
        .iter()
        .find_map(|candidate| local.typed(candidate))
        .unwrap_or_else(|| local.any())
}

/// Every readable `<local>` of `tree` with its kind resolved (see the module), by node id, in
/// tree order. `dependencies` none is a blok not mounted: a `like` then borrows nothing (unless
/// `arg_ports` says the action), and says nothing of it either.
pub fn resolve_locals(
    tree: &ComponentNodeInputModel,
    dependencies: Option<&[AgentDependencyInputModel]>,
    arg_ports: &ArgPorts,
) -> Vec<(String, ResolvedLocal)> {
    let by_key: HashMap<&str, &AgentDependencyInputModel> = dependencies
        .into_iter()
        .flatten()
        .map(|d| (d.key.as_str(), d))
        .collect();
    let mut uses = Uses {
        dependencies: &by_key,
        arg_ports,
        arguments: HashMap::new(),
        binds: HashMap::new(),
    };
    let into: Names = into_sites(tree)
        .into_iter()
        .map(|(name, _)| (name, None))
        .collect();
    uses.walk_node(tree, &into);
    let mut resolved = vec![];
    for node in iter_nodes(tree) {
        if node.component != LOCAL_COMPONENT || local_problem(node).is_some() {
            continue;
        }
        let Ok(local) = declared_local(node) else {
            continue;
        };
        let (borrowed, like_problem) = match local_like(node) {
            Some(like) if dependencies.is_some() || !arg_ports.is_empty() => {
                match borrowed_type(like, &by_key, arg_ports) {
                    Ok(borrowed) => (Some(borrowed), None),
                    Err(problem) => (None, Some(problem)),
                }
            }
            _ => (None, None),
        };
        let local = resolve_local(
            &local,
            uses.arguments.get(&node.id).map_or(&[][..], Vec::as_slice),
            uses.binds.get(&node.id).map_or(&[][..], Vec::as_slice),
            borrowed.as_ref(),
        );
        resolved.push((
            node.id.clone(),
            ResolvedLocal {
                local,
                like_problem,
            },
        ));
    }
    resolved
}
