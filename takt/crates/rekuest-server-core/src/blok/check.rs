//! Validate a v2 blok: its tree against what it declares, its kind, and the catalogs that draw
//! it (the Rust twin of arkitekt-spec's `declare/blok/validate.py` and `walk.py`, held to the
//! `checked` cases of `tests/fixtures/bsx_v2.json`).
//!
//! Every rule is a row of the vocabulary's diagnostic table: the `<Blok>` (its kind drawn in its
//! context, a scene's or a logo's one element, `chrome` a panel's or a scene's, the kind the
//! mount takes), the declarations (`<local>` first among its parent's children and shadowing
//! nothing, `<query>`/`<subscription>` children of the `<Blok>`, each named once), roots and the
//! per-kind matrix, agents' state, tasks and assets, the callback rule (one task call or one
//! effect, a query effect naming a declared query by a literal), locals as ports (`bind_kinds`,
//! `arg_kinds`), records, the theme. A finding is an error (the blok is refused) or a warning
//! (it is reported).
//!
//! Mounted or not: a [`BlokScope`] without dependencies is a blok checked on its own; what its
//! mount provides (`deps.<key>`, `Placed`/`Route` keys, a local's `like`) is then taken as
//! written. Every server path mounts.
//!
//! The walk's scoping:
//! - `into` names are in scope in the whole tree, each written by one call site;
//! - a `<local>` is in scope in its parent's subtree: every local of a node is bound before any
//!   of its children is walked;
//! - a `foreach`'s `let` (and its `index`) is in scope in its children and its own `key`, not its `items`;
//! - `event` only in a callback prop, `error` only inside the `<error>` slot of a `<suspense>`.

use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

use super::declared::{
    blok_header, blok_logo_tree, content, declaration_problem, declared_dependencies,
    declared_queries, declared_subscriptions, header_problem, iter_with_parents, local_declared,
    local_name, local_problem, BlokHeader, DeclaredDependencies, DEPENDENCIES_COMPONENT, KEY_PROP,
    LIKE_PROP, LOCAL_COMPONENT, LOGO_COMPONENT, NAME_PROP, QUERY_COMPONENT, SUBSCRIPTION_COMPONENT,
    VAR_COMPONENT,
};
use super::expr::{s, AGENTS_ROOT, PICK_VERB, REFERENCE_SEPARATOR};
use super::infer::{resolve_locals, ArgPorts, ResolvedLocal};
use super::vocabulary::{vocabulary, Finding, Kind, RecordField, Vocabulary};
use crate::bsx::PICK_FUNCTION;
use crate::enums::PortKind;
use crate::inputs::{
    is_field_path, is_name, operation_keyword, ActionArgumentInputModel, AgentDependencyInputModel,
    AgentProbeInputModel, ArgPortInputModel, BlokDemoInputModel, BlokLocalInputModel,
    BlokQueryInputModel, BlokSubscriptionInputModel, ComponentNodeInputModel,
    ComponentPropInputModel, PortMatchInputModel, StateDependencyInputModel, UtilCallInputModel,
};
use crate::pyjson::repr;

const ITEM: &str = "...";
/// Props whose value is the blok's dependency keys, written out (not paths): each must be a dependency.
const DEPENDENCY_KEY_PROPS: [(&str, &str); 2] = [("Placed", "dep"), ("Route", "through")];
/// The kinds that have a run's dependencies: an action's own bloks, for a run and for starting one.
const RUN_KINDS: [&str; 2] = ["action", "assign"];
const ASSIGN_KIND: &str = "assign";
/// The root of a port blok: the port it is the widget of.
const PORT_ROOT: &str = "port";
/// The one field of `port` a control writes.
const PORT_VALUE_FIELD: &str = "value";
/// The field of `assign` that holds one flag per argument the caller decided.
const GIVEN_FIELD: &str = "given";
/// The one field of an agent that is no string: whether it is connected right now.
const CONNECTED_FIELD: &str = "connected";
const ASK_COMPONENT: &str = "ask";
const CHROME_NONE: &str = "none";
/// The language's elements whose children do not always show: a host part inside one still claims its slot.
const CONDITIONAL_COMPONENTS: [&str; 9] = [
    "if", "else", "match", "case", "default", "foreach", "suspense", "fallback", "error",
];
/// Starts the run an assign blok prepares.
const START_EFFECT: &str = "assign.start";

const TOAST_COMPONENT: &str = "Toast";
const TOAST_TASK_PROP: &str = "task";
const CLASS_NAME: &str = "className";
const LOGO: &str = "LOGO";
const TEXT_NODE: &str = "#text";
pub const FOREACH_COMPONENT: &str = "foreach";
pub const FOREACH_LET_PROP: &str = "let";
pub const FOREACH_INDEX_PROP: &str = "index";
pub const FOREACH_ITEMS_PROP: &str = "items";
pub const SUSPENSE_COMPONENT: &str = "suspense";
pub const ERROR_SLOT: &str = "error";
pub const WHEN_PROP: &str = "when";
pub const BIND_PROP: &str = "bind";
/// The effect `queries.<name>.refetch()` is stored as.
pub const REFETCH_FUNCTION: &str = "query.refetch";
/// The effect `queries.<name>.fetch_more(...)` is stored as.
pub const FETCH_MORE_FUNCTION: &str = "query.fetch_more";
/// The BSX sugar `queries.<name>.<verb>(...)`: each verb and the effect it is stored as.
pub const QUERY_SUGAR: [(&str, &str); 2] = [
    ("refetch", REFETCH_FUNCTION),
    ("fetch_more", FETCH_MORE_FUNCTION),
];
/// Item kinds that are no scalar: a foreach over them is keyed by a stable field, not its position.
const COMPOUND_KINDS: [PortKind; 4] = [
    PortKind::DICT,
    PortKind::MODEL,
    PortKind::LIST,
    PortKind::UNION,
];
pub const FOREACH_KEY_PROP: &str = "key";
/// The field of a task record that holds its returns.
const RETURNS_FIELD: &str = "returns";

/// Port kinds whose fields the port does not declare: a path below such a local is taken as written.
const OPAQUE_KINDS: [PortKind; 4] = [
    PortKind::STRUCTURE,
    PortKind::MEMORY_STRUCTURE,
    PortKind::INTERFACE,
    PortKind::UNION,
];

/// The verb of a query effect (`refetch` for `query.refetch`), or none.
pub fn query_verb(operation: &str) -> Option<&'static str> {
    QUERY_SUGAR
        .iter()
        .find(|(_, function)| *function == operation)
        .map(|(verb, _)| *verb)
}

/// Where a blok is mounted: what its mount provides, which its reads and calls resolve against.
/// The default is a blok checked on its own, mounted nowhere.
#[derive(Debug, Clone, Default)]
pub struct BlokScope {
    /// The kinds the mount takes (an agent's blok: agent; an action's: action or assign); empty
    /// takes any.
    pub kinds: Vec<String>,
    /// What the mount is, for a message (`an action's run bloks`).
    pub mount: String,
    /// What `self` and `deps.<key>` resolve against; a `self` entry types `self`. None: not
    /// mounted, every agent (and a `Placed` key, a local's `like`) taken as written.
    pub dependencies: Option<Vec<AgentDependencyInputModel>>,
    /// The keys `args.<key>` may read; none reads any.
    pub args: Option<Vec<String>>,
    pub demo: Option<BlokDemoInputModel>,
    /// The assets `self.assets.<name>` may name; none names any.
    pub assets: Option<HashSet<String>>,
    /// The full arg ports of the actions this side knows (an agent's own): what a local passed
    /// to one whole, or borrowing one (`like`), takes its port from.
    pub arg_ports: ArgPorts,
    /// The port a port blok is the widget of, as a match: what `port.value` is typed as. None
    /// leaves the value open.
    pub port: Option<PortMatchInputModel>,
    /// Per action of the blok's own agent, that action's dependencies: what an agent blok's
    /// `<dependencies key="cells" like="self.<action>" />` declares as `cells.<dependency>`.
    /// None: not known here, they are then taken as written.
    pub workflows: Option<HashMap<String, Vec<AgentDependencyInputModel>>>,
}

const PICKER_COMPONENT: &str = "deppicker";
/// `cells.options.<dependency>`: the agents that fit a declared dependency, to pick among.
const OPTIONS_SEGMENT: &str = "options";

const CUSTOM_KIND: &str = "custom";
/// The kinds whose blok may declare an action's dependencies itself (`<dependencies>`).
const DECLARING_KINDS: [&str; 2] = ["agent", CUSTOM_KIND];

/// `text` (a path, a call) as a custom blok writes it: a bound agent stored `deps.<key>`
/// reads `agents.<key>`; one through declared dependencies is left.
fn as_written(text: &str) -> Option<String> {
    let rest = text.strip_prefix("deps.")?;
    let key = rest.split(['.', '(']).next().unwrap_or_default();
    (!key.contains(REFERENCE_SEPARATOR)).then(|| format!("{AGENTS_ROOT}.{rest}"))
}

/// The dependency `<key>.<name>` of declared dependencies, as it is stored.
fn reference_key(key: &str, name: &str) -> String {
    format!("{key}{REFERENCE_SEPARATOR}{name}")
}

/// The dependencies `declared` borrows, each keyed `<key>__<dependency>`; or what is wrong with
/// its `like`.
pub fn borrowed_dependencies(
    declared: &DeclaredDependencies,
    workflows: &HashMap<String, Vec<AgentDependencyInputModel>>,
) -> Result<Vec<AgentDependencyInputModel>, String> {
    let Some(action) = declared.action() else {
        return Err("is not self.<action>".to_owned());
    };
    let Some(dependencies) = workflows.get(action) else {
        return Err(format!("names no action of the app ({})", repr(&s(action))));
    };
    if dependencies.is_empty() {
        return Err(format!(
            "names an action with no dependencies ({})",
            repr(&s(action))
        ));
    }
    let taken = dependencies.iter().find(|dependency| {
        dependency.key == OPTIONS_SEGMENT
            || dependency.key == PICK_VERB
            || dependency.key.contains(REFERENCE_SEPARATOR)
    });
    if let Some(taken) = taken {
        return Err(format!(
            "names an action with a dependency called {}, which cannot be read through a reference: rename that parameter of {action}",
            repr(&s(taken.key.clone()))
        ));
    }
    Ok(dependencies
        .iter()
        .map(|dependency| AgentDependencyInputModel {
            key: reference_key(&declared.key, &dependency.key),
            ..dependency.clone()
        })
        .collect())
}

/// `scope` with the dependencies an agent blok declares itself (`<dependencies>`) among its
/// dependencies, keyed `<key>__<dependency>` and demanded as their action demands them. A
/// declaration whose `like` does not resolve is left out (the checker says why), as is every
/// one where the app's actions are not known (`workflows` none).
pub fn bound_scope(tree: &ComponentNodeInputModel, scope: &BlokScope) -> BlokScope {
    let mut bound = scope.clone();
    let Some(workflows) = &scope.workflows else {
        return bound;
    };
    if blok_header(tree).ok().map(|header| header.kind).as_deref() != Some("agent") {
        return bound;
    }
    let mut dependencies = scope.dependencies.clone().unwrap_or_default();
    let mut added = false;
    for declared in declared_dependencies(tree) {
        for dependency in borrowed_dependencies(&declared, workflows).unwrap_or_default() {
            if !dependencies.iter().any(|known| known.key == dependency.key) {
                dependencies.push(dependency);
                added = true;
            }
        }
    }
    if added {
        bound.dependencies = Some(dependencies);
    }
    bound
}

impl BlokScope {
    /// Mounted where `kinds` go, with `dependencies` (none of them is still a mount).
    pub fn mounted(
        kinds: &[&str],
        mount: impl Into<String>,
        dependencies: Vec<AgentDependencyInputModel>,
    ) -> BlokScope {
        BlokScope {
            kinds: kinds.iter().map(|kind| (*kind).to_owned()).collect(),
            mount: mount.into(),
            dependencies: Some(dependencies),
            ..BlokScope::default()
        }
    }

    /// Whether the blok is checked where it is mounted (its dependencies are known).
    pub fn is_mounted(&self) -> bool {
        self.dependencies.is_some()
    }
}

/// What a name in scope is bound to: a value of a known shape, or a `<local>`.
#[derive(Debug, Clone)]
enum Bound {
    Shape(Shape),
    Local {
        node: String,
        local: Box<BlokLocalInputModel>,
    },
}

// --------------------------------------------------------------------------- shapes

/// What a path holds: a typed value, or an open one whose fields are taken as written.
#[derive(Debug, Clone, PartialEq)]
struct Shape {
    key: Option<String>,
    kind: Option<PortKind>,
    open: bool,
    children: Option<Vec<Shape>>,
}

