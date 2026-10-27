//! Blok trees checked against the v2 catalogs, and port calls and widgets against base@1
//! (`facade/catalog_validation.py`).
//!
//! A blok tree (blok language v2) is checked by `rekuest_core::blok`'s checker against a view:
//! the spec's catalogs (base@2, the standard catalogs and the Orkestrator's module, vendored with
//! the vocabulary) plus what the blok's `Module` registered beyond them. Every finding carries
//! its vocabulary code; an error refuses the blok, a warning is stored with it.
//!
//! Port calls and widgets feed an action's definition hash, so they stay on base@1: hard errors
//! (`Err`) for a known operation called wrongly or two catalogs disagreeing; warnings
//! ([`Diagnostic`], stored on the row) for an operation or catalog nobody provides yet.

use std::collections::{BTreeMap, HashMap, HashSet};

use rekuest_core::blok::infer::{resolve_scope, ArgPorts};
use rekuest_core::blok::view::ORKESTRATOR_MODULE;
use rekuest_core::blok::{blok_view, check_blok_tree, BlokScope, BlokView, Finding};
use rekuest_core::catalogs::{
    base_components, base_operations, base_version_named, BASE_CATALOG_ID, BASE_CATALOG_VERSION,
};
use rekuest_core::enums::CatalogValueKind;
use rekuest_core::inputs::{
    blok_asset_path, iter_component_nodes, iter_util_calls, ArgPortInputModel,
    AssignWidgetInputModel, CatalogComponentInputModel, CatalogOperationInputModel,
    ComponentNodeInputModel, ComponentPropInputModel, DefinitionInputModel, OptimisticInputModel,
    ReturnPortInputModel, ReturnWidgetInputModel, UtilCallInputModel, ANCHOR_COMPONENT,
    ASK_COMPONENT, ROUTE_COMPONENT,
};
use rekuest_core::pyjson::{repr, repr_str};
use serde::Serialize;
use serde_json::Value;
use sqlx::PgConnection;

pub const UNKNOWN_OPERATION: &str = "unknown_operation";
pub const UNKNOWN_CATALOG: &str = "unknown_catalog";
// What a blok does against the UI catalog it renders in is the UI's business: the renderer
// draws what it does not know as an inline error. So these are findings stored on the row,
// never a refusal; what the server builds on (parse, ids, dependencies, args, services, the
// context root, logo limits, asset bindings) still refuses.
pub const UNKNOWN_COMPONENT: &str = "unknown_component";
pub const UNKNOWN_PROP: &str = "unknown_prop";
pub const MISSING_PROP: &str = "missing_prop";
pub const CHILDREN_NOT_ACCEPTED: &str = "children_not_accepted";
pub const UNBOUND_CALLBACK: &str = "unbound_callback";
pub const MISPLACED_COMPONENT: &str = "misplaced_component";
pub const OPERATION_ARGUMENTS: &str = "operation_arguments";
pub const CATALOG_CONFLICT: &str = "catalog_conflict";
/// A non-fatal registration finding (`DiagnosticModel`), dumped as stored on the row.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Diagnostic {
    pub level: &'static str,
    pub code: String,
    pub message: String,
    pub path: Option<String>,
}

impl Diagnostic {
    fn warning(code: &str, message: String, path: String) -> Self {
        Self {
            level: "WARNING",
            code: code.to_owned(),
            message,
            path: Some(path),
        }
    }
}

/// `dump_diagnostics`: the JSON stored on Implementation and Blok rows.
pub fn dump_diagnostics(diagnostics: &[Diagnostic]) -> Value {
    serde_json::to_value(diagnostics).expect("diagnostics serialize")
}

/// A registered `Module`, as the checks read it.
#[derive(Debug, Clone)]
pub struct Catalog {
    pub id: i64,
    pub name: String,
    pub components: Vec<CatalogComponentInputModel>,
    pub operations: Vec<CatalogOperationInputModel>,
}

impl Catalog {
    /// Whether an app registered it: one only created by naming it (no components, no
    /// operations) checks nothing (`Module.is_registered`).
    pub fn is_registered(&self) -> bool {
        !self.components.is_empty() || !self.operations.is_empty()
    }

