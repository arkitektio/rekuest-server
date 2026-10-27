//! The catalogs a v2 blok renders with (`arkitekt_spec/blok`), vendored from the spec:
//! `catalogs/base@2.json` (the language), the standard catalogs (`ui@1`, `scene@1`, `logo@1`,
//! `structure@1`, `html@1`) and `modules/orkestrator.json` (the `orkestrator` module, what only
//! the Orkestrator draws). A *catalog* is spec-owned and versioned, a blok declares the ones it
//! needs; a *module* is an app's own unversioned extras, registered as a `Module`, which a blok
//! names as its `module`. A [`BlokView`] merges them with whatever a UI app registered, and
//! adds the vocabulary's functions.

use std::collections::BTreeMap;
use std::sync::OnceLock;

use serde::Deserialize;

use super::vocabulary::{vocabulary, Function, Param};
use crate::inputs::{CatalogComponentInputModel, CatalogOperationInputModel};

/// The language catalog every renderer implements.
pub const BASE_NAME: &str = "base";
/// The module of what only the Orkestrator draws, beside the standard catalogs.
pub const ORKESTRATOR_MODULE: &str = "orkestrator";
/// Where the Orkestrator's module ships, relative to `arkitekt_spec/blok`.
pub const ORKESTRATOR_MODULE_FILE: &str = "modules/orkestrator.json";
/// The standard catalogs, in the spec's order.
pub const STANDARD_CATALOGS: [&str; 5] = ["ui", "scene", "logo", "structure", "html"];

const BASE_2: &str = include_str!("data/catalogs/base@2.json");
const UI_1: &str = include_str!("data/catalogs/ui@1.json");
const SCENE_1: &str = include_str!("data/catalogs/scene@1.json");
const LOGO_1: &str = include_str!("data/catalogs/logo@1.json");
const STRUCTURE_1: &str = include_str!("data/catalogs/structure@1.json");
const HTML_1: &str = include_str!("data/catalogs/html@1.json");
const ORKESTRATOR: &str = include_str!("data/modules/orkestrator.json");

/// One spec-owned catalog file.
#[derive(Debug, Clone, Deserialize)]
pub struct SpecCatalog {
    pub name: String,
    #[serde(default)]
    pub version: Option<i64>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub components: Vec<CatalogComponentInputModel>,
}

impl SpecCatalog {
    /// How it is named: `ui@1`, or `orkestrator` for one without a version.
    pub fn label(&self) -> String {
        match self.version {
            Some(version) => format!("{}@{version}", self.name),
            None => self.name.clone(),
        }
    }
}

fn load(file: &str, text: &str) -> SpecCatalog {
    let mut catalog: SpecCatalog = serde_json::from_str(text)
        .unwrap_or_else(|e| panic!("the vendored {file} is no catalog: {e}"));
    let label = catalog.label();
    for component in &mut catalog.components {
        component.catalog = Some(label.clone());
    }
    catalog
}

/// The vendored text of a spec file (`catalogs/ui@1.json`, `modules/orkestrator.json`), for a
/// party that serves it on.
pub fn catalog_text(file: &str) -> Option<&'static str> {
    Some(match file {
        "catalogs/base@2.json" => BASE_2,
        "catalogs/ui@1.json" => UI_1,
        "catalogs/scene@1.json" => SCENE_1,
        "catalogs/logo@1.json" => LOGO_1,
        "catalogs/structure@1.json" => STRUCTURE_1,
        "catalogs/html@1.json" => HTML_1,
        ORKESTRATOR_MODULE_FILE => ORKESTRATOR,
        _ => return None,
    })
}

/// base@2: the language components.
pub fn base_catalog() -> &'static SpecCatalog {
    static CATALOG: OnceLock<SpecCatalog> = OnceLock::new();
    CATALOG.get_or_init(|| {
        let file = format!("catalogs/{BASE_NAME}@{}.json", vocabulary().base_version);
        let text = catalog_text(&file).unwrap_or_else(|| panic!("no vendored {file}"));
        load(&file, text)
    })
}

/// Every standard catalog at the version the vocabulary names, in the spec's order.
pub fn standard_catalogs() -> &'static [SpecCatalog] {
    static CATALOGS: OnceLock<Vec<SpecCatalog>> = OnceLock::new();
    CATALOGS.get_or_init(|| {
        STANDARD_CATALOGS
            .iter()
            .map(|name| {
                let version = vocabulary()
                    .catalog_version(name)
                    .unwrap_or_else(|| panic!("the vocabulary names no catalog {name}"));
                let file = format!("catalogs/{name}@{version}.json");
                let text = catalog_text(&file).unwrap_or_else(|| panic!("no vendored {file}"));
                load(&file, text)
            })
            .collect()
    })
}

/// The `orkestrator` module: what only the Orkestrator draws.
pub fn orkestrator_module() -> &'static SpecCatalog {
    static CATALOG: OnceLock<SpecCatalog> = OnceLock::new();
    CATALOG.get_or_init(|| load(ORKESTRATOR_MODULE_FILE, ORKESTRATOR))
}

/// A component name two sources define differently: which one is drawn (`Shadow`).
///
/// A module's definition beats a standard catalog's (an app draws its own `Slider`); the
/// language's (base) is never shadowed; between two standard catalogs the first, in the
/// vocabulary's order, is kept.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Shadow {
    pub component: String,
    /// The source that defines it first: base or a standard catalog (`ui@1`).
    pub catalog: String,
    /// The source that defines it again: a module, or a later standard catalog.
    pub module: String,
    /// The one of the two whose definition is used.
    pub winner: String,
}

