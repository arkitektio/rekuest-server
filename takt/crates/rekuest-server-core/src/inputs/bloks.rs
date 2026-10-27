//! What a v2 blok declares beside its tree (`rekuest_core/inputs/models.py`, the blok v2
//! declarations): its locals, its demo and its declared reads (queries, subscriptions).

use serde::de::Error as _;
use serde::ser::Error as _;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Map, Value};

use super::calls::ActionArgumentInputModel;
use super::ports::ArgPortInputModel;
use super::ValidationError;
use crate::blok::vocabulary::{diagnostic_message, vocabulary};
use crate::enums::{ComponentContext, LocalKind, PortKind};

/// A local: blok-level form state that feeds a call, written only through `bind` on controls
/// (`BlokLocalInputModel`). A local is an arg port (its key the name the tree reads it by) whose
/// kind is a port kind or ANY, plus `persist`.
///
/// Its kind may be left out (absent or null on the wire): it is then inferred from the tree
/// (`blok::infer::resolve_scope`), and the stored local always carries one. A left-out kind is
/// kept distinct from a given ANY until inference has run: nothing fills it in when reading.
#[derive(Debug, Clone, PartialEq)]
pub struct BlokLocalInputModel {
    /// Its port fields. `port.kind` is the local's kind when that is a port kind; for ANY or a
    /// left-out kind it is a placeholder nothing reads (`arg_port()` is none).
    pub port: ArgPortInputModel,
    /// Its kind: none when left out, `ANY` for an untyped value.
    pub kind: Option<LocalKind>,
    /// Remember the last input on this device (never on a server).
    pub persist: bool,
    /// The fields the author sent (non-null), bar `kind`: what wins over an inferred type,
    /// and all a left-out local dumps (`model_fields_set`).
    pub sent: Vec<String>,
}

/// The kind a port of an unknown or ANY local is read with; nothing reads it.
const PLACEHOLDER_KIND: PortKind = PortKind::STRING;

impl BlokLocalInputModel {
    /// Its name, read bare in the tree (`{iterations}`).
    pub fn key(&self) -> &str {
        &self.port.key
    }

    /// Whether its kind is given (ANY included), rather than left to the tree.
    pub fn kind_given(&self) -> bool {
        self.kind.is_some()
    }

    /// The arg port it is, or none for an ANY (or not yet resolved) local.
    pub fn arg_port(&self) -> Option<&ArgPortInputModel> {
        self.kind.and_then(LocalKind::port_kind).map(|_| &self.port)
    }

    /// The arg port's rules, bar the default's fit (that is the checker's `local.default`);
    /// an ANY or unresolved local only those that hold whatever its kind.
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        match self.kind.and_then(LocalKind::port_kind) {
            Some(_) => self.port.validate_port(false),
            None => self.port.validate_untyped(),
        }
    }

    /// What the author sent, as JSON: the fields that win over an inferred type.
    pub fn own(&self) -> Map<String, Value> {
        let Ok(Value::Object(dumped)) = serde_json::to_value(&self.port) else {
            return Map::new();
        };
        dumped
            .into_iter()
            .filter(|(key, _)| self.sent.iter().any(|sent| sent == key))
            .collect()
    }

    /// This local with `typed` (a kind, and what goes with it) filling what it does not say;
    /// none when that makes no valid local.
    pub fn typed(&self, typed: &Map<String, Value>) -> Option<BlokLocalInputModel> {
        let mut merged = typed.clone();
        merged.extend(self.own());
        merged.insert("persist".into(), Value::Bool(self.persist));
        let mut local: BlokLocalInputModel = serde_json::from_value(Value::Object(merged)).ok()?;
        local.kind?;
        local.validate().ok()?;
        Some(local)
    }

    /// This local resolved to ANY.
    pub fn any(&self) -> BlokLocalInputModel {
        BlokLocalInputModel {
            kind: Some(LocalKind::ANY),
            ..self.clone()
        }
    }
}

impl<'de> Deserialize<'de> for BlokLocalInputModel {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = Map::<String, Value>::deserialize(deserializer)?;
        // Null reads as left out, for every field.
        let mut raw: Map<String, Value> = raw.into_iter().filter(|(_, v)| !v.is_null()).collect();
        let mut sent: Vec<String> = raw.keys().filter(|k| *k != "kind").cloned().collect();
        let persist = match raw.remove("persist") {
            None => false,
            Some(Value::Bool(persist)) => persist,
            Some(other) => {
                return Err(D::Error::custom(format!(
                    "persist: expected a bool, got {other}"
                )))
            }
        };
        let kind: Option<LocalKind> = raw
            .remove("kind")
            .map(serde_json::from_value)
            .transpose()
            .map_err(|e| D::Error::custom(format!("kind: {e}")))?;
        sent.sort();
        let port_kind = kind
            .and_then(LocalKind::port_kind)
            .unwrap_or(PLACEHOLDER_KIND);
        raw.insert("kind".into(), Value::String(port_kind.value().to_owned()));
        let port = ArgPortInputModel::deserialize(Value::Object(raw)).map_err(D::Error::custom)?;
        Ok(BlokLocalInputModel {
            port,
            kind,
            persist,
            sent,
        })
    }
}