type Resolved = Option<Shape>;

impl Shape {
    fn open(key: Option<&str>) -> Shape {
        Shape {
            key: key.map(str::to_owned),
            kind: None,
            open: true,
            children: None,
        }
    }

    fn typed(key: &str, kind: Option<PortKind>) -> Shape {
        Shape {
            key: Some(key.to_owned()),
            kind,
            open: false,
            children: None,
        }
    }

    fn with_key(mut self, key: &str) -> Shape {
        self.key = Some(key.to_owned());
        self
    }

    /// A port as the shape a path into it descends (the spec's `build_port_match`).
    fn of_port(port: &ArgPortInputModel) -> Shape {
        let children: Vec<Shape> = port.children.iter().flatten().map(Shape::of_port).collect();
        Shape {
            key: Some(port.key.clone()),
            kind: Some(port.kind),
            open: false,
            children: (!children.is_empty()).then_some(children),
        }
    }

    fn of_match(port: &PortMatchInputModel) -> Shape {
        Shape {
            key: port.key.clone(),
            kind: port.kind,
            open: port.identifier.as_deref() == Some("*"),
            children: port
                .children
                .as_ref()
                .map(|children| children.iter().map(Shape::of_match).collect()),
        }
    }
}

fn port_kind(kind: &str) -> Option<PortKind> {
    serde_json::from_value(s(kind)).ok()
}

fn record_shape(key: &str, fields: &[RecordField], overrides: &[(&str, Shape)]) -> Shape {
    let children = fields
        .iter()
        .map(|field| {
            if let Some((_, shape)) = overrides.iter().find(|(k, _)| *k == field.key) {
                return shape.clone().with_key(&field.key);
            }
            if !field.children.is_empty() {
                return record_shape(&field.key, &field.children, &[]);
            }
            if matches!(field.kind.as_str(), "DICT" | "ANY" | "LIST") {
                return Shape::open(Some(&field.key));
            }
            Shape::typed(&field.key, port_kind(&field.kind))
        })
        .collect();
    Shape {
        key: Some(key.to_owned()),
        kind: Some(PortKind::DICT),
        open: false,
        children: Some(children),
    }
}

/// A STRUCTURE as what may be read below it: the `structure` record (`object`, `__identifier`).
fn structure_shape(structure: &Shape) -> Shape {
    let key = structure
        .key
        .as_deref()
        .filter(|k| !k.is_empty())
        .unwrap_or("structure");
    record_shape(key, &vocabulary().records["structure"].fields, &[])
}

fn task_record_shape(key: &str, returns: Option<&[PortMatchInputModel]>) -> Shape {
    let record = &vocabulary().records["task"];
    match returns {
        Some(returns) if !returns.is_empty() => {
            let shape = Shape {
                key: Some("returns".into()),
                kind: Some(PortKind::DICT),
                open: false,
                children: Some(returns.iter().map(Shape::of_match).collect()),
            };
            record_shape(key, &record.fields, &[("returns", shape)])
        }
        _ => record_shape(key, &record.fields, &[]),
    }
}

fn state_shape(state: &StateDependencyInputModel) -> Shape {
    Shape {
        key: Some(state.key.clone()),
        kind: Some(PortKind::DICT),
        open: false,
        children: state
            .demand
            .as_ref()
            .and_then(|d| d.matches.as_ref())
            .map(|matches| matches.iter().map(Shape::of_match).collect()),
    }
}

// --------------------------------------------------------------------------- the walk

/// Where a value sits: the node, the prop, and which scoped roots are in force there.
struct Where<'t> {
    node: &'t ComponentNodeInputModel,
    key: String,
    callback: bool,
    in_error: bool,
}

impl Where<'_> {
    fn text(&self) -> String {
        format!(
            "{} of <{}> ({})",
            self.key, self.node.component, self.node.id
        )
    }
}

fn node_context(node: &ComponentNodeInputModel) -> String {
    format!("<{}> ({})", node.component, node.id)
}

/// The action a task call names.
pub fn action_key_for(call: &AgentProbeInputModel) -> &str {
    call.operation.rsplit('.').next().unwrap_or(&call.operation)
}

/// Whether `call` is an effect (`ui.copy`, `task.answer`, `query.refetch`,
/// `query.fetch_more`), not a pure function.
pub fn is_effect(call: &UtilCallInputModel) -> bool {
    vocabulary()
        .function(&call.operation)
        .is_some_and(|f| f.effect)
}

/// Whether a prop holds a callback's one thing: a task call or an effect.
pub fn is_callback_value(prop: &ComponentPropInputModel) -> bool {
    prop.agent_call.is_some() || prop.util_call.as_ref().is_some_and(is_effect)
}

/// Every name a task call writes its record to (`... into move`), with each call site, in the
/// order they are first written.
pub(crate) fn into_sites(
    node: &ComponentNodeInputModel,
) -> Vec<(String, Vec<(&AgentProbeInputModel, String)>)> {
    fn visit<'t>(
        current: &'t ComponentNodeInputModel,
        found: &mut Vec<(String, Vec<(&'t AgentProbeInputModel, String)>)>,
    ) {
        for prop in current.props.iter().flatten() {
            if let (Some(call), Some(name)) = (
                &prop.agent_call,
                prop.declares_value.as_deref().filter(|n| !n.is_empty()),
            ) {
                let site = (call, format!("{} of {}", prop.key, node_context(current)));
                match found.iter_mut().find(|(n, _)| n == name) {
                    Some((_, sites)) => sites.push(site),
                    None => found.push((name.to_owned(), vec![site])),
                }
            }
        }
        for child in current.children.iter().flatten() {
            visit(child, found);
        }
    }
    let mut found = vec![];
    visit(node, &mut found);
    found
}

/// The loop variable a `foreach` declares.
pub(crate) fn foreach_let(node: &ComponentNodeInputModel) -> Option<&str> {
    if node.component != FOREACH_COMPONENT {
        return None;
    }
    node.props
        .iter()
        .flatten()
        .filter(|p| p.key == FOREACH_LET_PROP)
        .find_map(|p| p.declares_value.as_deref().filter(|v| !v.is_empty()))
}

/// The name a `foreach` gives each item's position (`index="i"`), when it names one.
pub(crate) fn foreach_index(node: &ComponentNodeInputModel) -> Option<&str> {
    if node.component != FOREACH_COMPONENT {
        return None;
    }
    node.props
        .iter()
        .flatten()
        .filter(|p| p.key == FOREACH_INDEX_PROP)
        .find_map(|p| p.declares_value.as_deref().filter(|v| !v.is_empty()))
}

pub(crate) fn iter_nodes(node: &ComponentNodeInputModel) -> Vec<&ComponentNodeInputModel> {
    let mut nodes = vec![node];
    for child in node.children.iter().flatten() {
        nodes.extend(iter_nodes(child));
    }
    nodes
}

fn article(kind: &str) -> &'static str {
    if kind.starts_with(['a', 'e', 'i', 'o', 'u']) {
        "n"
    } else {
        ""
    }
}

fn what(prop: &ComponentPropInputModel) -> String {
    if prop.agent_call.is_some() {
        return "a task call".into();
    }
    if let Some(call) = &prop.util_call {
        return call.operation.clone();
    }
    if let Some(dynamic) = &prop.dynamic_value {
        return format!("the path {}", dynamic.path.as_deref().unwrap_or("None"));
    }
    format!(
        "the value {}",
        repr(prop.static_value.as_ref().unwrap_or(&Value::Null))
    )
}

fn argument_what(argument: &ActionArgumentInputModel) -> String {
    if let Some(path) = &argument.value_path {
        return format!("the path {path}");
    }
    if let Some(call) = &argument.util_call {
        return format!("a call of {}", call.operation);
    }
    if argument.agent_call.is_some() {
        return "a task call".into();
    }
    if argument.value_list.is_some() || argument.value_dict.is_some() {
        return "a list or object".into();
    }
    format!(
        "the value {}",
        repr(argument.value_literal.as_ref().unwrap_or(&Value::Null))
    )
}

/// Every literal string of a `str.classes` call: list entries, object keys, branches.
fn class_literals(call: &UtilCallInputModel) -> Vec<String> {
    fn visit(arguments: &[ActionArgumentInputModel], literals: &mut Vec<String>) {
        for argument in arguments {
            if let Some(Value::String(text)) = &argument.value_literal {
                literals.push(text.clone());
            }
            for nested in argument.value_dict.iter().flatten() {
                if let Some(key) = nested.key.as_deref().filter(|k| !k.is_empty()) {
                    literals.push(key.to_owned());
                }
            }
            if let Some(inner) = &argument.util_call {
                visit(inner.arguments.as_deref().unwrap_or_default(), literals);
            }
            visit(argument.value_list.as_deref().unwrap_or_default(), literals);
        }
    }
    let mut literals = vec![];
    visit(call.arguments.as_deref().unwrap_or_default(), &mut literals);
    literals
}

fn arbitrary_colour_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"^(?P<utility>bg|text|border|ring|fill|stroke|from|via|to|outline|decoration|shadow)-\[(#|rgb|hsl|oklch|color)")
            .expect("the arbitrary colour pattern compiles")
    })
}

fn colour_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        let theme = &vocabulary().theme;
        let mut utilities: Vec<String> = theme
            .colour_utilities
            .iter()
            .map(|u| regex::escape(u))
            .collect();
        utilities.sort_by_key(|u| std::cmp::Reverse(u.len()));
        let colours = theme.palette_colours.join("|");
        Regex::new(&format!(
            r"^(?P<utility>{})-(?P<colour>{colours})(?:-(?P<shade>\d{{2,3}}))?(?:/\d+)?$",
            utilities.join("|")
        ))
        .expect("the colour pattern compiles")
    })
}

type Scope = HashMap<String, Bound>;

/// A list of named declarations as a Python dict built from it reads: each name where it first
/// came, holding the last one of that name.
fn by_name<T: Clone>(items: Vec<T>, name: impl Fn(&T) -> &str) -> Vec<(String, T)> {
    let mut found: Vec<(String, T)> = vec![];
    for item in items {
        let key = name(&item).to_owned();
        match found.iter_mut().find(|(k, _)| *k == key) {
            Some((_, slot)) => *slot = item,
            None => found.push((key, item)),
        }
    }
    found
}

struct Checker<'a> {
    scope: &'a BlokScope,
    header: BlokHeader,
    vocabulary: &'static Vocabulary,
    /// The row of the blok's kind in its context.
    kind: Kind,
    findings: Vec<Finding>,
    mounted: bool,
    dependencies: HashMap<&'a str, &'a AgentDependencyInputModel>,
    /// Per `<local>` node id, its port with its kind resolved, in tree order.
    resolved: Vec<(String, ResolvedLocal)>,
    queries: Vec<(String, BlokQueryInputModel)>,
    subscriptions: Vec<(String, BlokSubscriptionInputModel)>,
    /// The node ids of the locals a control binds.
    bound_locals: HashSet<String>,
    into: HashSet<String>,
    /// Whether the tree places an `<ask>`.
    asks: bool,
    /// The id of the root that says `chrome="none"`, when it does.
    chromeless: Option<String>,
    /// The dependencies an agent blok declares itself, by key.
    declared: Vec<DeclaredDependencies>,
}

impl<'a> Checker<'a> {
    fn declaration(&self, key: &str) -> Option<&DeclaredDependencies> {
        self.declared.iter().find(|declared| declared.key == key)
    }

    /// Whether the dependencies declared as `key` are known here: its action is one of the
    /// app's, so what is read through it is held to that action's dependencies.
    fn typed(&self, key: &str) -> bool {
        match (self.declaration(key), &self.scope.workflows) {
            // A bound agent's actions are known only where the blok runs.
            (Some(declared), Some(workflows)) if declared.owner() == Some("self") => {
                borrowed_dependencies(declared, workflows).is_ok()
            }
            _ => false,
        }
    }

    /// Whether the kind reads the stored `root`: a custom blok's `agents` is stored `deps`.
    fn reads(&self, root: &str) -> bool {
        self.kind.reads(root)
    }

    /// Whether `dependency` (as stored) is one the blok declares in its own tree: every
    /// dependency of an agent blok, a `<key>__<name>` one of a custom blok.
    fn declares(&self, dependency: &str) -> bool {
        self.kind_name() == "agent"
            || (self.kind_name() == CUSTOM_KIND && dependency.contains(REFERENCE_SEPARATOR))
    }

    /// Whether `dependency` (stored `<key>__<name>`) is none this agent blok declares: its
    /// dependencies are the ones in its own tree, mounted or not.
    fn undeclared(&self, dependency: &str) -> bool {
        if !self.declares(dependency) || dependency == "self" {
            return false;
        }
        let Some((key, name)) = dependency.split_once(REFERENCE_SEPARATOR) else {
            return true;
        };
        if self.declaration(key).is_none() {
            return true;
        }
        self.typed(key) && name != OPTIONS_SEGMENT && !self.dependencies.contains_key(dependency)
    }

