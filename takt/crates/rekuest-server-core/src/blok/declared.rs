//! What a blok says of itself in its tree: its `<Blok>` header and its declarations (the Rust
//! twin of arkitekt-spec's `blok/declared.py`).
//!
//! Every blok is one `<Blok kind=... context=...>` element (`vocabulary.json` `blok`):
//! - the header ([`blok_header`]): its kind, its context, the catalog it is written for and the
//!   host's chrome, all literals on the root;
//! - its declarations, each a node of its own that draws nothing: `<local>` anywhere (first among
//!   its parent's children, in scope in its parent's subtree), `<query>` and `<subscription>` as
//!   children of the `<Blok>`, each with `<var>` children for its variables.
//!
//! Nothing here checks a rule beyond what makes a declaration readable at all; the checker
//! (`blok::check`) says the rest.

use serde_json::{Map, Value};

use super::expr::s;
use super::vocabulary::{vocabulary, Finding};
use crate::inputs::{
    blok_error, ActionArgumentInputModel, BlokLocalInputModel, BlokQueryInputModel,
    BlokSubscriptionInputModel, ComponentNodeInputModel, ComponentPropInputModel,
};
use crate::pyjson::repr;

pub const BLOK_COMPONENT: &str = "Blok";
pub const LOCAL_COMPONENT: &str = "local";
pub const QUERY_COMPONENT: &str = "query";
pub const SUBSCRIPTION_COMPONENT: &str = "subscription";
pub const VAR_COMPONENT: &str = "var";
pub const DEPENDENCIES_COMPONENT: &str = "dependencies";
/// A blok's own logo: a child of its `<Blok>` holding a drawing of the logo catalog.
pub const LOGO_COMPONENT: &str = "logo";
/// The components that declare what a blok owns and draw nothing.
pub const DECLARATION_COMPONENTS: [&str; 6] = [
    LOCAL_COMPONENT,
    QUERY_COMPONENT,
    SUBSCRIPTION_COMPONENT,
    VAR_COMPONENT,
    DEPENDENCIES_COMPONENT,
    LOGO_COMPONENT,
];
const LOGO_TITLE_PROP: &str = "title";
const LOGO_VIEW_BOX_PROP: &str = "viewBox";
/// A logo's grid when its `<logo>` names none: 24 units, the grid of the action logos.
pub const LOGO_VIEW_BOX: &str = "0 0 24 24";
/// The kind a `<logo>` is drawn as ([`blok_logo_tree`]): one drawn in the logo context.
pub const LOGO_KIND: &str = "agent";
/// The pen a `<logo>` draws with unless a shape says otherwise: the action logos' pen.
pub const LOGO_PEN: [(&str, &str); 5] = [
    ("fill", "none"),
    ("stroke", "ink"),
    ("strokeWidth", "1.8"),
    ("strokeLinecap", "round"),
    ("strokeLinejoin", "round"),
];
/// The name a local, a query or a subscription is read by.
pub const NAME_PROP: &str = "name";
/// A local's borrowed port: `self.<action>.<arg>` or `deps.<key>.<action>.<arg>`.
pub const LIKE_PROP: &str = "like";
pub const KEY_PROP: &str = "key";
pub const VALUE_PROP: &str = "value";
pub const KIND_ATTRIBUTE: &str = "kind";
pub const CONTEXT_ATTRIBUTE: &str = "context";
pub const CATALOG_ATTRIBUTE: &str = "catalog";
pub const CHROME_ATTRIBUTE: &str = "chrome";
/// What a `<local>` may say beside its name: a port's fields and `persist`
/// (`BlokLocalInput.model_fields` without `key`).
pub const LOCAL_FIELDS: [&str; 16] = [
    "children",
    "choices",
    "default",
    "description",
    "dimension",
    "effects",
    "identifier",
    "kind",
    "label",
    "nullable",
    "persist",
    "proposed_units",
    "reference_unit",
    "requires",
    "validators",
    "widget",
];
/// The props of a query or a subscription that must be literal strings.
const DECLARATION_TEXT: [&str; 3] = [NAME_PROP, "service", "document"];