impl Serialize for BlokLocalInputModel {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut dumped = match serde_json::to_value(&self.port).map_err(S::Error::custom)? {
            Value::Object(map) => map,
            _ => return Err(S::Error::custom("a port dumps as an object")),
        };
        dumped.insert("persist".into(), Value::Bool(self.persist));
        match self.kind {
            Some(kind) => {
                dumped.insert("kind".into(), Value::String(kind.value().to_owned()));
                dumped.serialize(serializer)
            }
            // A local whose kind is left out goes out with only the fields its author sent, so
            // whoever resolves it next still fills what was left out.
            None => dumped
                .into_iter()
                .filter(|(key, _)| self.sent.contains(key))
                .collect::<Map<String, Value>>()
                .serialize(serializer),
        }
    }
}

/// A declared read: one GraphQL `query` on a service (`BlokQueryInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlokQueryInputModel {
    pub key: String,
    pub service: String,
    pub document: String,
    #[serde(default)]
    pub variables: Vec<ActionArgumentInputModel>,
    #[serde(default)]
    pub poll: Option<f64>,
    /// The list field of its result that `fetch_more` appends new pages to, as a dotted path
    /// into the data (`positions`, `images.items`); without it the query cannot fetch more.
    #[serde(default)]
    pub more: Option<String>,
}

/// A declared stream: one GraphQL `subscription` on a service (`BlokSubscriptionInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlokSubscriptionInputModel {
    pub key: String,
    pub service: String,
    pub document: String,
    #[serde(default)]
    pub variables: Vec<ActionArgumentInputModel>,
    #[serde(default)]
    pub keep: Option<i64>,
    #[serde(default)]
    pub history: Option<String>,
}

/// What a preview renders a blok with, mirroring the roots it reads (`BlokDemoInputModel`).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlokDemoInputModel {
    #[serde(default)]
    pub deps: Option<Value>,
    #[serde(default)]
    pub args: Option<Value>,
    #[serde(default)]
    pub task: Option<Value>,
    #[serde(default)]
    pub queries: Option<Value>,
    #[serde(default)]
    pub subscriptions: Option<Value>,
}

fn is_name(key: &str) -> bool {
    let mut chars = key.chars();
    chars
        .next()
        .is_some_and(|first| first == '_' || first.is_ascii_alphabetic())
        && chars.all(|c| c == '_' || c.is_ascii_alphanumeric())
}

/// A query's `more`: a dotted path of names (`positions`, `images.items`).
fn is_field_path(path: &str) -> bool {
    path.split('.').all(is_name)
}

/// A catalog is spelled `name@version` (`ui@1`).
fn is_catalog(catalog: &str) -> bool {
    let Some((name, version)) = catalog.split_once('@') else {
        return false;
    };
    let mut chars = name.chars();
    chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
        && !version.is_empty()
        && version.chars().all(|c| c.is_ascii_digit())
}

/// The kind of a GraphQL document's first operation (`operation_keyword`): `query` (also for
/// `{ ... }`), `mutation`, `subscription`, or whatever word it starts with.
pub fn operation_keyword(document: &str) -> String {
    let text: String = document
        .split('\n')
        .map(|line| line.split_once('#').map_or(line, |(before, _)| before))
        .collect::<Vec<_>>()
        .join("\n");
    let text = text.trim_start();
    if text.starts_with('{') {
        return "query".into();
    }
    text.chars()
        .take_while(|c| c.is_ascii_alphabetic() || *c == '_')
        .collect()
}

/// What a v2 blok declares beside its tree (`check_blok_declarations`)
#[derive(Debug, Clone, Copy)]
pub struct BlokDeclarations<'a> {
    pub catalogs: &'a [String],
    pub locals: &'a [BlokLocalInputModel],
    pub queries: &'a [BlokQueryInputModel],
    pub subscriptions: &'a [BlokSubscriptionInputModel],
    pub dependency_keys: &'a [&'a str],
    pub demo: Option<&'a BlokDemoInputModel>,
}

