//! Blok manifests, port calls and port widgets checked against the base catalog and the
//! organization's UI catalogs (`facade/catalog_validation.py`).
//!
//! Two kinds of finding: hard errors (`Err`, the registration is refused) for a known operation
//! called wrongly, an unknown component once a catalog has registered components, or two catalogs
//! disagreeing; warnings ([`Diagnostic`], stored on the row) for an operation or catalog nobody
//! provides yet, so UI apps can roll out operations before agents are redeployed.

use std::collections::{BTreeMap, HashMap, HashSet};

use rekuest_core::catalogs::{
    base_operations, base_version_named, BASE_CATALOG_ID, BASE_CATALOG_VERSION,
};
use rekuest_core::enums::CatalogValueKind;
use rekuest_core::inputs::{
    iter_component_nodes, iter_util_calls, ArgPortInputModel, AssignWidgetInputModel,
    CatalogComponentInputModel, CatalogOperationInputModel, ComponentNodeInputModel,
    ComponentPropInputModel, DefinitionInputModel, OptimisticInputModel, ReturnPortInputModel,
    ReturnWidgetInputModel, UtilCallInputModel,
};
use rekuest_core::pyjson::{repr, repr_str};
use serde::Serialize;
use serde_json::Value;
use sqlx::PgConnection;

pub const UNKNOWN_OPERATION: &str = "unknown_operation";
pub const UNKNOWN_CATALOG: &str = "unknown_catalog";

/// A non-fatal registration finding (`DiagnosticModel`), dumped as stored on the row.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Diagnostic {
    pub level: &'static str,
    pub code: &'static str,
    pub message: String,
    pub path: Option<String>,
}

impl Diagnostic {
    fn warning(code: &'static str, message: String, path: String) -> Self {
        Self {
            level: "WARNING",
            code,
            message,
            path: Some(path),
        }
    }
}

/// `dump_diagnostics`: the JSON stored on Implementation and Blok rows.
pub fn dump_diagnostics(diagnostics: &[Diagnostic]) -> Value {
    serde_json::to_value(diagnostics).expect("diagnostics serialize")
}

/// A registered `UICatalog`, as the checks read it.
#[derive(Debug, Clone)]
pub struct Catalog {
    pub id: i64,
    pub name: String,
    pub components: Vec<CatalogComponentInputModel>,
    pub operations: Vec<CatalogOperationInputModel>,
}