/// What a `<Blok>` says of itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlokHeader {
    /// `agent`, `custom`, `task`, `action` or `assign`.
    pub kind: String,
    /// `PANEL`, `SCENE` or `LOGO` (a `ComponentContext` value).
    pub context: String,
    pub catalog: Option<String>,
    pub chrome: Option<String>,
    /// The root's id.
    pub node: String,
}

fn find_prop<'t>(
    node: &'t ComponentNodeInputModel,
    key: &str,
) -> Option<&'t ComponentPropInputModel> {
    node.props.iter().flatten().find(|prop| prop.key == key)
}

/// Whether a prop is a literal: no path, no call.
pub fn is_literal(prop: &ComponentPropInputModel) -> bool {
    prop.dynamic_value.is_none() && prop.util_call.is_none() && prop.agent_call.is_none()
}

fn literal_text<'t>(node: &'t ComponentNodeInputModel, key: &str) -> Option<&'t str> {
    find_prop(node, key)
        .and_then(|prop| prop.static_value.as_ref())
        .and_then(Value::as_str)
}

/// Why `tree` is no readable `<Blok>`, or none: `blok.root` or `blok.attribute`.
pub fn header_problem(tree: &ComponentNodeInputModel) -> Option<Finding> {
    let vocabulary = vocabulary();
    if tree.component != vocabulary.blok.component {
        return Some(Finding::of(
            "blok.root",
            Some(&tree.id),
            &[("component", s(tree.component.clone()))],
        ));
    }
    let kinds = vocabulary
        .kinds
        .keys()
        .cloned()
        .collect::<Vec<_>>()
        .join("|");
    let attribute = |problem: String| {
        Some(Finding::of(
            "blok.attribute",
            Some(&tree.id),
            &[("problem", s(problem)), ("kinds", s(kinds.clone()))],
        ))
    };
    for key in [KIND_ATTRIBUTE, CONTEXT_ATTRIBUTE] {
        let Some(prop) = find_prop(tree, key) else {
            return attribute(format!("needs {key}=\"...\""));
        };
        if !is_literal(prop) || !prop.static_value.as_ref().is_some_and(Value::is_string) {
            return attribute(format!("takes {key} as a literal string"));
        }
    }
    let kind = literal_text(tree, KIND_ATTRIBUTE).unwrap_or_default();
    if !vocabulary.kinds.contains_key(kind) {
        return attribute(format!("has no kind {}", repr(&s(kind))));
    }
    let context = literal_text(tree, CONTEXT_ATTRIBUTE).unwrap_or_default();
    if !vocabulary.blok.contexts.contains_key(context) {
        let spelled: Vec<&str> = vocabulary
            .blok
            .contexts
            .keys()
            .map(String::as_str)
            .collect();
        return attribute(format!(
            "has no context {} ({})",
            repr(&s(context)),
            spelled.join(", ")
        ));
    }
    for key in [CATALOG_ATTRIBUTE, CHROME_ATTRIBUTE] {
        if let Some(prop) = find_prop(tree, key) {
            if !is_literal(prop) || !prop.static_value.as_ref().is_some_and(Value::is_string) {
                return attribute(format!("takes {key} as a literal string"));
            }
        }
    }
    None
}

/// The header of a blok: what its `<Blok>` says; the finding when it is no readable `<Blok>`.
pub fn blok_header(tree: &ComponentNodeInputModel) -> Result<BlokHeader, Finding> {
    if let Some(problem) = header_problem(tree) {
        return Err(problem);
    }
    let spelled = literal_text(tree, CONTEXT_ATTRIBUTE).unwrap_or_default();
    Ok(BlokHeader {
        kind: literal_text(tree, KIND_ATTRIBUTE)
            .unwrap_or_default()
            .to_owned(),
        context: vocabulary().blok.contexts[spelled].clone(),
        catalog: literal_text(tree, CATALOG_ATTRIBUTE).map(str::to_owned),
        chrome: literal_text(tree, CHROME_ATTRIBUTE).map(str::to_owned),
        node: tree.id.clone(),
    })
}

