//! The blok language's vocabulary (`vocabulary.json`), typed: the Rust twin of arkitekt-spec's
//! `arkitekt_spec/blok/vocabulary.py`. The file is vendored byte for byte from the spec.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::OnceLock;

use regex::Regex;
use serde::Serialize;
use serde_json::{Map, Value};

use crate::pyjson::{repr, repr_str};

const VOCABULARY: &str = include_str!("data/vocabulary.json");

/// A function's parameter; positional arguments map onto params in order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Param {
    pub key: String,
    pub kind: String,
    pub required: bool,
}

/// A function of the language (`str.upper`) or an effect (`ui.copy`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Function {
    pub name: String,
    pub params: Vec<Param>,
    pub returns: String,
    pub description: String,
    pub null: String,
    pub lazy: bool,
    pub effect: bool,
    pub sees_status: bool,
    pub task_operation: bool,
}

impl Function {
    /// The parameter names in positional order.
    pub fn keys(&self) -> Vec<&str> {
        self.params.iter().map(|p| p.key.as_str()).collect()
    }

    /// The parameters every call passes.
    pub fn required_keys(&self) -> Vec<&str> {
        self.params
            .iter()
            .filter(|p| p.required)
            .map(|p| p.key.as_str())
            .collect()
    }
}

/// A blok kind and what it may read: one row of the root matrix.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Kind {
    pub name: String,
    pub roots: HashSet<String>,
    pub locals: bool,
    pub contexts: Vec<String>,
    pub description: String,
}

impl Kind {
    /// Its roots, sorted, joined for a message.
    pub fn roots_text(&self) -> String {
        let mut roots: Vec<&str> = self.roots.iter().map(String::as_str).collect();
        roots.sort_unstable();
        roots.join(", ")
    }
}

/// A drawing context: whether it draws text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Context {
    pub name: String,
    pub text: bool,
}

/// A field of a record (the task record, a query's record, `viewer`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordField {
    pub key: String,
    pub kind: String,
    pub nullable: bool,
    pub choices: Vec<String>,
    pub children: Vec<RecordField>,
}

/// A record shape the renderer fills and a blok reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Record {
    pub name: String,
    pub fields: Vec<RecordField>,
}

/// Whether a finding refuses the blok or is reported.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Level {
    Error,
    Warning,
}

impl Level {
    pub fn as_str(self) -> &'static str {
        match self {
            Level::Error => "error",
            Level::Warning => "warning",
        }
    }
}

/// One row of the diagnostic table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiagnosticSpec {
    pub code: String,
    pub level: Level,
    pub message: String,
}

/// The design language a blok is styled in.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Theme {
    pub colour_roles: Vec<String>,
    pub status_roles: Vec<String>,
    pub colour_utilities: Vec<String>,
    pub palette_colours: Vec<String>,
    pub palette_role_hints: BTreeMap<String, String>,
    pub logo_pens: Vec<String>,
    pub logo_paint_props: Vec<String>,
    pub scene_paint_props: Vec<String>,
    pub custom_variants: Vec<String>,
    pub data_attributes: Vec<String>,
}

/// How small a logo stays.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Limits {
    pub logo_max_nodes: usize,
    pub logo_max_depth: usize,
    pub logo_max_path_data: usize,
    pub logo_max_foreach_items: usize,
    pub logo_path_data_props: Vec<String>,
}

/// `vocabulary.json`, parsed.
#[derive(Debug)]
pub struct Vocabulary {
    pub vocab: i64,
    pub data_roots: Vec<String>,
    pub scoped_roots: Vec<String>,
    pub function_namespaces: Vec<String>,
    pub future_roots: Vec<String>,
    pub reserved: HashSet<String>,
    pub tasks_segment: String,
    pub assets_segment: String,
    pub reserved_state_names: HashSet<String>,
    pub agent_fields: Vec<String>,
    pub kinds: BTreeMap<String, Kind>,
    pub contexts: BTreeMap<String, Context>,
    /// A blok's root component -> the blok context it names (`Html` -> `HTML`).
    pub blok_roots: BTreeMap<String, String>,
    pub records: BTreeMap<String, Record>,
    pub idle_task: Map<String, Value>,
    pub functions: BTreeMap<String, Function>,
    pub operators: HashMap<String, String>,
    pub explicit_id: Regex,
    pub theme: Theme,
    pub limits: Limits,
    pub base_version: i64,
    /// The standard catalogs and their versions, in the file's order.
    pub catalogs: Vec<(String, i64)>,
    /// Per catalog kind of a `bind` prop, the port kinds of the locals it writes.
    pub bind_kinds: BTreeMap<String, HashSet<String>>,
    /// Per port kind of a local, the kinds of an action's arg port that take it whole.
    pub arg_kinds: BTreeMap<String, HashSet<String>>,
    pub diagnostics: HashMap<String, DiagnosticSpec>,
}