    /// What `key` may read through, for a message: the dependencies declared under it, else
    /// the keys the blok declares.
    fn dependency_known(&self, key: &str) -> String {
        if self.kind_name() != "agent" && self.declaration(key).is_none() {
            return self.dependency_keys();
        }
        if self.typed(key) {
            let prefix = reference_key(key, "");
            let names: Vec<String> = self
                .dependencies
                .keys()
                .filter_map(|name| name.strip_prefix(prefix.as_str()))
                .map(|name| format!("{key}.{name}"))
                .collect();
            return sorted_keys(names.iter().map(String::as_str));
        }
        sorted_keys(self.declared.iter().map(|declared| declared.key.as_str()))
    }

    fn unknown_dependency(&mut self, at: &str, path: &str, shown: &str, key: &str) {
        let fields = [
            ("path", s(path)),
            ("key", s(shown)),
            ("keys", s(self.dependency_known(key))),
            ("hint", s(dependency_hint(key, self.kind_name()))),
        ];
        self.add("deps.unknown", Some(at), &fields);
    }

    /// `<key>.options.<dependency>`: the agents that fit it, each `{id, name, connected, picked}`.
    fn options(&mut self, key: &str, rest: &[&str], path: &str, node_id: &str) -> Resolved {
        let Some(name) = rest.first() else {
            let root = format!("{key}.{OPTIONS_SEGMENT}");
            self.add(
                "roots.whole",
                Some(node_id),
                &[("path", s(path)), ("root", s(root))],
            );
            return None;
        };
        if self.typed(key)
            && !self
                .dependencies
                .contains_key(reference_key(key, name).as_str())
        {
            self.unknown_dependency(node_id, path, &format!("{key}.{name}"), key);
            return None;
        }
        let field = |key: &str, kind: PortKind| Shape::typed(key, Some(kind));
        let option = Shape {
            key: Some("...".to_owned()),
            kind: Some(PortKind::DICT),
            open: false,
            children: Some(vec![
                field("id", PortKind::STRING),
                field("name", PortKind::STRING),
                field("connected", PortKind::BOOL),
                field("picked", PortKind::BOOL),
            ]),
        };
        let listed = Shape {
            key: Some((*name).to_owned()),
            kind: Some(PortKind::LIST),
            open: false,
            children: Some(vec![option]),
        };
        self.descend(Some(listed), &rest[1..], path, node_id)
    }

    /// `<key>.pick(<dependency>: <agent id>)`: declared dependencies, and dependencies of them.
    fn check_pick(&mut self, call: &UtilCallInputModel, where_: &Where<'_>) {
        let argument = |key: &str| {
            call.arguments
                .iter()
                .flatten()
                .find(|argument| argument.key.as_deref() == Some(key))
        };
        let key = argument("key")
            .and_then(|named| named.value_literal.as_ref())
            .and_then(Value::as_str)
            .map(str::to_owned);
        let Some(key) = key.filter(|key| self.declaration(key).is_some()) else {
            let shown = argument("key")
                .and_then(|named| named.value_literal.as_ref())
                .and_then(Value::as_str)
                .unwrap_or("?")
                .to_owned();
            let path = format!("{shown}.{PICK_VERB}(...)");
            self.unknown_dependency(&where_.node.id, &path, &shown, &shown);
            return;
        };
        if !self.typed(&key) {
            return;
        }
        let picked: Vec<String> = argument("picks")
            .and_then(|picks| picks.value_dict.as_ref())
            .into_iter()
            .flatten()
            .filter_map(|pick| pick.key.clone())
            .collect();
        for name in picked {
            if !self
                .dependencies
                .contains_key(reference_key(&key, &name).as_str())
            {
                let path = format!("{key}.{PICK_VERB}(...)");
                self.unknown_dependency(&where_.node.id, &path, &format!("{key}.{name}"), &key);
            }
        }
    }

    /// `<deppicker key="cells" />` or `key="cells.microscope"`: declared dependencies.
    fn check_picker(&mut self, node: &ComponentNodeInputModel) {
        let written = node
            .props
            .iter()
            .flatten()
            .find(|prop| prop.key == KEY_PROP)
            .and_then(|prop| prop.static_value.as_ref())
            .and_then(Value::as_str);
        let Some(written) = written else {
            return; // a catalog says a key is needed
        };
        let (key, name) = written.split_once('.').unwrap_or((written, ""));
        let known = self.declaration(key).is_some()
            && (name.is_empty()
                || !self.typed(key)
                || self
                    .dependencies
                    .contains_key(reference_key(key, name).as_str()));
        if !known {
            let path = format!("{}.key", node.component);
            self.unknown_dependency(&node.id, &path, written, key);
        }
    }

    /// A `<dependencies>` is a child of an agent blok's `<Blok>`, keyed once, and names one of
    /// the app's own actions that has dependencies.
    fn check_dependencies(&mut self, tree: &ComponentNodeInputModel) {
        for (node, ancestors) in iter_with_parents(tree) {
            if node.component != DEPENDENCIES_COMPONENT {
                continue;
            }
            let literal = |key: &str| {
                node.props
                    .iter()
                    .flatten()
                    .find(|prop| prop.key == key)
                    .and_then(|prop| prop.static_value.as_ref())
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            };
            let key = literal(KEY_PROP);
            if ancestors.len() != 1 || !DECLARING_KINDS.contains(&self.kind_name()) {
                let at = match ancestors.last() {
                    Some(parent) if ancestors.len() != 1 => parent.id.clone(),
                    _ => format!("a{} {} blok", article(self.kind_name()), self.kind_name()),
                };
                let fields = [
                    ("key", s(key.unwrap_or_else(|| "?".to_owned()))),
                    ("at", s(at)),
                ];
                self.add("dependencies.position", Some(&node.id), &fields);
                continue;
            }
            let Some(key) = key.filter(|key| is_name(key)) else {
                let fields = [
                    ("node", s(node.id.clone())),
                    ("problem", s("has no plain key")),
                ];
                self.add("dependencies.key", Some(&node.id), &fields);
                continue;
            };
            if key == "self" || self.declaration(&key).is_some() {
                let problem = if key == "self" {
                    "is keyed self, which is the blok's own agent".to_owned()
                } else {
                    format!("declares {} a second time", repr(&s(key.clone())))
                };
                let fields = [("node", s(node.id.clone())), ("problem", s(problem))];
                self.add("dependencies.key", Some(&node.id), &fields);
                continue;
            }
            let declared = DeclaredDependencies {
                key: key.clone(),
                node: node.id.clone(),
                like: literal(LIKE_PROP),
            };
            let owner = declared.owner();
            let problem = if self.kind_name() == CUSTOM_KIND {
                match owner {
                    None | Some("self") => Some("is not agents.<name>.<action>".to_owned()),
                    Some(owner) if self.mounted && !self.dependencies.contains_key(owner) => {
                        Some(format!(
                            "names the agent {}, which the blok is not bound to",
                            repr(&s(owner))
                        ))
                    }
                    Some(_) => None,
                }
            } else if owner != Some("self") {
                Some("is not self.<action>".to_owned())
            } else {
                self.scope
                    .workflows
                    .as_ref()
                    .and_then(|workflows| borrowed_dependencies(&declared, workflows).err())
            };
            self.declared.push(declared.clone());
            if let Some(problem) = problem {
                let fields = [
                    ("key", s(key)),
                    ("like", s(declared.like.unwrap_or_default())),
                    ("problem", s(problem)),
                ];
                self.add("dependencies.like", Some(&node.id), &fields);
            }
        }
    }

    /// `with <key>`: dependencies the blok declares, of the action that is called.
    fn check_with(&mut self, call: &AgentProbeInputModel, where_: &Where<'_>) {
        let given = call.given.clone().unwrap_or_default();
        let action = action_key_for(call).to_owned();
        let Some(declared) = self.declaration(&given).cloned() else {
            let keys = sorted_keys(self.declared.iter().map(|declared| declared.key.as_str()));
            let fields = [
                ("path", s(format!("with {given}"))),
                ("key", s(given.clone())),
                ("keys", s(keys)),
                ("hint", s(dependency_hint(&given, self.kind_name()))),
            ];
            self.add("deps.unknown", Some(&where_.node.id), &fields);
            return;
        };
        let called = (call.dependency.as_str(), action.as_str());
        if let Some((_, of)) = declared.target().filter(|target| *target != called) {
            let fields = [
                ("path", s(where_.text())),
                ("key", s(given)),
                ("action", s(action)),
                ("of", s(of)),
            ];
            self.add("with.mismatch", Some(&where_.node.id), &fields);
        }
    }

    fn add(&mut self, code: &str, at: Option<&str>, fields: &[(&str, Value)]) {
        if self.kind_name() != CUSTOM_KIND {
            self.findings.push(Finding::of(code, at, fields));
            return;
        }
        let written: Vec<(&str, Value)> = fields
            .iter()
            .map(|(key, value)| {
                let rewritten = value.as_str().and_then(as_written).map(s);
                (*key, rewritten.unwrap_or_else(|| value.clone()))
            })
            .collect();
        self.findings.push(Finding::of(code, at, &written));
    }

    fn kind_name(&self) -> &str {
        &self.header.kind
    }

    fn not_in_kind(&mut self, at: &str, path: &str, root: &str) {
        let fields = [
            ("path", s(path)),
            ("n", s(article(self.kind_name()))),
            ("kind", s(self.kind_name())),
            ("root", s(root)),
            ("roots", s(self.kind.roots_text())),
        ];
        self.add("roots.not_in_kind", Some(at), &fields);
    }