/// The catalog a blok's `<Blok>` names, none for the base catalog (and for no `<Blok>`).
pub fn blok_catalog(tree: &ComponentNodeInputModel) -> Option<&str> {
    if tree.component != BLOK_COMPONENT {
        return None;
    }
    literal_text(tree, CATALOG_ATTRIBUTE)
}

/// Whether `node` declares something (a local, a query, ...) rather than drawing.
pub fn is_declaration(node: &ComponentNodeInputModel) -> bool {
    DECLARATION_COMPONENTS.contains(&node.component.as_str())
}

/// The children of `node` that draw: its declarations left out.
pub fn content(node: &ComponentNodeInputModel) -> Vec<&ComponentNodeInputModel> {
    node.children
        .iter()
        .flatten()
        .filter(|child| !is_declaration(child))
        .collect()
}

/// The name a `<local>` declares, or none when it has none.
pub fn local_name(node: &ComponentNodeInputModel) -> Option<&str> {
    literal_text(node, NAME_PROP)
}

/// The argument a `<local>` borrows its port from (`self.move.to`), or none.
pub fn local_like(node: &ComponentNodeInputModel) -> Option<&str> {
    literal_text(node, LIKE_PROP)
}

fn local_attribute(
    node: &ComponentNodeInputModel,
    name: &str,
    key: &str,
    problem: String,
) -> Finding {
    Finding::of(
        "local.attribute",
        Some(&node.id),
        &[("name", s(name)), ("key", s(key)), ("problem", s(problem))],
    )
}

/// Why a `<local>` declares nothing readable, or none: `local.attribute`.
pub fn local_problem(node: &ComponentNodeInputModel) -> Option<Finding> {
    let Some(name) = local_name(node) else {
        return Some(local_attribute(
            node,
            "?",
            NAME_PROP,
            "is missing: write name=\"x\"".into(),
        ));
    };
    for prop in node.props.iter().flatten() {
        if prop.key == NAME_PROP || prop.key == LIKE_PROP {
            continue;
        }
        if !LOCAL_FIELDS.contains(&prop.key.as_str()) {
            return Some(local_attribute(
                node,
                name,
                &prop.key,
                format!("is no field of a local ({}, like)", LOCAL_FIELDS.join(", ")),
            ));
        }
        if !is_literal(prop) {
            return Some(local_attribute(
                node,
                name,
                &prop.key,
                "is not a literal".into(),
            ));
        }
    }
    if node.children.as_ref().is_some_and(|c| !c.is_empty()) {
        return Some(local_attribute(
            node,
            name,
            "children",
            "are not elements: a local holds nothing".into(),
        ));
    }
    if let Err(error) = declared_local(node) {
        return Some(local_attribute(
            node,
            name,
            "its port",
            format!("is no valid port ({error})"),
        ));
    }
    None
}

/// A `<local>` as the port it declares, its kind left out unless it gives one (`like` is
/// resolved where the blok is mounted). Why it is no valid local, when it is none.
pub fn declared_local(node: &ComponentNodeInputModel) -> Result<BlokLocalInputModel, String> {
    let Some(name) = local_name(node) else {
        return Err("its name is missing: write name=\"x\"".into());
    };
    let mut fields = Map::new();
    fields.insert("key".into(), s(name));
    for prop in node.props.iter().flatten() {
        if prop.key == NAME_PROP
            || prop.key == LIKE_PROP
            || !LOCAL_FIELDS.contains(&prop.key.as_str())
        {
            continue;
        }
        if let Some(value) = &prop.static_value {
            fields.insert(prop.key.clone(), value.clone());
        }
    }
    // Worded as pydantic words a model validator's refusal, inside the spec's `BlokError`.
    let refused = |problem: String| {
        blok_error(
            "local.attribute",
            &[
                ("name", s(name)),
                ("key", s("its port")),
                ("problem", s(problem)),
            ],
        )
        .0
    };
    let mut local: BlokLocalInputModel = serde_json::from_value(Value::Object(fields))
        .map_err(|error| refused(format!("is no valid port: {error}")))?;
    local
        .validate()
        .map_err(|error| refused(format!("is no valid port: Value error, {error}")))?;
    Ok(local)
}