    fn from_row(
        id: i64,
        name: String,
        components: Value,
        operations: Value,
    ) -> Result<Self, String> {
        fn parse<T: serde::de::DeserializeOwned>(
            value: Value,
            what: &str,
            name: &str,
        ) -> Result<T, String> {
            serde_json::from_value(value).map_err(|e| {
                format!(
                    "catalog {}: stored {what} do not parse: {e}",
                    repr_str(name)
                )
            })
        }
        Ok(Self {
            id,
            components: parse(components, "components", &name)?,
            operations: parse(operations, "operations", &name)?,
            name,
        })
    }

    /// The named catalog of the organization, if registered.
    pub async fn find(
        conn: &mut PgConnection,
        name: &str,
        organization: i64,
    ) -> Result<Option<Self>, String> {
        let row: Option<(i64, String, Value, Value)> = sqlx::query_as(
            "SELECT id, name, components, operations FROM facade_module WHERE name = $1 AND organization_id = $2",
        )
        .bind(name)
        .bind(organization)
        .fetch_optional(conn)
        .await
        .map_err(|e| e.to_string())?;
        row.map(|(id, name, components, operations)| {
            Self::from_row(id, name, components, operations)
        })
        .transpose()
    }

    /// The named catalog of the organization, created empty if it does not exist yet
    /// (`Module.objects.get_or_create`).
    pub async fn get_or_create(
        conn: &mut PgConnection,
        name: &str,
        organization: i64,
    ) -> Result<Self, String> {
        sqlx::query(
            "INSERT INTO facade_module (name, organization_id)
             VALUES ($1, $2) ON CONFLICT (organization_id, name) DO NOTHING",
        )
        .bind(name)
        .bind(organization)
        .execute(&mut *conn)
        .await
        .map_err(|e| e.to_string())?;
        Ok(Self::find(conn, name, organization)
            .await?
            .expect("the catalog was just ensured"))
    }
}

type Operations = BTreeMap<String, CatalogOperationInputModel>;
type Components = HashMap<String, CatalogComponentInputModel>;

/// The operations in force: base plus every catalog's (`resolve_operations`). Two catalogs that
/// define one differently are a `catalog_conflict`; the first keeps it.
fn resolve_operations(catalogs: &[Catalog]) -> (Operations, Vec<Diagnostic>) {
    let mut operations = base_operations();
    let base: std::collections::HashSet<String> = operations.keys().cloned().collect();
    let mut provider: HashMap<&str, &str> = HashMap::new();
    let mut conflicts = vec![];
    for catalog in catalogs {
        for operation in &catalog.operations {
            // Base wins: a catalog registered before an operation joined base still lists it.
            if base.contains(&operation.name) {
                continue;
            }
            if let (Some(previous), Some(first)) = (
                operations.get(&operation.name),
                provider.get(operation.name.as_str()),
            ) {
                if previous != operation {
                    conflicts.push(Diagnostic::warning(
                        CATALOG_CONFLICT,
                        format!(
                            "operation {} is defined differently by catalogs {} and {}",
                            repr_str(&operation.name),
                            repr_str(first),
                            repr_str(&catalog.name)
                        ),
                        catalog.name.clone(),
                    ));
                }
                continue;
            }
            operations.insert(operation.name.clone(), operation.clone());
            provider.entry(&operation.name).or_insert(&catalog.name);
        }
    }
    (operations, conflicts)
}