    fn query(&self, name: &str) -> Option<&BlokQueryInputModel> {
        self.queries
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, query)| query)
    }

    fn resolved_local(&self, node: &str) -> Option<&ResolvedLocal> {
        self.resolved
            .iter()
            .find(|(id, _)| id == node)
            .map(|(_, resolved)| resolved)
    }

    // -- declarations

    fn check_declarations(&mut self, tree: &'a ComponentNodeInputModel) {
        let vocabulary = self.vocabulary;
        let kind = self.kind_name().to_owned();
        let row = vocabulary.kind(&kind).expect("the header names a kind");
        let context = self.header.context.clone();
        let spelled = context.to_lowercase();
        if !row.contexts.contains(&context) {
            let contexts: Vec<String> = row.contexts.iter().map(|c| c.to_lowercase()).collect();
            let fields = [
                ("n", s(article(&kind))),
                ("kind", s(kind.clone())),
                ("contexts", s(contexts.join(", "))),
                ("context", s(spelled.clone())),
            ];
            self.add("context.kind", Some(&tree.id), &fields);
        }
        if !self.scope.kinds.is_empty() && !self.scope.kinds.contains(&kind) {
            let mount = if self.scope.mount.is_empty() {
                "here".to_owned()
            } else {
                self.scope.mount.clone()
            };
            let fields = [
                ("n", s(article(&kind))),
                ("kind", s(kind.clone())),
                ("m", s(article(&self.scope.kinds[0]))),
                ("expected", s(self.scope.kinds.join(" or "))),
                ("where", s(mount)),
            ];
            self.add("kind.placement", Some(&tree.id), &fields);
        }
        if self.header.chrome.is_some() && !vocabulary.blok.chrome.contains(&context) {
            self.add(
                "chrome.context",
                Some(&tree.id),
                &[("context", s(spelled.clone()))],
            );
        }
        if let Some(allowed) = vocabulary.blok.content.get(&context) {
            let drawn = content(tree);
            if drawn.len() != 1 || !allowed.contains(&drawn[0].component) {
                let found: Vec<String> = drawn
                    .iter()
                    .map(|node| {
                        if node.component == TEXT_NODE {
                            "text".to_owned()
                        } else {
                            format!("<{}>", node.component)
                        }
                    })
                    .collect();
                let found = if found.is_empty() {
                    "nothing".to_owned()
                } else {
                    found.join(", ")
                };
                let expected: Vec<String> =
                    allowed.iter().map(|name| format!("one <{name}>")).collect();
                let fields = [
                    ("context", s(spelled.clone())),
                    ("expected", s(expected.join(" or "))),
                    ("found", s(found)),
                ];
                self.add("blok.content", Some(&tree.id), &fields);
            }
        }
        self.check_dependencies(tree);
        self.check_local_positions(tree);
        self.check_queries(tree);
        self.check_logo_declaration(tree);
        for (name, sites) in into_sites(tree) {
            self.into.insert(name.clone());
            if vocabulary.is_reserved(&name) {
                self.add(
                    "roots.reserved",
                    Some(&sites[0].1),
                    &[("name", s(name.clone()))],
                );
            }
            if sites.len() > 1 {
                let fields = [
                    ("name", s(name.clone())),
                    ("first", s(sites[0].1.clone())),
                    ("second", s(sites[1].1.clone())),
                ];
                self.add("into.twice", Some(&sites[1].1), &fields);
            }
        }
    }

    /// The blok's own `<logo>`: a child of its `<Blok>`, one, in a blok that is no logo, and a
    /// fixed drawing. What it draws is checked as a logo blok ([`check_blok`]). The port of
    /// arkitekt-spec's `check_logo_declaration`.
    fn check_logo_declaration(&mut self, tree: &'a ComponentNodeInputModel) {
        let mut own = 0;
        for (node, ancestors) in iter_with_parents(tree) {
            if node.component != LOGO_COMPONENT {
                continue;
            }
            if ancestors.len() != 1 {
                self.add("logo.misplaced", Some(&node.id), &[]);
                continue;
            }
            own += 1;
            if own > 1 {
                self.add("logo.twice", Some(&node.id), &[]);
            } else if self.header.context == LOGO {
                self.add("logo.in_logo", Some(&node.id), &[]);
            }
            for (drawn, _) in iter_with_parents(node) {
                for prop in drawn.props.iter().flatten() {
                    if prop.dynamic_value.is_some()
                        || prop.agent_call.is_some()
                        || prop.util_call.is_some()
                    {
                        let fields = [
                            ("component", s(drawn.component.clone())),
                            ("prop", s(prop.key.clone())),
                        ];
                        self.add("logo.dynamic", Some(&drawn.id), &fields);
                    }
                }
            }
        }
    }

    /// Each `<local>` reads, comes first among its parent's children, inside an element that
    /// takes children, in a kind that has locals; a persisted one inside a keyless foreach is a
    /// warning.
    fn check_local_positions(&mut self, tree: &ComponentNodeInputModel) {
        for (node, ancestors) in iter_with_parents(tree) {
            if node.component == VAR_COMPONENT
                && !ancestors.last().is_some_and(|parent| {
                    parent.component == QUERY_COMPONENT
                        || parent.component == SUBSCRIPTION_COMPONENT
                })
            {
                let fields = [
                    ("component", s("var")),
                    ("problem", s("belongs inside a <query> or <subscription>")),
                ];
                self.add("query.declaration", Some(&node.id), &fields);
            }
            if node.component != LOCAL_COMPONENT {
                continue;
            }
            let name = local_name(node).unwrap_or("?").to_owned();
            if let Some(problem) = local_problem(node) {
                self.findings.push(problem);
                continue;
            }
            if !self.kind.locals {
                let path = format!("<local name={}>", repr(&s(name.clone())));
                self.not_in_kind(&node.id, &path, "locals");
            }
            if self.vocabulary.is_reserved(&name) {
                self.add(
                    "roots.reserved",
                    Some(&node.id),
                    &[("name", s(name.clone()))],
                );
            }
            let Some(parent) = ancestors.last() else {
                let fields = [
                    ("name", s(name.clone())),
                    ("problem", s("is a blok's root; a blok is a <Blok>")),
                ];
                self.add("local.position", Some(&node.id), &fields);
                continue;
            };
            let siblings = parent.children.as_deref().unwrap_or_default();
            let other = siblings
                .iter()
                .take_while(|sibling| !std::ptr::eq(*sibling, node))
                .find(|sibling| sibling.component != LOCAL_COMPONENT);
            if let Some(other) = other {
                let shown = if other.component == TEXT_NODE {
                    "text".to_owned()
                } else {
                    format!("<{}>", other.component)
                };
                let problem = format!(
                    "comes after {shown} ({}); locals come first among their parent's children",
                    other.id
                );
                self.add(
                    "local.position",
                    Some(&node.id),
                    &[("name", s(name.clone())), ("problem", s(problem))],
                );
            }
            if [
                QUERY_COMPONENT,
                SUBSCRIPTION_COMPONENT,
                LOCAL_COMPONENT,
                VAR_COMPONENT,
            ]
            .contains(&parent.component.as_str())
            {
                let problem = format!(
                    "is inside <{}>, which declares, and draws nothing",
                    parent.component
                );
                self.add(
                    "local.position",
                    Some(&node.id),
                    &[("name", s(name.clone())), ("problem", s(problem))],
                );
            }
            // Whether the parent takes children at all is its catalog's to say: none is read here.
            let Some(resolved) = self.resolved_local(&node.id).cloned() else {
                continue;
            };
            if let Some(problem) = &resolved.like_problem {
                let like = node
                    .props
                    .iter()
                    .flatten()
                    .find(|prop| prop.key == LIKE_PROP)
                    .and_then(|prop| prop.static_value.clone())
                    .unwrap_or(Value::Null);
                let fields = [
                    ("name", s(name.clone())),
                    ("like", like),
                    ("problem", s(problem.clone())),
                ];
                self.add("local.like", Some(&node.id), &fields);
            }
            self.check_local_default(&node.id, &resolved.local);
            if resolved.local.persist {
                let loop_ = ancestors
                    .iter()
                    .rev()
                    .find(|ancestor| ancestor.component == FOREACH_COMPONENT);
                if loop_.is_some_and(|loop_| {
                    !loop_
                        .props
                        .iter()
                        .flatten()
                        .any(|p| p.key == FOREACH_KEY_PROP)
                }) {
                    self.add("local.persist_id", Some(&node.id), &[("name", s(name))]);
                }
            }
        }
    }

    /// Queries and subscriptions are children of the `<Blok>`, each declared once and readable.
    fn check_queries(&mut self, tree: &ComponentNodeInputModel) {
        let mut first: Vec<((String, String), String)> = vec![];
        for (node, ancestors) in iter_with_parents(tree) {
            if node.component != QUERY_COMPONENT && node.component != SUBSCRIPTION_COMPONENT {
                continue;
            }
            let name = node
                .props
                .iter()
                .flatten()
                .find(|prop| {
                    prop.key == NAME_PROP
                        && prop.static_value.as_ref().is_some_and(Value::is_string)
                })
                .and_then(|prop| prop.static_value.as_ref())
                .and_then(Value::as_str)
                .unwrap_or("?")
                .to_owned();
            if ancestors.len() != 1 {
                let at = ancestors
                    .last()
                    .map_or_else(|| "the root".to_owned(), |parent| parent.id.clone());
                let fields = [
                    ("component", s(node.component.clone())),
                    ("name", s(name.clone())),
                    ("at", s(at)),
                ];
                self.add("query.position", Some(&node.id), &fields);
            }
            if let Some(problem) = declaration_problem(node) {
                self.findings.push(problem);
                continue;
            }
            let key = (node.component.clone(), name.clone());
            match first.iter().find(|(k, _)| *k == key) {
                Some((_, seen)) => {
                    let fields = [
                        ("what", s(node.component.clone())),
                        ("name", s(name)),
                        ("first", s(seen.clone())),
                        ("second", s(node.id.clone())),
                    ];
                    self.add("query.duplicate", Some(&node.id), &fields);
                }
                None => first.push((key, node.id.clone())),
            }
        }
        self.queries = by_name(declared_queries(tree), |query| query.key.as_str());
        self.subscriptions = by_name(declared_subscriptions(tree), |subscription| {
            subscription.key.as_str()
        });
        let declared: Vec<(&str, String, String)> = self
            .queries
            .iter()
            .map(|(_, query)| ("query", query.key.clone(), query.document.clone()))
            .chain(self.subscriptions.iter().map(|(_, subscription)| {
                (
                    "subscription",
                    subscription.key.clone(),
                    subscription.document.clone(),
                )
            }))
            .collect();
        for (what, key, document) in declared {
            let roots = if what == "query" {
                "queries"
            } else {
                "subscriptions"
            };
            let at = format!("{roots}.{key}");
            if !is_name(&key) {
                self.add(
                    "manifest.name",
                    Some(&at),
                    &[("what", s(what)), ("key", s(key.clone()))],
                );
            }
            if self.vocabulary.is_reserved(&key) {
                self.add("roots.reserved", Some(&at), &[("name", s(key.clone()))]);
            }
            let operation = operation_keyword(&document);
            if operation == "mutation" {
                let fields = [
                    ("kind", s(what)),
                    ("name", s(key.clone())),
                    (
                        "problem",
                        s("is a mutation: a blok reads, and writes only through a task"),
                    ),
                ];
                self.add("query.read_only", Some(&at), &fields);
            } else if operation != what {
                let operation = if operation.is_empty() {
                    "document without an operation".to_owned()
                } else {
                    operation
                };
                let fields = [
                    ("kind", s(what)),
                    ("name", s(key.clone())),
                    ("operation", s(operation)),
                ];
                self.add("query.kind", Some(&at), &fields);
            }
        }
        let more: Vec<(String, String)> = self
            .queries
            .iter()
            .filter_map(|(_, query)| query.more.clone().map(|more| (query.key.clone(), more)))
            .collect();
        for (key, more) in more {
            if !is_field_path(&more) {
                let fields = [
                    ("name", s(key.clone())),
                    ("more", s(more)),
                    (
                        "problem",
                        s("is not a field path into its data (positions, images.items)"),
                    ),
                ];
                self.add("query.more", Some(&format!("queries.{key}")), &fields);
            }
        }
        if self.header.kind == "action"
            && self.header.context == LOGO
            && !self.dependencies.is_empty()
        {
            let at = self.header.node.clone();
            self.add("manifest.action_logo", Some(&at), &[]);
        }
        let histories: Vec<(String, String)> = self
            .subscriptions
            .iter()
            .filter_map(|(_, subscription)| {
                subscription
                    .history
                    .clone()
                    .map(|history| (subscription.key.clone(), history))
            })
            .collect();
        for (key, history) in histories {
            if self.query(&history).is_none() {
                let fields = [("name", s(key.clone())), ("history", s(history))];
                self.add(
                    "query.history",
                    Some(&format!("subscriptions.{key}")),
                    &fields,
                );
            }
        }
    }

    /// A local's default fits its port: a number for a number, a list for a list, one of its
    /// choices.
    fn check_local_default(&mut self, node_id: &str, local: &BlokLocalInputModel) {
        let Some(port) = local.arg_port() else {
            return;
        };
        let Some(default) = port.default.as_ref().filter(|d| !d.is_null()) else {
            return;
        };
        let kind = port.kind.value();
        let wrong = match kind {
            "STRING" | "DATE" | "STRUCTURE" => !default.is_string(),
            "INT" => !(default.is_i64() || default.is_u64()),
            "FLOAT" | "QUANTITY" => !default.is_number(),
            "BOOL" => !default.is_boolean(),
            "LIST" => !default.is_array(),
            "DICT" | "MODEL" => !default.is_object(),
            "ENUM" => port
                .choices
                .as_ref()
                .is_some_and(|choices| !choices.iter().any(|c| &c.value == default)),
            _ => false,
        };
        if wrong {
            let fields = [
                ("name", s(local.key())),
                ("default", default.clone()),
                ("kind", s(kind)),
            ];
            self.add("local.default", Some(node_id), &fields);
        }
    }

    /// The port a path into a local names (the local, or a field of it), or none when its
    /// shape does not say (an ANY local).
    fn local_port<'l>(local: &'l BlokLocalInputModel, path: &str) -> Option<&'l ArgPortInputModel> {
        let mut port: &ArgPortInputModel = local.arg_port()?;
        for part in path.split('.').skip(1) {
            let children = port.children.as_deref().unwrap_or_default();
            let child = children.iter().find(|c| c.key == part).or(match children {
                [only] if only.key == ITEM => Some(only),
                _ => None,
            });
            port = child?;
        }
        Some(port)
    }

    fn finish(&mut self) {
        let unused: Vec<(String, String)> = self
            .resolved
            .iter()
            .filter(|(node, resolved)| resolved.local.persist && !self.bound_locals.contains(node))
            .map(|(node, resolved)| (node.clone(), resolved.local.key().to_owned()))
            .collect();
        for (node, name) in unused {
            self.add("local.unused_persist", Some(&node), &[("name", s(name))]);
        }
        if let Some(root) = self.chromeless.clone() {
            if self.kind.roots.contains("task") && !self.asks {
                self.add("chrome.no_ask", Some(&root), &[]);
            }
        }
    }

    // -- the walk

    fn walk_node(
        &mut self,
        node: &'a ComponentNodeInputModel,
        scope: &Scope,
        ancestors: &mut Vec<&'a ComponentNodeInputModel>,
        in_error: bool,
    ) {
        self.visit_component(node, ancestors, scope);
        let mut inner = scope.clone();
        let let_ = foreach_let(node);
        if let Some(let_) = let_ {
            let items = node
                .props
                .iter()
                .flatten()
                .find(|p| p.key == FOREACH_ITEMS_PROP);
            let where_ = Where {
                node,
                key: FOREACH_ITEMS_PROP.into(),
                callback: false,
                in_error,
            };
            let bound = self.bind_let(let_, items, &where_, scope);
            inner.insert(let_.to_owned(), Bound::Shape(bound));
        }
        if let Some(index) = foreach_index(node) {
            let where_ = Where {
                node,
                key: FOREACH_INDEX_PROP.into(),
                callback: false,
                in_error,
            };
            let bound = self.bind_index(index, &where_, &inner);
            inner.insert(index.to_owned(), Bound::Shape(bound));
        }
        for prop in node.props.iter().flatten() {
            let prop_scope = if let_.is_some() && prop.key == FOREACH_ITEMS_PROP {
                scope
            } else {
                &inner
            };
            self.walk_prop(node, prop, prop_scope, in_error);
        }
        let in_slot = in_error
            || (node.component == ERROR_SLOT
                && ancestors
                    .last()
                    .is_some_and(|a| a.component == SUSPENSE_COMPONENT));
        let children = node.children.as_deref().unwrap_or_default();
        for child in children {
            if let Some(name) = local_declared(child) {
                let bound = self.bind_local(name, child, &inner);
                inner.insert(name.to_owned(), bound);
            }
        }
        ancestors.push(node);
        for child in children {
            // A blok's own <logo> draws nothing in it and reads nothing: it is checked as a logo
            // blok of its own (`blok_logo_tree`), never as part of the blok it stands for.
            if child.component == LOGO_COMPONENT {
                continue;
            }
            self.walk_node(child, &inner, ancestors, in_slot);
        }
        ancestors.pop();
    }

    fn walk_prop(
        &mut self,
        node: &'a ComponentNodeInputModel,
        prop: &'a ComponentPropInputModel,
        scope: &Scope,
        in_error: bool,
    ) {
        let where_ = Where {
            node,
            key: prop.key.clone(),
            callback: is_callback_value(prop),
            in_error,
        };
        if let Some(path) = prop.dynamic_value.as_ref().and_then(|d| d.path.as_deref()) {
            self.resolve(path, Some(&where_), scope, &where_.text());
        }
        if let Some(call) = &prop.agent_call {
            self.visit_agent_call(call, &where_, scope);
            for argument in call.arguments.iter().flatten() {
                self.walk_argument(argument, scope, &where_);
            }
        }
        if let Some(call) = &prop.util_call {
            self.walk_util(call, scope, &where_, true);
        }
    }

    fn walk_util(
        &mut self,
        call: &UtilCallInputModel,
        scope: &Scope,
        where_: &Where<'_>,
        top: bool,
    ) {
        self.visit_util_call(call, where_, top);
        for argument in call.arguments.iter().flatten() {
            self.walk_argument(argument, scope, where_);
        }
    }

    fn walk_argument(
        &mut self,
        argument: &ActionArgumentInputModel,
        scope: &Scope,
        where_: &Where<'_>,
    ) {
        if let Some(path) = &argument.value_path {
            self.resolve(path, Some(where_), scope, &where_.text());
        }
        if let Some(call) = &argument.agent_call {
            self.visit_agent_call(call, where_, scope);
            for nested in call.arguments.iter().flatten() {
                self.walk_argument(nested, scope, where_);
            }
        }
        if let Some(call) = &argument.util_call {
            self.walk_util(call, scope, where_, false);
        }
        for nested in argument
            .value_list
            .iter()
            .flatten()
            .chain(argument.value_dict.iter().flatten())
        {
            self.walk_argument(nested, scope, where_);
        }
    }

    // -- the visitor

    fn visit_component(
        &mut self,
        node: &ComponentNodeInputModel,
        ancestors: &[&ComponentNodeInputModel],
        scope: &Scope,
    ) {
        self.check_host_part(node, ancestors);
        if node.component == PICKER_COMPONENT {
            self.check_picker(node);
        }
        for prop in node.props.iter().flatten() {
            self.check_prop(node, prop, scope);
        }
    }

    /// What the language itself says of the host's parts, whatever catalog draws them: a part
    /// placed anywhere claims its slot, and a root without chrome leaves the asks to the blok.
    fn check_host_part(
        &mut self,
        node: &ComponentNodeInputModel,
        ancestors: &[&ComponentNodeInputModel],
    ) {
        if node.component == ASK_COMPONENT {
            self.asks = true;
        }
        if self.vocabulary.host_parts.contains(&node.component) {
            let inside = ancestors
                .iter()
                .rev()
                .find(|ancestor| CONDITIONAL_COMPONENTS.contains(&ancestor.component.as_str()))
                .map(|ancestor| ancestor.component.clone());
            if let Some(inside) = inside {
                let fields = [
                    ("component", s(node.component.clone())),
                    ("inside", s(inside)),
                ];
                self.add("part.conditional", Some(&node.id), &fields);
            }
        }
    }

    fn assign_kind(&mut self, node: &ComponentNodeInputModel, what: &str) {
        let fields = [
            ("what", s(what)),
            ("n", s(article(self.kind_name()))),
            ("kind", s(self.kind_name())),
        ];
        self.add("assign.kind", Some(&node.id), &fields);
    }

    /// A `Placed dep` or `Route through` names dependencies of the blok, written out as keys.
    fn check_dependency_keys(
        &mut self,
        node: &ComponentNodeInputModel,
        prop: &ComponentPropInputModel,
    ) {
        // Which kinds a Space and its parts belong to is their catalog's to say; mounted nowhere,
        // the keys are taken as written.
        if !RUN_KINDS.contains(&self.kind_name()) || !self.mounted {
            return;
        }
        let path = format!("{}.{}", node.component, prop.key);
        let keys: Vec<String> = match &prop.static_value {
            Some(Value::String(key)) => vec![key.clone()],
            Some(Value::Array(items)) => items
                .iter()
                .filter_map(|item| item.as_str().map(str::to_owned))
                .collect(),
            _ => vec![],
        };
        for key in keys {
            if !self.dependencies.contains_key(key.as_str()) {
                let fields = [
                    ("path", s(path.clone())),
                    ("key", s(key.clone())),
                    ("keys", s(self.dependency_keys())),
                    ("hint", s(dependency_hint(&key, self.kind_name()))),
                ];
                self.add("deps.unknown", Some(&node.id), &fields);
            }
        }
    }

    fn check_prop(
        &mut self,
        node: &ComponentNodeInputModel,
        prop: &ComponentPropInputModel,
        scope: &Scope,
    ) {
        let vocabulary = self.vocabulary;
        let at = Some(node.id.as_str());
        // `task.answer(...)`, `task.cancel()` and their kin act on the task the blok is about.
        let operation = prop
            .util_call
            .as_ref()
            .and_then(|call| vocabulary.functions.get(&call.operation))
            .filter(|function| function.task_operation)
            .map(|function| function.name.clone());
        if let Some(operation) = operation {
            if !self.kind.roots.contains("task") {
                self.not_in_kind(&node.id, &operation, "task");
            }
        }
        // An effect that is a root's own (port.set) belongs to the kinds that read that root.
        let needed = prop
            .util_call
            .as_ref()
            .and_then(|call| vocabulary.functions.get(&call.operation))
            .and_then(|function| Some((function.name.clone(), function.needs.clone()?)));
        if let Some((operation, root)) = needed {
            if !self.reads(&root) {
                self.not_in_kind(&node.id, &operation, &root);
            }
        }
        if prop.key == BIND_PROP {
            match prop.dynamic_value.as_ref().and_then(|d| d.path.as_deref()) {
                None => self.add("local.bind", at, &[("what", s(what(prop)))]),
                Some(path) => {
                    let root = path.split('.').next().unwrap_or_default();
                    match scope.get(root) {
                        // The one thing besides a local a control writes: the value of the port
                        // a port blok stands for (and what is below it), never what the form
                        // says of it.
                        None if root == PORT_ROOT && self.kind.roots.contains(PORT_ROOT) => {
                            if path.split('.').nth(1) != Some(PORT_VALUE_FIELD) {
                                self.add("port.bind", at, &[("path", s(path))]);
                            }
                        }
                        Some(Bound::Local { node: local, .. }) => {
                            self.bound_locals.insert(local.clone());
                        }
                        _ => self.add("local.unknown", at, &[("path", s(path)), ("root", s(root))]),
                    }
                }
            }
        }
        if prop
            .util_call
            .as_ref()
            .is_some_and(|call| call.operation == START_EFFECT)
            && self.kind_name() != ASSIGN_KIND
        {
            self.assign_kind(node, "assign.start()");
        }
        if DEPENDENCY_KEY_PROPS.contains(&(node.component.as_str(), prop.key.as_str())) {
            self.check_dependency_keys(node, prop);
        }
        if node.component == TOAST_COMPONENT && prop.key == TOAST_TASK_PROP {
            let path = prop.dynamic_value.as_ref().and_then(|d| d.path.as_deref());
            if path.is_none_or(|p| p != "task" && !self.into.contains(p)) {
                let written = path.map_or_else(|| what(prop), str::to_owned);
                self.add("toast.task", at, &[("what", s(written))]);
            }
        }
        if let Some(Value::String(value)) = &prop.static_value {
            if prop.key == CLASS_NAME {
                self.check_classes(value, &node.id);
            }
            if self.header.context == LOGO && vocabulary.theme.logo_paint_props.contains(&prop.key)
            {
                self.check_paint(node, &prop.key, value);
            }
        }
        if prop.key == CLASS_NAME {
            if let Some(call) = &prop.util_call {
                for literal in class_literals(call) {
                    self.check_classes(&literal, &node.id);
                }
            }
        }
    }

    fn check_classes(&mut self, classes: &str, owner: &str) {
        let theme = &self.vocabulary.theme;
        for token in classes.split_whitespace() {
            let base = token
                .rsplit(':')
                .next()
                .unwrap_or(token)
                .trim_start_matches('!');
            if let Some(found) = colour_pattern().captures(base) {
                let role = theme
                    .palette_role_hints
                    .get(&found["colour"])
                    .map_or("primary", String::as_str);
                let suggestion = format!("{}-{role}", &found["utility"]);
                self.add(
                    "palette.raw_colour",
                    Some(owner),
                    &[("token", s(token)), ("suggestion", s(suggestion))],
                );
                continue;
            }
            if let Some(found) = arbitrary_colour_pattern().captures(base) {
                let roles = theme
                    .colour_roles
                    .iter()
                    .take(6)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(", ");
                let suggestion = format!("{}-<role> ({roles}, ...)", &found["utility"]);
                self.add(
                    "palette.raw_colour",
                    Some(owner),
                    &[("token", s(token)), ("suggestion", s(suggestion))],
                );
                continue;
            }
            // A colour that is no role is told as that; anything else outside the portable subset as this.
            if let Some(what) = unportable_class(token, self.vocabulary) {
                self.add(
                    "class.not_portable",
                    Some(owner),
                    &[("token", s(token)), ("what", s(what))],
                );
            }
        }
    }

    fn check_paint(&mut self, node: &ComponentNodeInputModel, key: &str, value: &str) {
        let paint = value.trim();
        let pens = &self.vocabulary.theme.logo_pens;
        if pens.iter().any(|p| p == paint) || (paint.starts_with("url(#") && paint.ends_with(')')) {
            return;
        }
        let fields = [
            ("component", s(node.component.clone())),
            ("prop", s(key)),
            ("value", s(value)),
            ("pens", s(pens.join("|"))),
        ];
        self.add("palette.logo_paint", Some(&node.id), &fields);
    }

    /// A scene paints with the theme's colour roles, resolved by the renderer against the
    /// viewer's brand.
    fn visit_agent_call(&mut self, call: &AgentProbeInputModel, where_: &Where<'_>, scope: &Scope) {
        let owner = where_.text();
        let action = action_key_for(call).to_owned();
        if !self.kind.calls {
            let called = if call.dependency == "self" {
                format!("self.{action}(...)")
            } else {
                format!("deps.{}.{action}(...)", call.dependency)
            };
            let fields = [
                ("path", s(called)),
                ("n", s(article(self.kind_name()))),
                ("kind", s(self.kind_name())),
            ];
            self.add("calls.not_in_kind", Some(&where_.node.id), &fields);
            return;
        }
        if call.dependency == "self" {
            if !self.kind.roots.contains("self") {
                self.not_in_kind(&where_.node.id, &format!("self.{action}(...)"), "self");
                return;
            }
        } else if !self.reads("deps") {
            self.not_in_kind(
                &where_.node.id,
                &format!("deps.{}.{action}(...)", call.dependency),
                "deps",
            );
            return;
        }
        if call.given.is_some() {
            self.check_with(call, where_);
        }
        let undeclared = self.undeclared(&call.dependency);
        let known = if undeclared {
            None
        } else {
            self.dependencies.get(call.dependency.as_str()).copied()
        };
        let Some(dependency) = known else {
            let declares = self.declares(&call.dependency);
            let unknown = if declares { undeclared } else { self.mounted };
            if call.dependency != "self" && unknown && declares {
                let shown = call.dependency.replace(REFERENCE_SEPARATOR, ".");
                let written = call
                    .dependency
                    .split(REFERENCE_SEPARATOR)
                    .next()
                    .unwrap_or_default()
                    .to_owned();
                self.unknown_dependency(&where_.node.id, &owner, &shown, &written);
            } else if call.dependency != "self" && unknown {
                let fields = [
                    ("path", s(owner)),
                    ("key", s(call.dependency.clone())),
                    ("keys", s(self.dependency_keys())),
                    (
                        "hint",
                        s(dependency_hint(&call.dependency, self.kind_name())),
                    ),
                ];
                self.add("deps.unknown", Some(&where_.node.id), &fields);
            }
            return;
        };
        if let Some(actions) = &dependency.action_dependencies {
            if !actions.iter().any(|a| a.key == action) {
                let fields = [
                    ("path", s(owner)),
                    ("key", s(call.dependency.clone())),
                    ("action", s(action)),
                    (
                        "actions",
                        s(sorted_keys(actions.iter().map(|a| a.key.as_str()))),
                    ),
                ];
                self.add("deps.unknown_action", Some(&where_.node.id), &fields);
                return;
            }
        }
        let demand = dependency
            .action_dependencies
            .iter()
            .flatten()
            .find(|a| a.key == action)
            .and_then(|a| a.demand.as_ref());
        if let Some(matches) = demand
            .and_then(|d| d.arg_matches.as_deref())
            .filter(|m| !m.is_empty())
        {
            self.check_arg_kinds(call, matches, where_, scope);
        }
    }

    /// A local passed whole to a task call fits the action's arg port (`arg_kinds`).
    fn check_arg_kinds(
        &mut self,
        call: &AgentProbeInputModel,
        matches: &[PortMatchInputModel],
        where_: &Where<'_>,
        scope: &Scope,
    ) {
        let name = if call.dependency == "self" {
            format!("self.{}", call.operation)
        } else {
            format!("deps.{}.{}", call.dependency, call.operation)
        };
        for argument in call.arguments.iter().flatten() {
            let Some(path) = argument.value_path.as_deref() else {
                continue;
            };
            let argument_key = argument.key.as_deref().unwrap_or_default();
            // The last match of a key wins, as a dict built from them keeps it.
            let Some(port_match) = matches
                .iter()
                .rev()
                .find(|m| m.key.as_deref() == Some(argument_key))
            else {
                continue;
            };
            let Some(match_kind) = port_match.kind else {
                continue;
            };
            let root = path.split('.').next().unwrap_or_default();
            let Some(Bound::Local { local, .. }) = scope.get(root) else {
                continue;
            };
            let Some(port) = Self::local_port(local, path) else {
                continue;
            };
            let kind = port.kind.value();
            let port_kind = match_kind.value();
            let other_structure = kind == "STRUCTURE"
                && port_kind == "STRUCTURE"
                && port.identifier.is_some()
                && port_match.identifier.is_some()
                && port.identifier != port_match.identifier;
            let refused = self
                .vocabulary
                .arg_kinds
                .get(kind)
                .is_some_and(|allowed| !allowed.contains(port_kind));
            if refused || other_structure {
                let (shown, local_kind) = if other_structure {
                    (
                        format!(
                            "{port_kind} {}",
                            port_match.identifier.as_deref().unwrap_or_default()
                        ),
                        format!("{kind} {}", port.identifier.as_deref().unwrap_or_default()),
                    )
                } else {
                    (port_kind.to_owned(), kind.to_owned())
                };
                let fields = [
                    ("call", s(name.clone())),
                    (
                        "argument",
                        argument.key.clone().map_or(Value::Null, Value::String),
                    ),
                    ("port_kind", s(shown)),
                    ("path", s(path)),
                    ("kind", s(local_kind)),
                ];
                self.add("local.arg_kind", Some(&where_.node.id), &fields);
            }
        }
    }

    fn dependency_keys(&self) -> String {
        sorted_keys(self.dependencies.keys().copied())
    }

    fn visit_util_call(&mut self, call: &UtilCallInputModel, where_: &Where<'_>, top: bool) {
        let Some(function) = self.vocabulary.function(&call.operation).cloned() else {
            let namespace = call.operation.split('.').next().unwrap_or_default();
            if !self
                .vocabulary
                .function_namespaces
                .iter()
                .any(|n| n == namespace)
                || !call.operation.contains('.')
            {
                self.add(
                    "expr.unknown_callee",
                    Some(&where_.node.id),
                    &[("callee", s(call.operation.clone()))],
                );
            }
            // A function of a namespace this vocabulary does not list may be a catalog's: not the server's to say.
            return;
        };
        if function.effect && !top {
            self.add(
                "callback.nested",
                Some(&where_.node.id),
                &[("what", s(call.operation.clone()))],
            );
        }
        if top {
            if let Some(verb) = query_verb(&call.operation) {
                self.check_query_effect(call, verb, where_);
            }
        }
        if call.operation == PICK_FUNCTION {
            self.check_pick(call, where_);
        }
        let keys: HashSet<&str> = call
            .arguments
            .iter()
            .flatten()
            .filter_map(|a| a.key.as_deref())
            .collect();
        let known = function.keys();
        let mut unknown: Vec<&str> = keys
            .iter()
            .copied()
            .filter(|key| {
                !known.contains(key) && (key.is_empty() || !key.chars().all(|c| c.is_ascii_digit()))
            })
            .collect();
        unknown.sort_unstable();
        if let Some(first) = unknown.first() {
            let detail = format!(
                "has no argument {} ({})",
                crate::pyjson::repr_str(first),
                known.join(", ")
            );
            self.add(
                "expr.arguments",
                Some(&where_.node.id),
                &[
                    ("function", s(call.operation.clone())),
                    ("detail", s(detail)),
                ],
            );
        }
        let missing: Vec<&str> = function
            .required_keys()
            .into_iter()
            .filter(|key| !keys.contains(key))
            .collect();
        if !missing.is_empty() {
            let detail = format!("needs {}", missing.join(", "));
            self.add(
                "expr.arguments",
                Some(&where_.node.id),
                &[
                    ("function", s(call.operation.clone())),
                    ("detail", s(detail)),
                ],
            );
        }
    }

    /// `query.refetch` / `query.fetch_more`: a declared query, named by a literal; fetch_more
    /// only of one that declares `more`.
    fn check_query_effect(&mut self, call: &UtilCallInputModel, verb: &str, where_: &Where<'_>) {
        let Some(named) = call
            .arguments
            .iter()
            .flatten()
            .find(|a| a.key.as_deref() == Some("query"))
        else {
            return; // expr.arguments says it is missing
        };
        let literal = match &named.value_literal {
            Some(Value::String(name))
                if named.value_path.is_none()
                    && named.util_call.is_none()
                    && named.agent_call.is_none()
                    && named.value_list.is_none()
                    && named.value_dict.is_none() =>
            {
                Some(name.clone())
            }
            _ => None,
        };
        let Some(name) = literal else {
            let fields = [
                ("function", s(call.operation.clone())),
                ("verb", s(verb)),
                ("what", s(argument_what(named))),
            ];
            self.add("query.effect_target", Some(&where_.node.id), &fields);
            return;
        };
        let path = format!("queries.{name}.{verb}()");
        if !self.kind.roots.contains("queries") {
            self.not_in_kind(&where_.node.id, &path, "queries");
            return;
        }
        let Some(query) = self.query(&name).cloned() else {
            let fields = [
                ("path", s(path)),
                ("name", s(name)),
                (
                    "names",
                    s(sorted_keys(self.queries.iter().map(|(k, _)| k.as_str()))),
                ),
            ];
            self.add("query.unknown", Some(&where_.node.id), &fields);
            return;
        };
        if call.operation == FETCH_MORE_FUNCTION && query.more.is_none() {
            self.add(
                "query.fetch_more",
                Some(&where_.node.id),
                &[("path", s(path)), ("name", s(name))],
            );
        }
    }

    fn bind_let(
        &mut self,
        name: &str,
        items: Option<&ComponentPropInputModel>,
        where_: &Where<'_>,
        scope: &Scope,
    ) -> Shape {
        if self.vocabulary.is_reserved(name) {
            self.add(
                "roots.reserved",
                Some(&where_.node.id),
                &[("name", s(name))],
            );
        } else if let Some(bound) = scope.get(name) {
            let fields = [
                ("name", s(name)),
                ("first", s(self.binding_what(name, bound))),
                (
                    "second",
                    s(format!("the let of <foreach> ({})", where_.node.id)),
                ),
            ];
            self.add("roots.shadow", Some(&where_.node.id), &fields);
        }
        let keyed = where_
            .node
            .props
            .iter()
            .flatten()
            .any(|p| p.key == FOREACH_KEY_PROP);
        if let Some(Value::Array(literal)) = items.and_then(|i| i.static_value.as_ref()) {
            if !keyed
                && literal
                    .iter()
                    .any(|item| item.is_object() || item.is_array())
            {
                self.add(
                    "foreach.position_key",
                    Some(&where_.node.id),
                    &[("let", s(name))],
                );
            }
        }
        let Some(path) = items
            .and_then(|i| i.dynamic_value.as_ref())
            .and_then(|d| d.path.as_deref())
        else {
            return Shape::open(Some(name));
        };
        let resolved = self.resolve(path, None, scope, &where_.text());
        let Some(resolved) = resolved.filter(|r| !r.open) else {
            return Shape::open(Some(name));
        };
        if let Some([child]) = resolved.children.as_deref() {
            if child.key.as_deref() == Some(ITEM) {
                let compound = child.children.as_ref().is_some_and(|c| !c.is_empty())
                    || child.kind.is_some_and(|k| COMPOUND_KINDS.contains(&k));
                if !keyed && compound {
                    self.add(
                        "foreach.position_key",
                        Some(&where_.node.id),
                        &[("let", s(name))],
                    );
                }
                return child.clone().with_key(name);
            }
        }
        if matches!(resolved.kind, None | Some(PortKind::LIST)) {
            return Shape::open(Some(name));
        }
        let kind = resolved
            .kind
            .map(|k| k.value().to_lowercase())
            .unwrap_or_default();
        let fields = [
            ("path", s(path)),
            (
                "detail",
                s(format!(
                    "{FOREACH_ITEMS_PROP} of a foreach reads a list, not this {kind}"
                )),
            ),
        ];
        self.add("path.unknown", Some(&where_.node.id), &fields);
        Shape::open(Some(name))
    }

    /// What a `foreach`'s `index` is bound to: the item's 0-based position. `scope` holds the
    /// loop variable already.
    fn bind_index(&mut self, name: &str, where_: &Where<'_>, scope: &Scope) -> Shape {
        if self.vocabulary.is_reserved(name) {
            self.add(
                "roots.reserved",
                Some(&where_.node.id),
                &[("name", s(name))],
            );
        } else if let Some(bound) = scope.get(name) {
            let fields = [
                ("name", s(name)),
                ("first", s(self.binding_what(name, bound))),
                (
                    "second",
                    s(format!("the index of <foreach> ({})", where_.node.id)),
                ),
            ];
            self.add("roots.shadow", Some(&where_.node.id), &fields);
        }
        Shape::typed(name, Some(PortKind::INT))
    }

    /// What a `<local>` named `name` (the node `node`) is bound to in its parent's subtree;
    /// `scope` is what is in scope where it is declared.
    fn bind_local(&mut self, name: &str, node: &ComponentNodeInputModel, scope: &Scope) -> Bound {
        if !self.vocabulary.is_reserved(name) {
            if let Some(bound) = scope.get(name) {
                let fields = [
                    ("name", s(name)),
                    ("first", s(self.binding_what(name, bound))),
                    ("second", s(format!("the local {}", node.id))),
                ];
                self.add("roots.shadow", Some(&node.id), &fields);
            }
        }
        match self.resolved_local(&node.id) {
            Some(resolved) => Bound::Local {
                node: node.id.clone(),
                local: Box::new(resolved.local.clone()),
            },
            None => Bound::Shape(Shape::open(Some(name))),
        }
    }

    /// What a name in scope is, for a message.
    fn binding_what(&self, name: &str, bound: &Bound) -> String {
        match bound {
            Bound::Local { node, .. } => format!("the local {node}"),
            Bound::Shape(_) if self.into.contains(name) => "a call's record".to_owned(),
            Bound::Shape(_) => "a let".to_owned(),
        }
    }

    fn bind_into(&self, name: &str, call: &AgentProbeInputModel) -> Shape {
        let action = action_key_for(call);
        let returns = self
            .dependencies
            .get(call.dependency.as_str())
            .and_then(|d| d.action_dependencies.as_ref())
            .and_then(|actions| {
                actions
                    .iter()
                    .rev()
                    .find(|a| a.key == action && a.demand.is_some())
                    .and_then(|a| a.demand.as_ref())
                    .and_then(|d| d.return_matches.as_deref())
            });
        task_record_shape(name, returns)
    }

    // -- paths

    /// What `path` holds, recording a finding (and returning none) when it holds nothing.
    fn resolve(
        &mut self,
        path: &str,
        where_: Option<&Where<'_>>,
        scope: &Scope,
        owner: &str,
    ) -> Resolved {
        let vocabulary = self.vocabulary;
        let node_id = where_.map_or_else(|| owner.to_owned(), |w| w.node.id.clone());
        let parts: Vec<&str> = path.split('.').collect();
        let (root, rest) = (parts[0], &parts[1..]);
        if let Some(bound) = scope.get(root) {
            let shape = match bound {
                Bound::Local { local, .. } => {
                    let Some(port) = local.arg_port().filter(|p| !OPAQUE_KINDS.contains(&p.kind))
                    else {
                        return Some(Shape::open(Some(root)));
                    };
                    return self.descend(
                        Some(Shape::of_port(port).with_key(root)),
                        rest,
                        path,
                        &node_id,
                    );
                }
                Bound::Shape(shape) => shape.clone(),
            };
            if self.into.contains(root) {
                self.check_returns(path, rest, &node_id);
            }
            return self.descend(Some(shape), rest, path, &node_id);
        }
        if root == "event" {
            let Some(where_) = where_.filter(|w| w.callback) else {
                self.add(
                    "roots.event_outside_callback",
                    Some(&node_id),
                    &[("path", s(path))],
                );
                return None;
            };
            return self.event_field(rest, where_);
        }
        if root == "error" {
            if !where_.is_some_and(|w| w.in_error) {
                self.add(
                    "roots.error_outside_boundary",
                    Some(&node_id),
                    &[("path", s(path))],
                );
                return None;
            }
            let shape = record_shape("error", &vocabulary.records["error"].fields, &[]);
            return self.descend(Some(shape), rest, path, &node_id);
        }
        if !vocabulary.data_roots.iter().any(|r| r == root) {
            let written = if vocabulary.function_namespaces.iter().any(|n| n == root) {
                format!("{root} (a function namespace)")
            } else {
                root.to_owned()
            };
            self.add(
                "roots.unknown",
                Some(&node_id),
                &[("path", s(path)), ("root", s(written))],
            );
            return None;
        }
        // `agents` is how a custom blok writes `deps`: a stored tree never holds it.
        if !self.reads(root) || root == AGENTS_ROOT {
            self.not_in_kind(&node_id, path, root);
            return None;
        }
        let whole = |checker: &mut Self, root: &str| {
            checker.add(
                "roots.whole",
                Some(&node_id),
                &[("path", s(path)), ("root", s(root))],
            );
            None
        };
        match root {
            "then" => {
                if !rest.is_empty() {
                    self.add(
                        "path.unknown",
                        Some(&node_id),
                        &[
                            ("path", s(path)),
                            (
                                "detail",
                                s("then is a moment (null while live), it has no fields"),
                            ),
                        ],
                    );
                }
                Some(Shape::typed("then", Some(PortKind::STRING)))
            }
            "self" => {
                let dependency = self.dependencies.get("self").copied();
                self.agent(dependency, "self", rest, path, &node_id)
            }
            "deps" => {
                let Some(key) = rest.first() else {
                    return whole(self, "deps");
                };
                // An agent blok's dependencies are in its own tree (`cells.microscope`, stored
                // `deps.cells__microscope`): one it does not declare is unknown, mounted or
                // not; one it declares and this side cannot type is open.
                if self.declares(key) {
                    if let Some((reference, OPTIONS_SEGMENT)) = key.split_once(REFERENCE_SEPARATOR)
                    {
                        if self.declaration(reference).is_some() {
                            return self.options(reference, &rest[1..], path, &node_id);
                        }
                    }
                }
                let undeclared = self.undeclared(key);
                let known = if undeclared {
                    None
                } else {
                    self.dependencies.get(key).copied()
                };
                let Some(dependency) = known else {
                    if !undeclared && (!self.mounted || self.declares(key)) {
                        return Some(Shape::open(Some(path)));
                    }
                    if self.declares(key) {
                        let (reference, shown) = match key.split_once(REFERENCE_SEPARATOR) {
                            Some((reference, name)) => (reference, format!("{reference}.{name}")),
                            None => (*key, (*key).to_owned()),
                        };
                        self.unknown_dependency(&node_id, path, &shown, reference);
                        return None;
                    }
                    let fields = [
                        ("path", s(path)),
                        ("key", s(*key)),
                        ("keys", s(self.dependency_keys())),
                        ("hint", s(dependency_hint(key, self.kind_name()))),
                    ];
                    self.add("deps.unknown", Some(&node_id), &fields);
                    return None;
                };
                self.agent(Some(dependency), key, &rest[1..], path, &node_id)
            }
            "args" => {
                let Some(key) = rest.first() else {
                    return whole(self, "args");
                };
                if let Some(args) = &self.scope.args {
                    if !args.iter().any(|a| a == key) {
                        let keys = if args.is_empty() {
                            "none".to_owned()
                        } else {
                            args.join(", ")
                        };
                        let fields = [("path", s(path)), ("key", s(*key)), ("keys", s(keys))];
                        self.add("args.unknown", Some(&node_id), &fields);
                        return None;
                    }
                }
                Some(Shape::open(Some(path)))
            }
            "task" => {
                self.check_returns(path, rest, &node_id);
                self.descend(Some(task_record_shape("task", None)), rest, path, &node_id)
            }
            "assign" => {
                // `assign.given.<key>` is open below `given`: one flag per argument of the action.
                if let [GIVEN_FIELD, key, ..] = rest {
                    if let Some(args) = &self.scope.args {
                        if !args.iter().any(|a| a == key) {
                            let keys = if args.is_empty() {
                                "none".to_owned()
                            } else {
                                args.join(", ")
                            };
                            let fields = [("path", s(path)), ("key", s(*key)), ("keys", s(keys))];
                            self.add("args.unknown", Some(&node_id), &fields);
                            return None;
                        }
                    }
                    return Some(Shape::typed(key, Some(PortKind::BOOL)));
                }
                let shape = record_shape("assign", &vocabulary.records["assign"].fields, &[]);
                self.descend(Some(shape), rest, path, &node_id)
            }
            PORT_ROOT => {
                // `port.value` is the port's own type where the mount says it; the rest is the record.
                let value = self.scope.port.as_ref().map(Shape::of_match);
                let overrides: Vec<(&str, Shape)> =
                    value.into_iter().map(|v| (PORT_VALUE_FIELD, v)).collect();
                let shape =
                    record_shape(PORT_ROOT, &vocabulary.records[PORT_ROOT].fields, &overrides);
                self.descend(Some(shape), rest, path, &node_id)
            }
            "viewer" => {
                let shape = record_shape("viewer", &vocabulary.records["viewer"].fields, &[]);
                self.descend(Some(shape), rest, path, &node_id)
            }
            "queries" => {
                let Some(name) = rest.first() else {
                    return whole(self, "queries");
                };
                if self.query(name).is_none() {
                    let fields = [
                        ("path", s(path)),
                        ("name", s(*name)),
                        (
                            "names",
                            s(sorted_keys(self.queries.iter().map(|(k, _)| k.as_str()))),
                        ),
                    ];
                    self.add("query.unknown", Some(&node_id), &fields);
                    return None;
                }
                let shape = record_shape(name, &vocabulary.records["query"].fields, &[]);
                self.descend(Some(shape), &rest[1..], path, &node_id)
            }
            "subscriptions" => {
                let Some(name) = rest.first() else {
                    return whole(self, "subscriptions");
                };
                if !self.subscriptions.iter().any(|(k, _)| k == name) {
                    let fields = [
                        ("path", s(path)),
                        ("name", s(*name)),
                        (
                            "names",
                            s(sorted_keys(
                                self.subscriptions.iter().map(|(k, _)| k.as_str()),
                            )),
                        ),
                    ];
                    self.add("subscription.unknown", Some(&node_id), &fields);
                    return None;
                }
                let shape = record_shape(name, &vocabulary.records["subscription"].fields, &[]);
                self.descend(Some(shape), &rest[1..], path, &node_id)
            }
            _ => Some(Shape::open(Some(path))),
        }
    }

    /// What a callback is called with is its component's to say: any field is taken as written.
    fn event_field(&mut self, rest: &[&str], _where: &Where<'_>) -> Resolved {
        Some(Shape::open(Some(rest.first().copied().unwrap_or("event"))))
    }

    /// What a path below an agent (`self`, `deps.<key>`) holds.
    /// `<record>.returns.return0`: a return the action left unnamed (a warning).
    fn check_returns(&mut self, path: &str, rest: &[&str], node_id: &str) {
        if let [RETURNS_FIELD, port, ..] = rest {
            let digits = port.strip_prefix("return").unwrap_or_default();
            if !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit()) {
                self.add(
                    "returns.unnamed",
                    Some(node_id),
                    &[("path", s(path)), ("port", s(*port))],
                );
            }
        }
    }

    fn agent(
        &mut self,
        dependency: Option<&AgentDependencyInputModel>,
        key: &str,
        rest: &[&str],
        path: &str,
        node_id: &str,
    ) -> Resolved {
        let vocabulary = self.vocabulary;
        let Some((head, tail)) = rest.split_first() else {
            return Some(Shape::open(Some(key)));
        };
        if vocabulary.agent_fields.iter().any(|f| f == head) {
            if !tail.is_empty() {
                let fields = [
                    ("path", s(path)),
                    ("detail", s(format!("{head} of an agent has no fields"))),
                ];
                self.add("path.unknown", Some(node_id), &fields);
            }
            let kind = if *head == CONNECTED_FIELD {
                PortKind::BOOL
            } else {
                PortKind::STRING
            };
            return Some(Shape::typed(head, Some(kind)));
        }
        if *head == vocabulary.tasks_segment {
            let Some((action, below)) = tail.split_first() else {
                self.add(
                    "roots.whole",
                    Some(node_id),
                    &[("path", s(path)), ("root", s(format!("{key}.tasks")))],
                );
                return None;
            };
            let mut returns = None;
            if let Some(actions) = dependency.and_then(|d| d.action_dependencies.as_ref()) {
                let Some(demand) = actions.iter().find(|a| a.key == *action) else {
                    let fields = [
                        ("path", s(path)),
                        ("key", s(key)),
                        ("action", s(*action)),
                        (
                            "actions",
                            s(sorted_keys(actions.iter().map(|a| a.key.as_str()))),
                        ),
                    ];
                    self.add("deps.unknown_action", Some(node_id), &fields);
                    return None;
                };
                returns = demand
                    .demand
                    .as_ref()
                    .and_then(|d| d.return_matches.as_deref());
            }
            self.check_returns(path, below, node_id);
            return self.descend(
                Some(task_record_shape(action, returns)),
                below,
                path,
                node_id,
            );
        }
        if *head == vocabulary.assets_segment {
            if tail.len() != 1 {
                self.add("deps.asset", Some(node_id), &[("path", s(path))]);
                return None;
            }
            if key == "self"
                && self
                    .scope
                    .assets
                    .as_ref()
                    .is_some_and(|assets| !assets.contains(tail[0]))
            {
                self.add("deps.asset", Some(node_id), &[("path", s(path))]);
                return None;
            }
            return Some(Shape::typed(tail[0], Some(PortKind::STRING)));
        }
        let Some(states) = dependency.and_then(|d| d.state_dependencies.as_ref()) else {
            return Some(Shape::open(Some(path)));
        };
        let Some(state) = states.iter().find(|d| d.key == *head) else {
            let fields = [
                ("path", s(path)),
                ("key", s(key)),
                ("state", s(*head)),
                (
                    "states",
                    s(sorted_keys(states.iter().map(|d| d.key.as_str()))),
                ),
            ];
            self.add("deps.unknown_state", Some(node_id), &fields);
            return None;
        };
        self.descend(Some(state_shape(state)), tail, path, node_id)
    }

    /// Follow `rest` down a shape's children.
    fn descend(&mut self, shape: Resolved, rest: &[&str], path: &str, node_id: &str) -> Resolved {
        let mut current = shape;
        for (index, part) in rest.iter().enumerate() {
            let shape = match &current {
                Some(shape) if !shape.open => shape,
                _ => return current,
            };
            // A structure reads below it as the reference it travels as.
            let structure;
            let shape = if shape.kind == Some(PortKind::STRUCTURE)
                && shape.children.as_deref().unwrap_or_default().is_empty()
            {
                structure = structure_shape(shape);
                &structure
            } else {
                shape
            };
            let children = shape.children.as_deref().unwrap_or_default();
            let mut child = children.iter().find(|c| c.key.as_deref() == Some(*part));
            if child.is_none() {
                if let [only] = children {
                    if only.key.as_deref() == Some(ITEM) {
                        child = Some(only);
                    }
                }
            }
            let Some(child) = child.cloned() else {
                let mut available: Vec<&str> = children
                    .iter()
                    .filter_map(|c| c.key.as_deref())
                    .filter(|k| *k != ITEM)
                    .collect();
                available.sort_unstable();
                let joined = rest[..index].join(".");
                let at = if !joined.is_empty() {
                    joined
                } else {
                    shape
                        .key
                        .clone()
                        .filter(|k| !k.is_empty())
                        .unwrap_or_else(|| "it".into())
                };
                let detail = format!(
                    "{} does not exist in {at}{}",
                    crate::pyjson::repr_str(part),
                    if available.is_empty() {
                        " (it has no fields)".to_owned()
                    } else {
                        format!(" (it has: {})", available.join(", "))
                    }
                );
                self.add(
                    "path.unknown",
                    Some(node_id),
                    &[("path", s(path)), ("detail", s(detail))],
                );
                return None;
            };
            current = Some(child);
        }
        current
    }
}