/// A `<dependencies key="cells" like="self.<action>" />` of an agent blok: the dependencies of
/// one of the app's own actions, each read and called as `cells.<dependency>` and handed to a
/// run of that action with `with cells`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeclaredDependencies {
    pub key: String,
    pub node: String,
    /// As written; none when it is missing or no literal.
    pub like: Option<String>,
}

impl DeclaredDependencies {
    /// Whose action it names: `("self", action)` for `self.<action>`, `(key, action)` for a
    /// custom blok's `agents.<key>.<action>` (stored `deps.<key>.<action>`).
    pub fn target(&self) -> Option<(&str, &str)> {
        let parts: Vec<&str> = self.like.as_deref()?.split('.').collect();
        match parts[..] {
            ["self", action] if !action.is_empty() => Some(("self", action)),
            ["deps", key, action] if !key.is_empty() && key != "self" && !action.is_empty() => {
                Some((key, action))
            }
            _ => None,
        }
    }

    /// The dependency whose action it names: `self`, or a custom blok's bound agent.
    pub fn owner(&self) -> Option<&str> {
        self.target().map(|(owner, _)| owner)
    }

    /// The action `like` names.
    pub fn action(&self) -> Option<&str> {
        self.target().map(|(_, action)| action)
    }
}

/// The keyed `<dependencies>` children of a blok's `<Blok>`, in order.
pub fn declared_dependencies(tree: &ComponentNodeInputModel) -> Vec<DeclaredDependencies> {
    tree.children
        .iter()
        .flatten()
        .filter(|child| child.component == DEPENDENCIES_COMPONENT)
        .filter_map(|child| {
            let key = literal_text(child, KEY_PROP).filter(|key| !key.is_empty())?;
            Some(DeclaredDependencies {
                key: key.to_owned(),
                node: child.id.clone(),
                like: literal_text(child, LIKE_PROP).map(str::to_owned),
            })
        })
        .collect()
}

/// The blok's own `<logo>`: the first among the children of its `<Blok>`, or None. A `<logo>`
/// anywhere else, or a second one, stands for nothing (`logo.misplaced`, `logo.twice`). The
/// port of arkitekt-spec's `declared_logo`.
pub fn declared_logo(tree: &ComponentNodeInputModel) -> Option<&ComponentNodeInputModel> {
    tree.children
        .iter()
        .flatten()
        .find(|child| child.component == LOGO_COMPONENT)
}

fn literal(key: &str, value: &str) -> ComponentPropInputModel {
    ComponentPropInputModel {
        key: key.to_owned(),
        static_value: Some(Value::String(value.to_owned())),
        dynamic_value: None,
        declares_value: None,
        agent_call: None,
        util_call: None,
    }
}