/// The components in force, `None` while no catalog registered any (`resolve_components`). Two
/// catalogs that define one differently are a `catalog_conflict`; the first keeps it.
fn resolve_components(catalogs: &[Catalog]) -> (Option<Components>, Vec<Diagnostic>) {
    // The base catalog's control flow, then the union of what the catalogs registered.
    let mut components: Components = base_components().into_iter().collect();
    let mut provider: HashMap<&str, &str> = HashMap::new();
    let base: std::collections::HashSet<String> = components.keys().cloned().collect();
    let mut registered = false;
    let mut conflicts = vec![];
    for catalog in catalogs {
        if catalog.components.is_empty() {
            continue;
        }
        registered = true;
        for component in &catalog.components {
            // Base wins, as for operations: an older registration may still list it.
            if base.contains(&component.name) {
                continue;
            }
            if let Some(previous) = components.get(&component.name) {
                if previous != component {
                    conflicts.push(Diagnostic::warning(
                        CATALOG_CONFLICT,
                        format!(
                            "component {} is defined differently by catalogs {} and {}",
                            repr_str(&component.name),
                            repr_str(provider[component.name.as_str()]),
                            repr_str(&catalog.name)
                        ),
                        catalog.name.clone(),
                    ));
                }
                continue;
            }
            components.insert(component.name.clone(), component.clone());
            provider.entry(&component.name).or_insert(&catalog.name);
        }
    }
    (registered.then_some(components), conflicts)
}

fn catalog_label(catalogs: &[Catalog]) -> String {
    std::iter::once(BASE_CATALOG_ID)
        .chain(catalogs.iter().map(|c| c.name.as_str()))
        .collect::<Vec<_>>()
        .join(" + ")
}

fn py_list(items: &[&str]) -> String {
    repr(&Value::Array(
        items
            .iter()
            .map(|s| Value::String((*s).to_owned()))
            .collect(),
    ))
}

/// A known operation takes exactly its argument keys; an unknown one, or other keys, are
/// warnings (`_check_call`).
fn check_call(
    call: &UtilCallInputModel,
    operations: &Operations,
    label: &str,
    owner: &str,
) -> Vec<Diagnostic> {
    let Some(spec) = operations.get(&call.operation) else {
        return vec![Diagnostic::warning(
            UNKNOWN_OPERATION,
            format!(
                "{owner}: operation {} is not provided by the catalog ({label}); the UI cannot evaluate this call until it is registered",
                repr_str(&call.operation)
            ),
            owner.to_owned(),
        )];
    };
    let mut diagnostics = vec![];
    let accepted: HashSet<&str> = spec.arguments.iter().map(|a| a.key.as_str()).collect();
    let passed: HashSet<&str> = call
        .arguments
        .iter()
        .flatten()
        .filter_map(|a| a.key.as_deref())
        .collect();
    let mut unknown: Vec<&str> = passed.difference(&accepted).copied().collect();
    unknown.sort();
    if !unknown.is_empty() {
        diagnostics.push(Diagnostic::warning(
            OPERATION_ARGUMENTS,
            format!(
                "{owner}: operation {} does not accept arguments {}",
                repr_str(&call.operation),
                py_list(&unknown)
            ),
            owner.to_owned(),
        ));
    }
    let mut missing: Vec<&str> = spec
        .arguments
        .iter()
        .filter(|a| a.required && !passed.contains(a.key.as_str()))
        .map(|a| a.key.as_str())
        .collect();
    missing.sort();
    if !missing.is_empty() {
        diagnostics.push(Diagnostic::warning(
            OPERATION_ARGUMENTS,
            format!(
                "{owner}: operation {} requires arguments {}",
                repr_str(&call.operation),
                py_list(&missing)
            ),
            owner.to_owned(),
        ));
    }
    diagnostics
}

fn check_calls<'a>(
    calls: impl IntoIterator<Item = &'a UtilCallInputModel>,
    operations: &Operations,
    label: &str,
    owner: &str,
) -> Vec<Diagnostic> {
    let mut diagnostics = vec![];
    for call in calls {
        for candidate in std::iter::once(call).chain(iter_util_calls(call.arguments.as_deref())) {
            diagnostics.extend(check_call(candidate, operations, label, owner));
        }
    }
    diagnostics
}

fn prop_calls(prop: &ComponentPropInputModel) -> Vec<&UtilCallInputModel> {
    let mut calls: Vec<&UtilCallInputModel> = prop.util_call.iter().collect();
    if let Some(call) = &prop.agent_call {
        calls.extend(iter_util_calls(call.arguments.as_deref()));
    }
    calls
}