fn sorted_keys<'k>(keys: impl Iterator<Item = &'k str>) -> String {
    let mut keys: Vec<&str> = keys.collect();
    keys.sort_unstable();
    if keys.is_empty() {
        "none".into()
    } else {
        keys.join(", ")
    }
}

/// A logo is an icon: few nodes, shallow, little path data, and loops over few items.
pub fn check_logo(
    tree: &ComponentNodeInputModel,
    demo: Option<&BlokDemoInputModel>,
) -> Vec<Finding> {
    let limits = &vocabulary().limits;
    fn visit(
        current: &ComponentNodeInputModel,
        depth: usize,
        demo: Option<&BlokDemoInputModel>,
        state: &mut (usize, usize, Vec<Finding>),
    ) {
        let limits = &vocabulary().limits;
        state.0 += 1;
        if depth > limits.logo_max_depth {
            let detail = format!("a logo nests at most {} deep", limits.logo_max_depth);
            state.2.push(Finding::of(
                "logo.complexity",
                Some(&current.id),
                &[("detail", s(detail))],
            ));
        }
        for prop in current.props.iter().flatten() {
            if limits.logo_path_data_props.contains(&prop.key) {
                if let Some(Value::String(data)) = &prop.static_value {
                    state.1 += data.chars().count();
                }
            }
            if current.component == FOREACH_COMPONENT && prop.key == FOREACH_ITEMS_PROP {
                let items = demo_items(prop, demo);
                if items > limits.logo_max_foreach_items {
                    let detail = format!(
                        "a foreach in a logo draws at most {} items, its demo has {items}",
                        limits.logo_max_foreach_items
                    );
                    state.2.push(Finding::of(
                        "logo.complexity",
                        Some(&current.id),
                        &[("detail", s(detail))],
                    ));
                }
            }
        }
        for child in current.children.iter().flatten() {
            visit(child, depth + 1, demo, state);
        }
    }
    let mut state = (0, 0, vec![]);
    visit(tree, 1, demo, &mut state);
    let (nodes, path_data, mut findings) = state;
    if nodes > limits.logo_max_nodes {
        let detail = format!(
            "a logo has at most {} components, this one {nodes}",
            limits.logo_max_nodes
        );
        findings.push(Finding::of(
            "logo.complexity",
            Some(&tree.id),
            &[("detail", s(detail))],
        ));
    }
    if path_data > limits.logo_max_path_data {
        let detail = format!(
            "a logo holds at most {} characters of path data, this one {path_data}",
            limits.logo_max_path_data
        );
        findings.push(Finding::of(
            "logo.complexity",
            Some(&tree.id),
            &[("detail", s(detail))],
        ));
    }
    findings
}