/// What a v2 blok is checked against: the components of base, the standard catalogs and the
/// Orkestrator's module (and what a UI registered), and the vocabulary's functions plus those
/// a module adds.
#[derive(Debug, Clone)]
pub struct BlokView {
    pub components: BTreeMap<String, CatalogComponentInputModel>,
    pub functions: BTreeMap<String, Function>,
    pub label: String,
    /// Whether the components are every one the renderer draws, so an unknown one is a finding.
    pub complete: bool,
    /// Per component name two sources define differently, which one is used (`catalog.shadow`).
    pub shadows: BTreeMap<String, Shadow>,
}

impl BlokView {
    /// The component called `name`, or none.
    pub fn component(&self, name: &str) -> Option<&CatalogComponentInputModel> {
        self.components.get(name)
    }

    /// The function called `name`, or none.
    pub fn function(&self, name: &str) -> Option<&Function> {
        self.functions.get(name)
    }
}

/// Equal as the spec's ComponentSpec compares: everything but the catalog that provides it.
fn same_component(a: &CatalogComponentInputModel, b: &CatalogComponentInputModel) -> bool {
    let mut b = b.clone();
    b.catalog.clone_from(&a.catalog);
    *a == b
}

/// How one source joins the view.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Source {
    /// base or a standard catalog: a name it defines again is kept as first defined.
    Catalog,
    /// a module: beats a standard catalog's (or an earlier module's) definition, never base's.
    Module,
    /// the Orkestrator's registered module: adds what the vendored files lack, nothing else.
    Additive,
}

#[derive(Default)]
struct Merged {
    components: BTreeMap<String, CatalogComponentInputModel>,
    shadows: BTreeMap<String, Shadow>,
}

fn merge(merged: &mut Merged, added: &[CatalogComponentInputModel], label: &str, source: Source) {
    let base = base_catalog().label();
    for component in added {
        let mut component = component.clone();
        if component.catalog.is_none() {
            component.catalog = Some(label.to_owned());
        }
        let Some(previous) = merged.components.get(&component.name) else {
            merged.components.insert(component.name.clone(), component);
            continue;
        };
        if same_component(previous, &component) || source == Source::Additive {
            continue;
        }
        let kept = previous.catalog.clone().unwrap_or_else(|| "?".into());
        let other = component
            .catalog
            .clone()
            .unwrap_or_else(|| label.to_owned());
        let wins = source == Source::Module && kept != base;
        let shadow = Shadow {
            component: component.name.clone(),
            catalog: kept.clone(),
            module: other.clone(),
            winner: if wins { other } else { kept },
        };
        merged.shadows.insert(component.name.clone(), shadow);
        if wins {
            merged.components.insert(component.name.clone(), component);
        }
    }
}

/// The view base, the standard catalogs and the Orkestrator's module give, with what the
/// blok's registered `Module` adds on top (`blok_view`): `registered` is its name and
/// components, its operations `extra_operations`.
///
/// A name two sources define differently is drawn as [`Shadow`] says (a module's over a
/// standard catalog's; base's always) and recorded in `shadows`; the checker warns
/// `catalog.shadow` where a tree uses it. The Orkestrator's registered module is the vendored
/// one as the app has it: it only adds names the vendored files lack. An operation joins the
/// functions only under a function namespace, and never replaces one of the vocabulary's.
pub fn blok_view(
    registered: Option<(&str, &[CatalogComponentInputModel])>,
    extra_operations: &[CatalogOperationInputModel],
    complete: bool,
) -> BlokView {
    let vocabulary = vocabulary();
    let mut merged = Merged::default();
    let base = base_catalog();
    let mut labels = vec![base.label()];
    merge(
        &mut merged,
        &base.components,
        &base.label(),
        Source::Catalog,
    );
    for catalog in standard_catalogs() {
        labels.push(catalog.label());
        merge(
            &mut merged,
            &catalog.components,
            &catalog.label(),
            Source::Catalog,
        );
    }
    let orkestrator = orkestrator_module();
    labels.push(orkestrator.label());
    merge(
        &mut merged,
        &orkestrator.components,
        &orkestrator.label(),
        Source::Module,
    );
    if let Some((name, extra)) = registered {
        labels.push(name.to_owned());
        let source = if name == ORKESTRATOR_MODULE {
            Source::Additive
        } else {
            Source::Module
        };
        merge(&mut merged, extra, name, source);
    }
    let Merged {
        components,
        shadows,
    } = merged;
    let mut functions = vocabulary.functions.clone();
    for operation in extra_operations {
        let namespace = operation.name.split('.').next().unwrap_or_default();
        if functions.contains_key(&operation.name)
            || !vocabulary
                .function_namespaces
                .iter()
                .any(|n| n == namespace)
        {
            continue;
        }
        functions.insert(
            operation.name.clone(),
            Function {
                name: operation.name.clone(),
                params: operation
                    .arguments
                    .iter()
                    .map(|a| Param {
                        key: a.key.clone(),
                        kind: a.kind.value().to_owned(),
                        required: a.required,
                    })
                    .collect(),
                returns: operation.returns.value().to_owned(),
                description: operation.description.clone().unwrap_or_default(),
                null: String::new(),
                lazy: false,
                effect: false,
                sees_status: false,
                task_operation: false,
            },
        );
    }
    BlokView {
        components,
        functions,
        label: labels.join(" + "),
        complete,
        shadows,
    }
}

/// The Orkestrator's view: base, every standard catalog and its module (`default_view`).
pub fn default_view() -> &'static BlokView {
    static VIEW: OnceLock<BlokView> = OnceLock::new();
    VIEW.get_or_init(|| {
        let view = blok_view(None, &[], true);
        assert!(
            view.shadows.is_empty(),
            "the vendored catalogs shadow each other: {:?}",
            view.shadows
        );
        view
    })
}