/// One component node: its structure against the registered specs, its calls against the
/// operations (`_check_component`). What the catalog says of it is a warning; only an ASSET
/// prop not naming an asset refuses.
fn check_component(
    component: &str,
    props: Option<&[ComponentPropInputModel]>,
    has_children: bool,
    specs: Option<&Components>,
    operations: &Operations,
    label: &str,
    owner: &str,
) -> Result<Vec<Diagnostic>, String> {
    let mut diagnostics = vec![];
    let finding =
        |code: &'static str, message: String| Diagnostic::warning(code, message, owner.to_owned());
    let spec = specs.and_then(|specs| specs.get(component));
    if specs.is_some() {
        match spec {
            None => diagnostics.push(finding(
                UNKNOWN_COMPONENT,
                format!(
                    "{owner}: component {} is not registered in catalog ({label})",
                    repr_str(component)
                ),
            )),
            Some(spec) => {
                if has_children && !spec.accepts_children {
                    diagnostics.push(finding(
                        CHILDREN_NOT_ACCEPTED,
                        format!(
                            "{owner}: component {} does not accept children",
                            repr_str(component)
                        ),
                    ));
                }
                let known: HashSet<&str> = spec.props.iter().map(|p| p.key.as_str()).collect();
                let present: HashSet<&str> = props
                    .unwrap_or_default()
                    .iter()
                    .map(|p| p.key.as_str())
                    .collect();
                let mut unknown: Vec<&str> = present.difference(&known).copied().collect();
                unknown.sort();
                if !unknown.is_empty() {
                    diagnostics.push(finding(
                        UNKNOWN_PROP,
                        format!(
                            "{owner}: component {} has no props {}",
                            repr_str(component),
                            py_list(&unknown)
                        ),
                    ));
                }
                let mut missing: Vec<&str> = spec
                    .props
                    .iter()
                    .filter(|p| p.required && !present.contains(p.key.as_str()))
                    .map(|p| p.key.as_str())
                    .collect();
                missing.sort();
                missing.dedup();
                if !missing.is_empty() {
                    diagnostics.push(finding(
                        MISSING_PROP,
                        format!(
                            "{owner}: component {} requires props {}",
                            repr_str(component),
                            py_list(&missing)
                        ),
                    ));
                }
            }
        }
    }
    for prop in props.unwrap_or_default() {
        let prop_owner = format!("prop {} of {owner}", repr_str(&prop.key));
        if let Some(spec) = spec {
            let kind = spec
                .props
                .iter()
                .find(|p| p.key == prop.key)
                .map(|p| p.kind);
            if kind == Some(CatalogValueKind::CALLBACK)
                && prop.agent_call.is_none()
                && prop.util_call.is_none()
            {
                diagnostics.push(Diagnostic::warning(
                    UNBOUND_CALLBACK,
                    format!(
                        "{prop_owner} is a CALLBACK prop and must be bound via agent_call or util_call"
                    ),
                    prop_owner.clone(),
                ));
            }
            check_asset_prop(kind, prop, &prop_owner)?;
        }
        diagnostics.extend(check_calls(
            prop_calls(prop),
            operations,
            label,
            &prop_owner,
        ));
    }
    Ok(diagnostics)
}

/// An ASSET prop names a file an app registered: it is bound to `<dependency>.assets.<name>`
/// (`_check_asset_prop`).
fn check_asset_prop(
    kind: Option<CatalogValueKind>,
    prop: &ComponentPropInputModel,
    prop_owner: &str,
) -> Result<(), String> {
    if kind != Some(CatalogValueKind::ASSET) {
        return Ok(());
    }
    let named = prop
        .dynamic_value
        .as_ref()
        .and_then(|d| d.path.as_deref())
        .and_then(blok_asset_path);
    if named.is_none() {
        return Err(format!(
            "{prop_owner} is an ASSET prop and must name an asset the app registered, as in {}=\"@self.assets.<name>\"",
            prop.key
        ));
    }
    Ok(())
}