fn demo_items(prop: &ComponentPropInputModel, demo: Option<&BlokDemoInputModel>) -> usize {
    if let Some(Value::Array(items)) = &prop.static_value {
        return items.len();
    }
    let (Some(path), Some(demo)) = (
        prop.dynamic_value.as_ref().and_then(|d| d.path.as_deref()),
        demo,
    ) else {
        return 0;
    };
    let parts: Vec<&str> = path.split('.').collect();
    let (mut value, rest): (Option<&Value>, &[&str]) = match parts[0] {
        "self" => (demo.deps.as_ref().and_then(|d| d.get("self")), &parts[1..]),
        "deps" => (demo.deps.as_ref(), &parts[1..]),
        "task" => (demo.task.as_ref(), &parts[1..]),
        "args" => (demo.args.as_ref(), &parts[1..]),
        _ => (None, &parts[1..]),
    };
    for part in rest {
        value = value.and_then(|v| v.as_object()).and_then(|o| o.get(*part));
    }
    value.and_then(Value::as_array).map_or(0, Vec::len)
}

/// Every finding of `tree` mounted as `scope` says, and its `<local>` declarations resolved (by
/// node id, in tree order): errors and warnings, in tree order (`check_blok_tree`). A root that
/// is no readable `<Blok>` is the one finding, and resolves nothing. The language alone: no
/// catalog is read, so what a component takes, where it may sit and which catalog it is of is
/// left to whoever draws the blok.
pub fn check_blok(
    tree: &ComponentNodeInputModel,
    scope: &BlokScope,
) -> (Vec<Finding>, Vec<(String, ResolvedLocal)>) {
    if let Some(problem) = header_problem(tree) {
        return (vec![problem], vec![]);
    }
    let header = blok_header(tree).expect("a readable header");
    let bound = bound_scope(tree, scope);
    let scope = &bound;
    let resolved = resolve_locals(tree, scope.dependencies.as_deref(), &scope.arg_ports);
    let vocabulary = vocabulary();
    let kind = vocabulary
        .kind(&header.kind)
        .expect("the header names a kind")
        .drawn_in(&header.context)
        .clone();
    let chromeless = (header.chrome.as_deref() == Some(CHROME_NONE)).then(|| header.node.clone());
    let mut checker = Checker {
        scope,
        header,
        vocabulary,
        kind,
        findings: vec![],
        mounted: scope.is_mounted(),
        dependencies: scope
            .dependencies
            .iter()
            .flatten()
            .map(|d| (d.key.as_str(), d))
            .collect(),
        resolved,
        queries: vec![],
        subscriptions: vec![],
        bound_locals: HashSet::new(),
        into: HashSet::new(),
        asks: false,
        chromeless,
        declared: vec![],
    };
    checker.check_declarations(tree);
    let mut root_scope = Scope::new();
    for (name, sites) in into_sites(tree) {
        root_scope.insert(
            name.clone(),
            Bound::Shape(checker.bind_into(&name, sites[0].0)),
        );
    }
    checker.walk_node(tree, &root_scope, &mut vec![], false);
    if checker.header.context == LOGO {
        let demo = scope.demo.as_ref();
        checker.findings.extend(check_logo(tree, demo));
    } else if let Some(logo) = blok_logo_tree(tree) {
        // The blok's own <logo>, checked as the logo blok it stands for: the pens, the logo
        // limits.
        checker
            .findings
            .extend(check_blok_tree(&logo, &BlokScope::default()));
    }
    checker.finish();
    (checker.findings, checker.resolved)
}