/// The blok's `<logo>` as a logo blok of its own, or None when it declares none:
/// `<Blok kind="agent" context="logo"><Logo viewBox title><G pen>...</G></Logo></Blok>`. What is
/// checked (the pens, the logo limits) for it; its kind is `agent` whatever the blok's, since a
/// `<logo>` is literals only and reads nothing. The drawing's nodes are the `<logo>`'s own, the
/// frame takes its id (and two made from it). The port of arkitekt-spec's `blok_logo_tree`.
pub fn blok_logo_tree(tree: &ComponentNodeInputModel) -> Option<ComponentNodeInputModel> {
    let logo = declared_logo(tree)?;
    let props = logo.props.as_deref().unwrap_or_default();
    let find = |key: &str| props.iter().find(|prop| prop.key == key).cloned();
    let mut drawing_props = vec![
        find(LOGO_VIEW_BOX_PROP).unwrap_or_else(|| literal(LOGO_VIEW_BOX_PROP, LOGO_VIEW_BOX))
    ];
    if let Some(title) = find(LOGO_TITLE_PROP) {
        drawing_props.push(title);
    }
    let pen = ComponentNodeInputModel {
        id: format!("{}.pen", logo.id),
        component: "G".to_owned(),
        props: Some(
            LOGO_PEN
                .iter()
                .map(|(key, value)| literal(key, value))
                .collect(),
        ),
        children: Some(logo.children.clone().unwrap_or_default()),
    };
    let drawing = ComponentNodeInputModel {
        id: format!("{}.drawing", logo.id),
        component: "Logo".to_owned(),
        props: Some(drawing_props),
        children: Some(vec![pen]),
    };
    Some(ComponentNodeInputModel {
        id: logo.id.clone(),
        component: BLOK_COMPONENT.to_owned(),
        props: Some(vec![
            literal(KIND_ATTRIBUTE, LOGO_KIND),
            literal(CONTEXT_ATTRIBUTE, "logo"),
        ]),
        children: Some(vec![drawing]),
    })
}

/// Every node of `tree` with its ancestors, depth first.
pub fn iter_with_parents(
    tree: &ComponentNodeInputModel,
) -> Vec<(&ComponentNodeInputModel, Vec<&ComponentNodeInputModel>)> {
    fn visit<'t>(
        node: &'t ComponentNodeInputModel,
        ancestors: &mut Vec<&'t ComponentNodeInputModel>,
        found: &mut Vec<(
            &'t ComponentNodeInputModel,
            Vec<&'t ComponentNodeInputModel>,
        )>,
    ) {
        found.push((node, ancestors.clone()));
        ancestors.push(node);
        for child in node.children.iter().flatten() {
            visit(child, ancestors, found);
        }
        ancestors.pop();
    }
    let mut found = vec![];
    visit(tree, &mut vec![], &mut found);
    found
}

/// A `<var key value>` as the argument it is stored as, or none when it is none.
pub fn var_argument(node: &ComponentNodeInputModel) -> Option<ActionArgumentInputModel> {
    let key = find_prop(node, KEY_PROP)?
        .static_value
        .as_ref()?
        .as_str()?
        .to_owned();
    let argument = ActionArgumentInputModel {
        key: Some(key),
        value_literal: None,
        value_path: None,
        agent_call: None,
        util_call: None,
        value_list: None,
        value_dict: None,
    };
    let Some(value) = find_prop(node, VALUE_PROP) else {
        return Some(argument);
    };
    if value.static_value.is_none() && value.dynamic_value.is_none() && value.util_call.is_none() {
        return Some(argument);
    }
    if value.agent_call.is_some() {
        return None;
    }
    if let Some(path) = value.dynamic_value.as_ref().and_then(|d| d.path.clone()) {
        return Some(ActionArgumentInputModel {
            value_path: Some(path),
            ..argument
        });
    }
    if let Some(call) = &value.util_call {
        return Some(ActionArgumentInputModel {
            util_call: Some(call.clone()),
            ..argument
        });
    }
    Some(ActionArgumentInputModel {
        value_literal: value.static_value.clone(),
        ..argument
    })
}

fn query_declaration(at: &str, component: &str, problem: String) -> Finding {
    Finding::of(
        "query.declaration",
        Some(at),
        &[("component", s(component)), ("problem", s(problem))],
    )
}

