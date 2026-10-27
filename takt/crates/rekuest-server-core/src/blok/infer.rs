//! Resolve what a blok leaves to its tree: the kinds of its locals (the Rust twin of
//! arkitekt-spec's `declare/blok/infer.py`). Dependencies are never inferred: every
//! `deps.<key>` a blok reads, whatever its kind, must be declared (`deps.unknown`).
//!
//! **A local's kind**, when it is left out (a given kind, ANY included, is kept), is the first of:
//! 1. its `default`: a bool BOOL, an integer INT, a number FLOAT, a string STRING, a non-empty
//!    list of one scalar kind a LIST of it, a non-empty object of one scalar kind a DICT of it
//!    (the item port keyed `...`);
//! 2. the port of the task argument it is passed to whole (`self.move(to: position)`), at the
//!    first such call in tree order whose action port is known and makes a valid local;
//! 3. the bind kind of the first control bound to it whole, when that is STRING, INT, FLOAT or BOOL;
//! 4. else ANY.
//!
//! Every field the local sets wins over the inferred type, which only fills what it leaves out;
//! a candidate that makes no valid local with them is skipped.

use std::collections::{BTreeSet, HashMap, HashSet};

use serde_json::{Map, Value};

use super::check::{
    action_key_for, foreach_let, into_sites, BlokScope, BIND_PROP, FOREACH_ITEMS_PROP,
};
use super::view::BlokView;
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

/// Where a tree uses its locals whole (task arguments, binds).
struct Uses<'a> {
    locals: HashSet<&'a str>,
    dependencies: HashMap<&'a str, &'a AgentDependencyInputModel>,
    view: Option<&'a BlokView>,
    arg_ports: &'a ArgPorts,
    arguments: HashMap<String, Vec<Typed>>,
    binds: HashMap<String, Vec<String>>,
}

type Names = HashSet<String>;

impl Uses<'_> {
    fn visit_component(&mut self, node: &ComponentNodeInputModel, scope: &Names) {
        let bind = self
            .view
            .and_then(|view| view.component(&node.component))
            .and_then(|spec| spec.props.iter().find(|p| p.key == BIND_PROP));
        let Some(bind) = bind else { return };
        for prop in node.props.iter().flatten() {
            if prop.key != BIND_PROP {
                continue;
            }
            let Some(path) = prop.dynamic_value.as_ref().and_then(|d| d.path.as_deref()) else {
                continue;
            };
            if self.locals.contains(path) && !scope.contains(path) {
                self.binds
                    .entry(path.to_owned())
                    .or_default()
                    .push(bind.kind.value().to_owned());
            }
        }
    }

    fn visit_agent_call(&mut self, call: &AgentProbeInputModel, scope: &Names) {
        let action = action_key_for(call);
        let ports: HashMap<&str, &ArgPortInputModel> = self
            .arg_ports
            .get(&call.dependency)
            .and_then(|actions| actions.get(action))
            .map(|ports| ports.iter().map(|p| (p.key.as_str(), p)).collect())
            .unwrap_or_default();
        let demand = self
            .dependencies
            .get(call.dependency.as_str())
            .and_then(|d| d.action_dependencies.as_ref())
            .and_then(|actions| actions.iter().find(|a| a.key == action))
            .and_then(|a| a.demand.as_ref());
        let matches: HashMap<&str, &PortMatchInputModel> = demand
            .and_then(|d| d.arg_matches.as_ref())
            .map(|matches| {
                matches
                    .iter()
                    .filter_map(|m| m.key.as_deref().map(|k| (k, m)))
                    .collect()
            })
            .unwrap_or_default();
        for argument in call.arguments.iter().flatten() {
            let (Some(path), Some(key)) = (argument.value_path.as_deref(), argument.key.as_deref())
            else {
                continue;
            };
            if !self.locals.contains(path) || scope.contains(path) {
                continue;
            }
            let typed = match (ports.get(key), matches.get(key)) {
                (Some(port), _) => Some(port_type(port)),
                (None, Some(port_match)) => match_type(port_match),
                (None, None) => None,
            };
            if let Some(typed) = typed {
                self.arguments
                    .entry(path.to_owned())
                    .or_default()
                    .push(typed);
            }
        }
    }

    // -- the walk (`walk_tree`)

    fn walk_node(&mut self, node: &ComponentNodeInputModel, scope: &Names) {
        self.visit_component(node, scope);
        let mut inner = scope.clone();
        let let_ = foreach_let(node);
        if let Some(let_) = let_ {
            inner.insert(let_.to_owned());
        }
        for prop in node.props.iter().flatten() {
            let prop_scope = if let_.is_some() && prop.key == FOREACH_ITEMS_PROP {
                scope
            } else {
                &inner
            };
            self.walk_prop(prop, prop_scope);
        }
        for child in node.children.iter().flatten() {
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
) -> BlokLocalInputModel {
    if local.kind_given() {
        return local.clone();
    }
    let mut candidates: Vec<Typed> = vec![];
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

/// `scope` with what `tree` tells: each local's kind. Resolving a resolved scope changes nothing.
pub fn resolve_scope(
    tree: &ComponentNodeInputModel,
    scope: &BlokScope,
    view: Option<&BlokView>,
    arg_ports: &ArgPorts,
) -> BlokScope {
    let mut uses = Uses {
        locals: scope.locals.iter().map(BlokLocalInputModel::key).collect(),
        dependencies: scope
            .dependencies
            .iter()
            .map(|d| (d.key.as_str(), d))
            .collect(),
        view,
        arg_ports,
        arguments: HashMap::new(),
        binds: HashMap::new(),
    };
    let into: Names = into_sites(tree).into_iter().map(|(name, _)| name).collect();
    uses.walk_node(tree, &into);
    let no_arguments: Vec<Typed> = vec![];
    let no_binds: Vec<String> = vec![];
    let locals = scope
        .locals
        .iter()
        .map(|local| {
            resolve_local(
                local,
                uses.arguments.get(local.key()).unwrap_or(&no_arguments),
                uses.binds.get(local.key()).unwrap_or(&no_binds),
            )
        })
        .collect();
    BlokScope {
        locals,
        ..scope.clone()
    }
}