/// Every finding of `tree` mounted as `scope` says (`check_blok_tree`): see [`check_blok`].
pub fn check_blok_tree(tree: &ComponentNodeInputModel, scope: &BlokScope) -> Vec<Finding> {
    check_blok(tree, scope).0
}

/// What a blok of `kind` that reads an undeclared `deps.<key>` does about it:
/// - custom and task bloks declare it, to copy as it is: `dependencies=[AgentDependencyInput(key="laser")]`;
/// - an action blok's dependencies are its action's: ``add a dependency parameter `laser` to the action``;
/// - an agent blok declares it in its own tree: `<dependencies key="laser" like="self.<action>" />`.
pub fn dependency_hint(key: &str, kind: &str) -> String {
    match kind {
        "action" | "assign" | "port" => {
            format!("add a dependency parameter `{key}` to the action")
        }
        "agent" => format!(
            "declare it in the blok: <dependencies key=\"{key}\" like=\"self.<action>\" />, then read {key}.<dependency>"
        ),
        _ => format!("dependencies=[AgentDependencyInput(key=\"{key}\")]"),
    }
}

/// How a string on a typed prop is written as the literal it meant (`fullWidth={true}`).
pub fn literal_hint(key: &str, value: &str, kind: &str) -> String {
    let written = value.trim();
    let literal = if kind == "BOOL" {
        let lower = written.to_lowercase();
        if lower == "true" || lower == "false" {
            lower
        } else {
            "true".to_owned()
        }
    } else {
        let number = python_float(written).unwrap_or(0.0);
        if kind == "INT" || (number.fract() == 0.0 && number.is_finite() && !written.contains('.'))
        {
            format!("{}", number.trunc() as i64)
        } else {
            float_repr(number)
        }
    };
    format!("{key}={{{literal}}}")
}