impl Vocabulary {
    /// The function called `name`, or none.
    pub fn function(&self, name: &str) -> Option<&Function> {
        self.functions.get(name)
    }

    /// The blok kind called `name`, or none.
    pub fn kind(&self, name: &str) -> Option<&Kind> {
        self.kinds.get(name)
    }

    /// Whether `node_id` is an author's id rather than a derived one.
    pub fn is_explicit_id(&self, node_id: &str) -> bool {
        self.explicit_id.is_match(node_id)
    }

    /// Whether `name` is a reserved root (no local, let or into may take it).
    pub fn is_reserved(&self, name: &str) -> bool {
        self.reserved.contains(name)
    }

    /// The version a standard catalog is at, or none.
    pub fn catalog_version(&self, name: &str) -> Option<i64> {
        self.catalogs
            .iter()
            .find(|(m, _)| m == name)
            .map(|(_, v)| *v)
    }
}

type Loaded<T> = Result<T, String>;

fn object<'a>(value: Option<&'a Value>, where_: &str) -> Loaded<&'a Map<String, Value>> {
    value
        .and_then(Value::as_object)
        .ok_or_else(|| format!("vocabulary.json: {where_} is not an object"))
}

fn list<'a>(value: Option<&'a Value>, where_: &str) -> Loaded<&'a Vec<Value>> {
    value
        .and_then(Value::as_array)
        .ok_or_else(|| format!("vocabulary.json: {where_} is not a list"))
}

fn string(value: Option<&Value>, where_: &str) -> Loaded<String> {
    value
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| format!("vocabulary.json: {where_} is not a string"))
}

fn strings(value: Option<&Value>, where_: &str) -> Loaded<Vec<String>> {
    list(value, where_)?
        .iter()
        .map(|item| string(Some(item), where_))
        .collect()
}

fn flag(entry: &Map<String, Value>, key: &str) -> bool {
    entry.get(key) == Some(&Value::Bool(true))
}

fn text(entry: &Map<String, Value>, key: &str) -> String {
    entry
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn integer(value: Option<&Value>, where_: &str) -> Loaded<i64> {
    value
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("vocabulary.json: {where_} is not an integer"))
}

fn record_fields(raw: Option<&Value>, where_: &str) -> Loaded<Vec<RecordField>> {
    list(raw, where_)?
        .iter()
        .map(|item| {
            let entry = object(Some(item), where_)?;
            Ok(RecordField {
                key: string(entry.get("key"), &format!("{where_}.key"))?,
                kind: string(entry.get("kind"), &format!("{where_}.kind"))?,
                nullable: flag(entry, "nullable"),
                choices: match entry.get("choices") {
                    Some(choices) => strings(Some(choices), &format!("{where_}.choices"))?,
                    None => vec![],
                },
                children: match entry.get("children") {
                    Some(children) => record_fields(Some(children), &format!("{where_}.children"))?,
                    None => vec![],
                },
            })
        })
        .collect()
}

fn function(name: &str, raw: &Value) -> Loaded<Function> {
    let where_ = format!("functions.{name}");
    let entry = object(Some(raw), &where_)?;
    let mut params = vec![];
    for item in list(entry.get("params"), &format!("{where_}.params"))? {
        let spec = list(Some(item), &format!("{where_}.params"))?;
        params.push(Param {
            key: string(spec.first(), &format!("{where_}.params"))?,
            kind: string(spec.get(1), &format!("{where_}.params"))?,
            required: spec.get(2).is_none_or(|r| r == &Value::Bool(true)),
        });
    }
    Ok(Function {
        name: name.to_owned(),
        params,
        returns: string(entry.get("returns"), &format!("{where_}.returns"))?,
        description: text(entry, "description"),
        null: text(entry, "null"),
        lazy: flag(entry, "lazy"),
        effect: flag(entry, "effect"),
        sees_status: flag(entry, "sees_status"),
        task_operation: flag(entry, "task_operation"),
    })
}