/// `Anchor` and `Route` read the agents a run got, so only an action's own blok holds them;
/// `ask` a task's open ask, so only a blok that has a task (`check_run_components`).
pub fn check_run_components(
    components: &[ComponentNodeInputModel],
    kind: &str,
) -> Result<(), String> {
    for node in iter_component_nodes(Some(components)) {
        let run = matches!(node.component.as_str(), ANCHOR_COMPONENT | ROUTE_COMPONENT);
        if run && kind != "action" {
            return Err(format!(
                "component {}: a {} reads the agents of a run, so only an action's own blok holds one",
                repr_str(&node.id),
                repr_str(&node.component)
            ));
        }
        if node.component == ASK_COMPONENT && kind != "action" && kind != "task" {
            return Err(format!(
                "component {}: an 'ask' shows a task's open ask, so only a task's or an action's blok holds one",
                repr_str(&node.id)
            ));
        }
    }
    Ok(())
}

/// What a blok of `catalog` is checked against (`blok_view_for`): the spec's catalogs plus what
/// `catalog` registered beyond them (a component it defines differently from a standard catalog
/// is drawn as it defines it, `catalog.shadow` where a tree uses it). Unknown components are
/// findings once the catalog has registered any (the Orkestrator's always).
pub fn blok_view_for(catalog: &Catalog) -> BlokView {
    let complete = catalog.name == ORKESTRATOR_MODULE || !catalog.components.is_empty();
    // A registered component says which catalog it came from, unless it names its module.
    let components: Vec<CatalogComponentInputModel> = catalog
        .components
        .iter()
        .cloned()
        .map(|mut component| {
            component
                .catalog
                .get_or_insert_with(|| catalog.name.clone());
            component
        })
        .collect();
    blok_view(
        Some((&catalog.name, &components)),
        &catalog.operations,
        complete,
    )
}

/// `module.unknown` (a warning): the blok names a module that is not the Orkestrator's and that
/// nothing registered in the organization, so its own components are not checked. `None` for a
/// known module.
pub async fn module_unknown(
    conn: &mut PgConnection,
    catalog: &Catalog,
    organization: i64,
) -> Result<Option<Diagnostic>, sqlx::Error> {
    if catalog.name == ORKESTRATOR_MODULE || catalog.is_registered() {
        return Ok(None);
    }
    let mut modules: Vec<String> = sqlx::query_scalar(
        "SELECT name FROM facade_module
          WHERE organization_id = $1 AND (components <> '[]'::jsonb OR operations <> '[]'::jsonb)",
    )
    .bind(organization)
    .fetch_all(conn)
    .await?;
    modules.push(ORKESTRATOR_MODULE.to_owned());
    modules.sort();
    modules.dedup();
    let fields = [
        ("module", Value::String(catalog.name.clone())),
        ("modules", Value::String(modules.join(", "))),
    ];
    Ok(Some(diagnostic_of(&Finding::of(
        "module.unknown",
        None,
        &fields,
    ))))
}

/// A blok checked against its catalog: its warnings, and its scope resolved (its locals'
/// kinds), whose locals are stored as the blok's `resolved_locals`.
#[derive(Debug, Clone)]
pub struct CheckedBlok {
    pub diagnostics: Vec<Diagnostic>,
    pub scope: BlokScope,
}

/// A finding as a row stores it.
pub fn diagnostic_of(finding: &Finding) -> Diagnostic {
    Diagnostic {
        level: if finding.is_error() {
            "ERROR"
        } else {
            "WARNING"
        },
        code: finding.code.clone(),
        message: finding.message.clone(),
        path: finding.path.clone(),
    }
}

/// A v2 blok tree against what its blok declares (`scope`) and its catalog's view
/// (`validate_blok_against_catalog`): its scope resolved first (`resolve_scope`), then refused
/// on its first error (the finding, with its code); its warnings and resolved scope returned.
pub fn validate_blok_against_catalog(
    catalog: &Catalog,
    components: Option<&[ComponentNodeInputModel]>,
    scope: &BlokScope,
) -> Result<CheckedBlok, String> {
    validate_blok_with_arg_ports(catalog, components, scope, &ArgPorts::new())
}