/// Why a `<query>` or `<subscription>` declares nothing readable, or none: `query.declaration`.
pub fn declaration_problem(node: &ComponentNodeInputModel) -> Option<Finding> {
    for key in DECLARATION_TEXT {
        let readable = find_prop(node, key).is_some_and(|prop| {
            is_literal(prop)
                && prop
                    .static_value
                    .as_ref()
                    .and_then(Value::as_str)
                    .is_some_and(|text| !text.is_empty())
        });
        if !readable {
            return Some(query_declaration(
                &node.id,
                &node.component,
                format!("needs {key}=\"...\" as a literal string"),
            ));
        }
    }
    for prop in node.props.iter().flatten() {
        if !DECLARATION_TEXT.contains(&prop.key.as_str()) && !is_literal(prop) {
            return Some(query_declaration(
                &node.id,
                &node.component,
                format!("takes {} as a literal", prop.key),
            ));
        }
    }
    for child in node.children.iter().flatten() {
        if child.component != VAR_COMPONENT {
            return Some(query_declaration(
                &child.id,
                &node.component,
                format!("holds only <var> children, not <{}>", child.component),
            ));
        }
        if var_argument(child).is_none() {
            return Some(query_declaration(
                &child.id,
                &node.component,
                "has a <var> without key=\"...\" or with a task call as its value".into(),
            ));
        }
    }
    if let Err(problem) = declared(node) {
        return Some(query_declaration(&node.id, &node.component, problem));
    }
    None
}

/// A query or a subscription as the model it declares.
pub enum Declared {
    Query(BlokQueryInputModel),
    Subscription(BlokSubscriptionInputModel),
}

fn declared(node: &ComponentNodeInputModel) -> Result<Declared, String> {
    let mut fields = Map::new();
    for prop in node.props.iter().flatten() {
        if let Some(value) = &prop.static_value {
            let key = if prop.key == NAME_PROP {
                "key"
            } else {
                prop.key.as_str()
            };
            fields.insert(key.to_owned(), value.clone());
        }
    }
    let variables: Vec<ActionArgumentInputModel> = node
        .children
        .iter()
        .flatten()
        .filter_map(var_argument)
        .collect();
    fields.insert(
        "variables".into(),
        serde_json::to_value(variables).expect("arguments serialize"),
    );
    let fields = Value::Object(fields);
    if node.component == QUERY_COMPONENT {
        let query: BlokQueryInputModel =
            serde_json::from_value(fields).map_err(|error| error.to_string())?;
        query.validate().map_err(|error| error.to_string())?;
        Ok(Declared::Query(query))
    } else {
        let subscription: BlokSubscriptionInputModel =
            serde_json::from_value(fields).map_err(|error| error.to_string())?;
        subscription.validate().map_err(|error| error.to_string())?;
        Ok(Declared::Subscription(subscription))
    }
}

/// The readable `<query>` children of a blok's `<Blok>`, in order.
pub fn declared_queries(tree: &ComponentNodeInputModel) -> Vec<BlokQueryInputModel> {
    tree.children
        .iter()
        .flatten()
        .filter(|child| child.component == QUERY_COMPONENT && declaration_problem(child).is_none())
        .filter_map(|child| match declared(child) {
            Ok(Declared::Query(query)) => Some(query),
            _ => None,
        })
        .collect()
}

/// The readable `<subscription>` children of a blok's `<Blok>`, in order.
pub fn declared_subscriptions(tree: &ComponentNodeInputModel) -> Vec<BlokSubscriptionInputModel> {
    tree.children
        .iter()
        .flatten()
        .filter(|child| {
            child.component == SUBSCRIPTION_COMPONENT && declaration_problem(child).is_none()
        })
        .filter_map(|child| match declared(child) {
            Ok(Declared::Subscription(subscription)) => Some(subscription),
            _ => None,
        })
        .collect()
}

/// The name a `<local>` declares to its scope (its `name` prop's `declares_value`), or none.
pub fn local_declared(node: &ComponentNodeInputModel) -> Option<&str> {
    if node.component != LOCAL_COMPONENT {
        return None;
    }
    node.props
        .iter()
        .flatten()
        .filter(|prop| prop.key == NAME_PROP)
        .find_map(|prop| {
            prop.declares_value
                .as_deref()
                .filter(|name| !name.is_empty())
        })
}