/// A `vocabulary.json` payload as a [`Vocabulary`] (`parse_vocabulary`).
pub fn parse_vocabulary(raw: &Value) -> Loaded<Vocabulary> {
    let top = object(Some(raw), "the file")?;
    let roots = object(top.get("roots"), "roots")?;
    let data_roots: Vec<String> = object(roots.get("data"), "roots.data")?
        .keys()
        .cloned()
        .collect();
    let scoped_roots: Vec<String> = object(roots.get("scoped"), "roots.scoped")?
        .keys()
        .cloned()
        .collect();
    let namespaces = strings(roots.get("functions"), "roots.functions")?;
    let future = strings(roots.get("future"), "roots.future")?;
    let reserved: HashSet<String> = strings(top.get("reserved"), "reserved")?
        .into_iter()
        .collect();
    let expected: HashSet<String> = data_roots
        .iter()
        .chain(&scoped_roots)
        .chain(&namespaces)
        .chain(&future)
        .cloned()
        .collect();
    if reserved != expected {
        return Err("vocabulary.json: reserved differs from the roots".into());
    }
    let segments = object(top.get("agent_segments"), "agent_segments")?;

    let mut kinds = BTreeMap::new();
    for (name, item) in object(top.get("kinds"), "kinds")? {
        let entry = object(Some(item), &format!("kinds.{name}"))?;
        let kind_roots: HashSet<String> =
            strings(entry.get("roots"), &format!("kinds.{name}.roots"))?
                .into_iter()
                .collect();
        if kind_roots.iter().any(|root| !data_roots.contains(root)) {
            return Err(format!("vocabulary.json: kind {name} reads unknown roots"));
        }
        kinds.insert(
            name.clone(),
            Kind {
                name: name.clone(),
                roots: kind_roots,
                locals: flag(entry, "locals"),
                contexts: strings(entry.get("contexts"), &format!("kinds.{name}.contexts"))?,
                description: text(entry, "description"),
            },
        );
    }

    let mut contexts = BTreeMap::new();
    for (name, item) in object(top.get("contexts"), "contexts")? {
        let entry = object(Some(item), &format!("contexts.{name}"))?;
        contexts.insert(
            name.clone(),
            Context {
                name: name.clone(),
                text: flag(entry, "text"),
            },
        );
    }
    let blok_roots_entry = object(top.get("blok_roots"), "blok_roots")?;
    let mut blok_roots = BTreeMap::new();
    for (root, context) in object(blok_roots_entry.get("roots"), "blok_roots.roots")? {
        blok_roots.insert(
            root.clone(),
            string(Some(context), &format!("blok_roots.roots.{root}"))?,
        );
    }

    let mut records = BTreeMap::new();
    let mut idle_task = Map::new();
    for (name, item) in object(top.get("records"), "records")? {
        let entry = object(Some(item), &format!("records.{name}"))?;
        records.insert(
            name.clone(),
            Record {
                name: name.clone(),
                fields: record_fields(entry.get("fields"), &format!("records.{name}.fields"))?,
            },
        );
        if name == "task" {
            idle_task = object(entry.get("idle"), "records.task.idle")?.clone();
        }
    }

    let mut functions = BTreeMap::new();
    for (name, item) in object(top.get("functions"), "functions")? {
        if name == "description" {
            continue;
        }
        let namespace = name.split('.').next().unwrap_or_default();
        if !namespaces.iter().any(|n| n == namespace) && name != "task.answer" {
            return Err(format!(
                "vocabulary.json: function {name} is in no function namespace"
            ));
        }
        functions.insert(name.clone(), function(name, item)?);
    }

    let operators = object(top.get("operators"), "operators")?
        .iter()
        .filter(|(key, _)| key.as_str() != "description" && key.as_str() != "notes")
        .filter_map(|(key, value)| value.as_str().map(|v| (key.clone(), v.to_owned())))
        .collect();

    let stored = object(top.get("stored_tree"), "stored_tree")?;
    let explicit = string(stored.get("explicit_id"), "stored_tree.explicit_id")?;
    let explicit_id =
        Regex::new(&explicit).map_err(|e| format!("vocabulary.json: explicit_id: {e}"))?;
    let theme = object(top.get("theme"), "theme")?;
    let hints = object(theme.get("palette_role_hints"), "theme.palette_role_hints")?;
    let limits = object(top.get("limits"), "limits")?;
    let catalogs = object(top.get("catalogs"), "catalogs")?;
    let standard = object(catalogs.get("standard"), "catalogs.standard")?;
    let local_rules = object(top.get("locals"), "locals")?;
    let kind_table = |name: &str| -> Loaded<BTreeMap<String, HashSet<String>>> {
        object(local_rules.get(name), &format!("locals.{name}"))?
            .iter()
            .filter(|(key, _)| key.as_str() != "description")
            .map(|(key, value)| {
                Ok((
                    key.clone(),
                    strings(Some(value), &format!("locals.{name}.{key}"))?
                        .into_iter()
                        .collect(),
                ))
            })
            .collect()
    };

    let mut diagnostics = HashMap::new();
    for (code, item) in object(top.get("diagnostics"), "diagnostics")? {
        if code == "description" {
            continue;
        }
        let entry = object(Some(item), &format!("diagnostics.{code}"))?;
        let level = match entry.get("level").and_then(Value::as_str) {
            Some("error") => Level::Error,
            Some("warning") => Level::Warning,
            _ => return Err(format!("vocabulary.json: diagnostic {code} has no level")),
        };
        diagnostics.insert(
            code.clone(),
            DiagnosticSpec {
                code: code.clone(),
                level,
                message: string(entry.get("message"), &format!("diagnostics.{code}.message"))?,
            },
        );
    }

    let count = |key: &str| -> Loaded<usize> {
        integer(limits.get(key), &format!("limits.{key}"))
            .map(|n| usize::try_from(n).unwrap_or_default())
    };
    Ok(Vocabulary {
        vocab: integer(top.get("vocab"), "vocab")?,
        data_roots,
        scoped_roots,
        function_namespaces: namespaces,
        future_roots: future,
        reserved,
        tasks_segment: string(segments.get("tasks"), "agent_segments.tasks")?,
        assets_segment: string(segments.get("assets"), "agent_segments.assets")?,
        reserved_state_names: strings(segments.get("reserved_state_names"), "agent_segments")?
            .into_iter()
            .collect(),
        agent_fields: object(segments.get("fields"), "agent_segments.fields")?
            .keys()
            .cloned()
            .collect(),
        kinds,
        contexts,
        blok_roots,
        records,
        idle_task,
        functions,
        operators,
        explicit_id,
        theme: Theme {
            colour_roles: strings(theme.get("colour_roles"), "theme.colour_roles")?,
            status_roles: strings(theme.get("status_roles"), "theme.status_roles")?,
            colour_utilities: strings(theme.get("colour_utilities"), "theme.colour_utilities")?,
            palette_colours: strings(theme.get("palette_colours"), "theme.palette_colours")?,
            palette_role_hints: hints
                .iter()
                .map(|(k, v)| Ok((k.clone(), string(Some(v), "theme.palette_role_hints")?)))
                .collect::<Loaded<_>>()?,
            logo_pens: strings(theme.get("logo_pens"), "theme.logo_pens")?,
            logo_paint_props: strings(theme.get("logo_paint_props"), "theme.logo_paint_props")?,
            scene_paint_props: strings(theme.get("scene_paint_props"), "theme.scene_paint_props")?,
            custom_variants: strings(theme.get("custom_variants"), "theme.custom_variants")?,
            data_attributes: object(theme.get("data_attributes"), "theme.data_attributes")?
                .keys()
                .cloned()
                .collect(),
        },
        limits: Limits {
            logo_max_nodes: count("logo_max_nodes")?,
            logo_max_depth: count("logo_max_depth")?,
            logo_max_path_data: count("logo_max_path_data")?,
            logo_max_foreach_items: count("logo_max_foreach_items")?,
            logo_path_data_props: strings(
                limits.get("logo_path_data_props"),
                "limits.logo_path_data_props",
            )?,
        },
        base_version: integer(catalogs.get("base"), "catalogs.base")?,
        catalogs: standard
            .iter()
            .map(|(name, version)| {
                Ok((
                    name.clone(),
                    integer(Some(version), &format!("catalogs.standard.{name}"))?,
                ))
            })
            .collect::<Loaded<_>>()?,
        bind_kinds: kind_table("bind_kinds")?,
        arg_kinds: kind_table("arg_kinds")?,
        diagnostics,
    })
}