/// [`validate_blok_against_catalog`], its locals resolved with `arg_ports` too: the full arg
/// ports of the actions the blok's agent registers (`self`), as the SDK resolves them.
pub fn validate_blok_with_arg_ports(
    catalog: &Catalog,
    components: Option<&[ComponentNodeInputModel]>,
    scope: &BlokScope,
    arg_ports: &ArgPorts,
) -> Result<CheckedBlok, String> {
    let roots = components.unwrap_or_default();
    let [tree] = roots else {
        return Err(format!("a blok has exactly one root, not {}", roots.len()));
    };
    check_run_components(roots, &scope.kind)?;
    let view = blok_view_for(catalog);
    let resolved = resolve_scope(tree, scope, Some(&view), arg_ports);
    let findings = check_blok_tree(tree, &resolved, Some(&view));
    if let Some(error) = findings.iter().find(|f| f.is_error()) {
        return Err(error.to_string());
    }
    Ok(CheckedBlok {
        diagnostics: findings.iter().map(diagnostic_of).collect(),
        scope: resolved,
    })
}

/// A widget of a definition, assign or return side.
enum Widget<'a> {
    Assign(&'a AssignWidgetInputModel),
    Return(&'a ReturnWidgetInputModel),
}

/// Every widget of a definition with its owner label (`iter_definition_widgets`).
fn definition_widgets(definition: &DefinitionInputModel) -> Vec<(String, Widget<'_>)> {
    fn walk_args<'a>(
        ports: &'a [ArgPortInputModel],
        prefix: &str,
        out: &mut Vec<(String, Widget<'a>)>,
    ) {
        for port in ports {
            let owner = format!("{prefix} port {}", port.key);
            if let Some(widget) = &port.widget {
                for (depth, widget) in widget.chain().into_iter().enumerate() {
                    let label = if depth == 0 {
                        owner.clone()
                    } else {
                        format!("{owner} fallback {depth}")
                    };
                    out.push((label, Widget::Assign(widget)));
                    if let AssignWidgetInputModel::Search(search) = widget {
                        walk_args(
                            search.filters.as_deref().unwrap_or_default(),
                            &format!("{owner} filter"),
                            out,
                        );
                    }
                }
            }
            walk_args(port.children.as_deref().unwrap_or_default(), prefix, out);
        }
    }
    fn walk_returns<'a>(
        ports: &'a [ReturnPortInputModel],
        prefix: &str,
        out: &mut Vec<(String, Widget<'a>)>,
    ) {
        for port in ports {
            if let Some(widget) = &port.widget {
                out.push((
                    format!("{prefix} port {}", port.key),
                    Widget::Return(widget),
                ));
            }
            walk_returns(port.children.as_deref().unwrap_or_default(), prefix, out);
        }
    }
    let prefix = format!("Definition {}", definition.key);
    let mut out = vec![];
    walk_args(&definition.args, &prefix, &mut out);
    walk_returns(&definition.returns, &prefix, &mut out);
    out
}

/// Widgets are checked like blok components (`validate_widgets_against_catalogs`).
fn validate_widgets_against_catalogs(
    catalogs: &[Catalog],
    definition: &DefinitionInputModel,
) -> Result<Vec<Diagnostic>, String> {
    // Operation conflicts are reported with the definition's calls (`collect_diagnostics`).
    let (operations, _) = resolve_operations(catalogs);
    let (specs, mut diagnostics) = resolve_components(catalogs);
    let label = catalog_label(catalogs);
    for (owner, widget) in definition_widgets(definition) {
        let owner = format!("widget of {owner}");
        let custom = match widget {
            Widget::Assign(AssignWidgetInputModel::Custom(w)) => {
                Some((&w.component, w.props.as_deref()))
            }
            Widget::Return(ReturnWidgetInputModel::Custom(w)) => {
                Some((&w.component, w.props.as_deref()))
            }
            _ => None,
        };
        if let Some((component, props)) = custom {
            diagnostics.extend(check_component(
                component,
                props,
                false,
                specs.as_ref(),
                &operations,
                &label,
                &owner,
            )?);
        }
        // `iter_widget_calls`: a STATE_CHOICE's pointer and accessors.
        if let Widget::Assign(AssignWidgetInputModel::StateChoice(w)) = widget {
            let calls = w.state_call.iter().chain(
                w.state_accessors
                    .iter()
                    .flatten()
                    .filter_map(|a| a.call.as_ref()),
            );
            diagnostics.extend(check_calls(calls, &operations, &label, &owner));
        }
    }
    Ok(diagnostics)
}

/// Every effect and validator call of a definition plus optimistic pointers (`iter_definition_calls`).
fn definition_calls<'a>(
    definition: &'a DefinitionInputModel,
    optimistics: Option<&'a [OptimisticInputModel]>,
) -> Vec<&'a UtilCallInputModel> {
    fn walk_args<'a>(ports: &'a [ArgPortInputModel], out: &mut Vec<&'a UtilCallInputModel>) {
        for port in ports {
            out.extend(port.validators.iter().flatten().map(|v| &v.call));
            out.extend(port.effects.iter().flatten().map(|e| &e.call));
            walk_args(port.children.as_deref().unwrap_or_default(), out);
        }
    }
    fn walk_returns<'a>(ports: &'a [ReturnPortInputModel], out: &mut Vec<&'a UtilCallInputModel>) {
        for port in ports {
            out.extend(port.effects.iter().flatten().map(|e| &e.call));
            walk_returns(port.children.as_deref().unwrap_or_default(), out);
        }
    }
    let mut out = vec![];
    walk_args(&definition.args, &mut out);
    walk_returns(&definition.returns, &mut out);
    for group in &definition.port_groups {
        out.extend(group.effects.iter().flatten().map(|e| &e.call));
    }
    out.extend(
        optimistics
            .unwrap_or_default()
            .iter()
            .filter_map(|o| o.path_call.as_ref()),
    );
    out
}