/// The rules of what a v2 blok declares beside its tree, as the spec's `check_blok_declarations`
/// words them: catalogs spelled `name@version`; locals, queries and subscriptions each named
/// once, by a name (a local by no reserved one); a query a `query`, a subscription a
/// `subscription`, never a mutation; a query's `more` a dotted field path; a subscription's history a declared query; an action logo
/// declaring nothing; `demo.deps` naming only dependencies (and `self`). The tree is checked
/// against them by the blok checker.
pub fn check_blok_declarations(
    blok: BlokDeclarations<'_>,
    kind: &str,
    reserved: &dyn Fn(&str) -> bool,
) -> Result<(), ValidationError> {
    for catalog in blok.catalogs {
        if !is_catalog(catalog) {
            return Err(blok_error("manifest.catalog", &[("catalog", s(catalog))]));
        }
    }
    let named: [(&str, Vec<&str>); 3] = [
        (
            "locals",
            blok.locals.iter().map(BlokLocalInputModel::key).collect(),
        ),
        (
            "queries",
            blok.queries.iter().map(|q| q.key.as_str()).collect(),
        ),
        (
            "subscriptions",
            blok.subscriptions.iter().map(|s| s.key.as_str()).collect(),
        ),
    ];
    for (what, keys) in &named {
        // The first key (in order) that is declared more than once, as Python's `next(...)`.
        if let Some(twice) = keys
            .iter()
            .find(|key| keys.iter().filter(|other| other == key).count() > 1)
        {
            return Err(blok_error(
                "manifest.duplicate",
                &[("what", s(*what)), ("key", s(*twice))],
            ));
        }
        for key in keys {
            if !is_name(key) {
                return Err(blok_error(
                    "manifest.name",
                    &[("what", s(*what)), ("key", s(*key))],
                ));
            }
            if *what == "locals" && reserved(key) {
                return Err(blok_error("roots.reserved", &[("name", s(*key))]));
            }
        }
    }
    for query in blok.queries {
        check_operation("query", &query.key, &operation_keyword(&query.document))?;
        if let Some(more) = &query.more {
            if !is_field_path(more) {
                let fields = [
                    ("name", s(&query.key)),
                    ("more", s(more)),
                    (
                        "problem",
                        s("is not a field path into its data (positions, images.items)"),
                    ),
                ];
                return Err(blok_error("query.more", &fields));
            }
        }
    }
    for subscription in blok.subscriptions {
        check_operation(
            "subscription",
            &subscription.key,
            &operation_keyword(&subscription.document),
        )?;
        if let Some(history) = &subscription.history {
            if !blok.queries.iter().any(|q| &q.key == history) {
                return Err(blok_error(
                    "query.history",
                    &[("name", s(&subscription.key)), ("history", s(history))],
                ));
            }
        }
    }
    if kind == "action_logo"
        && !(blok.locals.is_empty()
            && blok.queries.is_empty()
            && blok.subscriptions.is_empty()
            && blok.dependency_keys.is_empty())
    {
        return Err(blok_error("manifest.action_logo", &[]));
    }
    if let Some(Value::Object(deps)) = blok.demo.and_then(|demo| demo.deps.as_ref()) {
        let mut unknown: Vec<&str> = deps
            .keys()
            .map(String::as_str)
            .filter(|key| *key != "self" && !blok.dependency_keys.contains(key))
            .collect();
        unknown.sort_unstable();
        if !unknown.is_empty() {
            return Err(blok_error(
                "manifest.demo",
                &[("keys", s(unknown.join(", ")))],
            ));
        }
    }
    Ok(())
}

fn s(value: impl Into<String>) -> Value {
    Value::String(value.into())
}

/// A blok refused with a row of the vocabulary's diagnostic table (`BlokError(code, **fields)`):
/// its message filled, its code after it.
pub fn blok_error(code: &str, fields: &[(&str, Value)]) -> ValidationError {
    ValidationError(format!("{} [{code}]", diagnostic_message(code, fields)))
}

/// A query's document is a `query`, a subscription's a `subscription`; neither writes
/// (`check_operation`).
pub fn check_operation(kind: &str, name: &str, operation: &str) -> Result<(), ValidationError> {
    if operation == kind {
        return Ok(());
    }
    if operation == "mutation" {
        let fields = [
            ("kind", s(kind)),
            ("name", s(name)),
            (
                "problem",
                s("is a mutation: a blok reads, and writes only through a task"),
            ),
        ];
        return Err(blok_error("query.read_only", &fields));
    }
    let operation = if operation.is_empty() {
        "document without an operation"
    } else {
        operation
    };
    let fields = [
        ("kind", s(kind)),
        ("name", s(name)),
        ("operation", s(operation)),
    ];
    Err(blok_error("query.kind", &fields))
}

/// `context.kind`: a blok of `kind` drawn in a context its kind has no blok for (`wrong_context`).
pub fn wrong_context(kind: &str, context: ComponentContext) -> ValidationError {
    let contexts = vocabulary()
        .kind(kind)
        .map(|k| k.contexts.join(", "))
        .unwrap_or_default();
    let n = if kind.starts_with(['a', 'e', 'i', 'o', 'u']) {
        "n"
    } else {
        ""
    };
    let fields = [
        ("n", s(n)),
        ("kind", s(kind)),
        ("contexts", s(contexts)),
        ("context", s(context.value())),
    ];
    blok_error("context.kind", &fields)
}

/// A blok is sent as its components or as its BSX source: not both, and (where it must be
/// sent) not neither (`check_blok_sent_once`).
pub fn check_blok_sent_once(
    components: bool,
    source: bool,
    required: bool,
) -> Result<(), ValidationError> {
    if components && source {
        return Err(blok_error("manifest.sent", &[("problem", s("not both"))]));
    }
    if required && !components && !source {
        return Err(blok_error(
            "manifest.sent",
            &[("problem", s("and this one has neither"))],
        ));
    }
    Ok(())
}