/// `vocabulary.json` as vendored, for a party that serves it on.
pub fn vocabulary_text() -> &'static str {
    VOCABULARY
}

/// The vendored vocabulary, parsed once. It ships with the binary: a broken copy is a build
/// that cannot serve, so it panics at first use (and in the tests).
pub fn vocabulary() -> &'static Vocabulary {
    static VOCAB: OnceLock<Vocabulary> = OnceLock::new();
    VOCAB.get_or_init(|| {
        let raw: Value =
            serde_json::from_str(VOCABULARY).expect("the vendored vocabulary.json is JSON");
        parse_vocabulary(&raw).expect("the vendored vocabulary.json is a vocabulary")
    })
}

/// A field a diagnostic message is filled with.
pub type Fields<'a> = &'a [(&'a str, Value)];

/// Fill a message template as Python's `str.format_map` would: `{name}` (a string as is,
/// anything else as its repr), `{name!r}` (the repr), `{{` and `}}`; a missing field reads `?`.
pub fn fill(template: &str, fields: Fields<'_>) -> String {
    let mut out = String::with_capacity(template.len());
    let mut chars = template.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '{' if chars.peek() == Some(&'{') => {
                chars.next();
                out.push('{');
            }
            '}' if chars.peek() == Some(&'}') => {
                chars.next();
                out.push('}');
            }
            '{' => {
                let mut name = String::new();
                for inner in chars.by_ref() {
                    if inner == '}' {
                        break;
                    }
                    name.push(inner);
                }
                let (name, conversion) = match name.split_once('!') {
                    Some((name, conversion)) => (name.to_owned(), Some(conversion.to_owned())),
                    None => (name, None),
                };
                match fields.iter().find(|(key, _)| *key == name) {
                    None => out.push('?'),
                    Some((_, value)) => match (value, conversion.as_deref()) {
                        (Value::String(s), Some("r")) => out.push_str(&repr_str(s)),
                        (Value::String(s), _) => out.push_str(s),
                        (other, _) => out.push_str(&repr(other)),
                    },
                }
            }
            other => out.push(other),
        }
    }
    out
}