fn unknown_catalog(definition: &DefinitionInputModel, name: &str, reason: &str) -> Diagnostic {
    let owner = format!("Definition {}", definition.key);
    Diagnostic::warning(
        UNKNOWN_CATALOG,
        format!(
            "{owner}: catalog {} was not applied: {reason}",
            repr_str(name)
        ),
        owner,
    )
}

/// The catalogs a definition opted into, and a warning per name that resolves to nothing
/// (`catalogs_for_definition`).
pub async fn catalogs_for_definition(
    conn: &mut PgConnection,
    definition: &DefinitionInputModel,
    organization: i64,
) -> Result<(Vec<Catalog>, Vec<Diagnostic>), String> {
    let mut catalogs = vec![];
    let mut diagnostics = vec![];
    let mut seen = HashSet::new();
    for name in definition.catalogs.iter().flatten() {
        if !seen.insert(name.as_str()) {
            continue;
        }
        if let Some(version) = base_version_named(name) {
            if version != BASE_CATALOG_VERSION {
                diagnostics.push(unknown_catalog(
                    definition,
                    name,
                    &format!("this server provides {BASE_CATALOG_ID}"),
                ));
            }
            continue;
        }
        match Catalog::find(conn, name, organization).await? {
            Some(catalog) => catalogs.push(catalog),
            None => diagnostics.push(unknown_catalog(
                definition,
                name,
                "it is not registered in this organization",
            )),
        }
    }
    Ok((catalogs, diagnostics))
}

/// A definition's calls and widgets against its catalogs; the diagnostics to store
/// (`_collect_diagnostics`).
pub async fn collect_diagnostics(
    conn: &mut PgConnection,
    definition: &DefinitionInputModel,
    organization: i64,
    optimistics: Option<&[OptimisticInputModel]>,
) -> Result<Vec<Diagnostic>, String> {
    let (catalogs, unapplied) = catalogs_for_definition(conn, definition, organization).await?;
    let (operations, mut diagnostics) = resolve_operations(&catalogs);
    let label = catalog_label(&catalogs);
    diagnostics.extend(check_calls(
        definition_calls(definition, optimistics),
        &operations,
        &label,
        &format!("Definition {}", definition.key),
    ));
    diagnostics.extend(validate_widgets_against_catalogs(&catalogs, definition)?);
    diagnostics.extend(unapplied);
    for diagnostic in &diagnostics {
        tracing::warn!("{}", diagnostic.message);
    }
    Ok(diagnostics)
}