/// Python's `float(text)` for what a prop literal may hold: decimal numbers, exponents,
/// `inf` and `nan`, underscores between digits.
fn python_float(text: &str) -> Option<f64> {
    let cleaned = text.replace('_', "");
    let lower = cleaned.to_lowercase();
    let unsigned = lower.trim_start_matches(['+', '-']);
    if matches!(unsigned, "inf" | "infinity" | "nan") {
        return lower.parse::<f64>().ok();
    }
    if text.contains("__") || text.starts_with('_') || text.ends_with('_') {
        return None;
    }
    cleaned.parse::<f64>().ok()
}

/// Python's `repr(float)`: the shortest round-tripping digits, `.0` for an integral value,
/// exponent notation from 1e16 (and below 1e-4).
fn float_repr(number: f64) -> String {
    if number.is_nan() {
        return "nan".into();
    }
    if number.is_infinite() {
        return if number > 0.0 {
            "inf".into()
        } else {
            "-inf".into()
        };
    }
    crate::pyjson::repr(&Value::from(number))
}

/// The first error of [`check_blok_tree`], or its warnings.
pub fn validate_blok(
    tree: &ComponentNodeInputModel,
    scope: &BlokScope,
) -> Result<Vec<Finding>, Finding> {
    let findings = check_blok_tree(tree, scope);
    match findings.iter().find(|f| f.is_error()) {
        Some(error) => Err(error.clone()),
        None => Ok(findings),
    }
}

/// A class at its variant colons, those inside brackets left alone (`[&>*]:p-2`).
fn split_class(token: &str) -> Vec<String> {
    let mut parts = vec![];
    let mut depth = 0usize;
    let mut current = String::new();
    for character in token.chars() {
        match character {
            '[' => depth += 1,
            ']' => depth = depth.saturating_sub(1),
            _ => {}
        }
        if character == ':' && depth == 0 {
            parts.push(std::mem::take(&mut current));
        } else {
            current.push(character);
        }
    }
    parts.push(current);
    parts
}

/// What of the class `token` is outside the portable subset (`the before variant`), or none.
pub fn unportable_class(token: &str, vocabulary: &Vocabulary) -> Option<String> {
    let mut parts = split_class(token);
    let utility = parts.pop().unwrap_or_default();
    for variant in &parts {
        if variant.starts_with('[') {
            return Some("an arbitrary variant".to_owned());
        }
        let name = variant.split('/').next().unwrap_or_default();
        let stem = name.split("-[").next().unwrap_or_default();
        if vocabulary.unportable_variants.contains(name)
            || vocabulary.unportable_variants.contains(stem)
        {
            return Some(format!("the {name} variant"));
        }
    }
    let utility = utility.trim_start_matches(['!', '-']);
    if utility.starts_with('[') {
        return Some("an arbitrary property".to_owned());
    }
    if utility.contains('[') {
        let prefix = utility.split("-[").next().unwrap_or_default();
        return (!vocabulary.arbitrary_values.contains(prefix))
            .then(|| format!("an arbitrary value of {prefix}"));
    }
    vocabulary
        .unportable_utilities
        .iter()
        .find(|prefix| utility == prefix.as_str() || utility.starts_with(&format!("{prefix}-")))
        .map(|prefix| format!("the {prefix} utility"))
}