/// The message of `code`, filled with `fields`; the code itself when the table lacks it.
pub fn diagnostic_message(code: &str, fields: Fields<'_>) -> String {
    vocabulary()
        .diagnostics
        .get(code)
        .map(|spec| fill(&spec.message, fields))
        .unwrap_or_else(|| code.to_owned())
}

/// A diagnostic: a row of the table, filled, and where (a node id, a declaration).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finding {
    pub code: String,
    pub level: Level,
    pub message: String,
    pub path: Option<String>,
}

impl Finding {
    /// The finding `code` at `path`, its message filled with `fields`. A code the table lacks
    /// is a programming error: it panics in debug builds and reads as an error otherwise.
    pub fn of(code: &str, path: Option<&str>, fields: Fields<'_>) -> Finding {
        let spec = vocabulary().diagnostics.get(code);
        debug_assert!(spec.is_some(), "no diagnostic {code} in the vocabulary");
        Finding {
            code: code.to_owned(),
            level: spec.map_or(Level::Error, |s| s.level),
            message: spec.map_or_else(|| code.to_owned(), |s| fill(&s.message, fields)),
            path: path.map(str::to_owned),
        }
    }

    /// Whether it refuses the blok.
    pub fn is_error(&self) -> bool {
        self.level == Level::Error
    }

    /// The finding as JSON: `{code, level, message, path?}` (`Finding.to_json`).
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("code".into(), Value::String(self.code.clone()));
        out.insert("level".into(), Value::String(self.level.as_str().into()));
        out.insert("message".into(), Value::String(self.message.clone()));
        if let Some(path) = &self.path {
            out.insert("path".into(), Value::String(path.clone()));
        }
        Value::Object(out)
    }
}

impl std::fmt::Display for Finding {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match &self.path {
            Some(path) => write!(f, "{path}: {} [{}]", self.message, self.code),
            None => write!(f, "{} [{}]", self.message, self.code),
        }
    }
}