impl Catalog {
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
            "SELECT id, name, components, operations FROM facade_uicatalog WHERE name = $1 AND organization_id = $2",
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
    /// (`UICatalog.objects.get_or_create`).
    pub async fn get_or_create(
        conn: &mut PgConnection,
        name: &str,
        organization: i64,
    ) -> Result<Self, String> {
        sqlx::query(
            "INSERT INTO facade_uicatalog (name, organization_id)
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

/// The operations in force: base plus every catalog's; two catalogs may only agree (`resolve_operations`).
fn resolve_operations(catalogs: &[Catalog]) -> Result<Operations, String> {
    let mut operations = base_operations();
    let mut provider: HashMap<&str, &str> = HashMap::new();
    for catalog in catalogs {
        for operation in &catalog.operations {
            if let (Some(previous), Some(first)) = (
                operations.get(&operation.name),
                provider.get(operation.name.as_str()),
            ) {
                if previous != operation {
                    return Err(format!(
                        "operation {} is defined differently by catalogs {} and {}",
                        repr_str(&operation.name),
                        repr_str(first),
                        repr_str(&catalog.name)
                    ));
                }
            }
            operations.insert(operation.name.clone(), operation.clone());
            provider.entry(&operation.name).or_insert(&catalog.name);
        }
    }
    Ok(operations)
}

/// The components in force, `None` while no catalog registered any (`resolve_components`).
fn resolve_components(catalogs: &[Catalog]) -> Result<Option<Components>, String> {
    let mut components: Components = HashMap::new();
    let mut provider: HashMap<&str, &str> = HashMap::new();
    let mut registered = false;
    for catalog in catalogs {
        if catalog.components.is_empty() {
            continue;
        }
        registered = true;
        for component in &catalog.components {
            if let Some(previous) = components.get(&component.name) {
                if previous != component {
                    return Err(format!(
                        "component {} is defined differently by catalogs {} and {}",
                        repr_str(&component.name),
                        repr_str(provider[component.name.as_str()]),
                        repr_str(&catalog.name)
                    ));
                }
            }
            components.insert(component.name.clone(), component.clone());
            provider.entry(&component.name).or_insert(&catalog.name);
        }
    }
    Ok(registered.then_some(components))
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

/// A known operation takes exactly its argument keys; an unknown one is a warning (`_check_call`).
fn check_call(
    call: &UtilCallInputModel,
    operations: &Operations,
    label: &str,
    owner: &str,
) -> Result<Option<Diagnostic>, String> {
    let Some(spec) = operations.get(&call.operation) else {
        return Ok(Some(Diagnostic::warning(
            UNKNOWN_OPERATION,
            format!(
                "{owner}: operation {} is not provided by the catalog ({label}); the UI cannot evaluate this call until it is registered",
                repr_str(&call.operation)
            ),
            owner.to_owned(),
        )));
    };
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
        return Err(format!(
            "{owner}: operation {} does not accept arguments {}",
            repr_str(&call.operation),
            py_list(&unknown)
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
        return Err(format!(
            "{owner}: operation {} requires arguments {}",
            repr_str(&call.operation),
            py_list(&missing)
        ));
    }
    Ok(None)
}

fn check_calls<'a>(
    calls: impl IntoIterator<Item = &'a UtilCallInputModel>,
    operations: &Operations,
    label: &str,
    owner: &str,
) -> Result<Vec<Diagnostic>, String> {
    let mut diagnostics = vec![];
    for call in calls {
        for candidate in std::iter::once(call).chain(iter_util_calls(call.arguments.as_deref())) {
            diagnostics.extend(check_call(candidate, operations, label, owner)?);
        }
    }
    Ok(diagnostics)
}

fn prop_calls(prop: &ComponentPropInputModel) -> Vec<&UtilCallInputModel> {
    let mut calls: Vec<&UtilCallInputModel> = prop.util_call.iter().collect();
    if let Some(call) = &prop.agent_call {
        calls.extend(iter_util_calls(call.arguments.as_deref()));
    }
    calls
}

/// One component node: its structure against the registered specs, its calls against the
/// operations (`_check_component`).
fn check_component(
    component: &str,
    props: Option<&[ComponentPropInputModel]>,
    has_children: bool,
    specs: Option<&Components>,
    operations: &Operations,
    label: &str,
    owner: &str,
) -> Result<Vec<Diagnostic>, String> {
    let spec = specs.and_then(|specs| specs.get(component));
    if specs.is_some() {
        let Some(spec) = spec else {
            return Err(format!(
                "{owner}: component {} is not registered in catalog ({label})",
                repr_str(component)
            ));
        };
        if has_children && !spec.accepts_children {
            return Err(format!(
                "{owner}: component {} does not accept children",
                repr_str(component)
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
            return Err(format!(
                "{owner}: component {} has no props {}",
                repr_str(component),
                py_list(&unknown)
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
            return Err(format!(
                "{owner}: component {} requires props {}",
                repr_str(component),
                py_list(&missing)
            ));
        }
    }
    let mut diagnostics = vec![];
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
                return Err(format!(
                    "{prop_owner} is a CALLBACK prop and must be bound via agent_call or util_call"
                ));
            }
        }
        diagnostics.extend(check_calls(
            prop_calls(prop),
            operations,
            label,
            &prop_owner,
        )?);
    }
    Ok(diagnostics)
}

/// Every component, prop and operation a tree uses is provided (`validate_components_against_catalogs`).
pub fn validate_components_against_catalogs(
    catalogs: &[Catalog],
    components: Option<&[ComponentNodeInputModel]>,
) -> Result<Vec<Diagnostic>, String> {
    let operations = resolve_operations(catalogs)?;
    let specs = resolve_components(catalogs)?;
    let label = catalog_label(catalogs);
    let mut diagnostics = vec![];
    for node in iter_component_nodes(components) {
        diagnostics.extend(check_component(
            &node.component,
            node.props.as_deref(),
            node.children.as_ref().is_some_and(|c| !c.is_empty()),
            specs.as_ref(),
            &operations,
            &label,
            &format!("component {}", repr_str(&node.id)),
        )?);
    }
    Ok(diagnostics)
}

/// `validate_manifest_against_catalog`: a blok manifest against its one catalog.
pub fn validate_manifest_against_catalog(
    catalog: &Catalog,
    components: Option<&[ComponentNodeInputModel]>,
) -> Result<Vec<Diagnostic>, String> {
    validate_components_against_catalogs(std::slice::from_ref(catalog), components)
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
    let operations = resolve_operations(catalogs)?;
    let specs = resolve_components(catalogs)?;
    let label = catalog_label(catalogs);
    let mut diagnostics = vec![];
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
            diagnostics.extend(check_calls(calls, &operations, &label, &owner)?);
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
    let operations = resolve_operations(&catalogs)?;
    let label = catalog_label(&catalogs);
    let mut diagnostics = check_calls(
        definition_calls(definition, optimistics),
        &operations,
        &label,
        &format!("Definition {}", definition.key),
    )?;
    diagnostics.extend(validate_widgets_against_catalogs(&catalogs, definition)?);
    diagnostics.extend(unapplied);
    for diagnostic in &diagnostics {
        tracing::warn!("{}", diagnostic.message);
    }
    Ok(diagnostics)
}
