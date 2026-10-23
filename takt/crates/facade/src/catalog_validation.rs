//! Blok manifests, port calls and port widgets checked against the base catalog and the
//! organization's UI catalogs (`facade/catalog_validation.py`).
//!
//! Two kinds of finding: hard errors (`Err`, the registration is refused) for a known operation
//! called wrongly, an unknown component once a catalog has registered components, or two catalogs
//! disagreeing; warnings ([`Diagnostic`], stored on the row) for an operation or catalog nobody
//! provides yet, so UI apps can roll out operations before agents are redeployed.

use std::collections::{BTreeMap, HashMap, HashSet};

use rekuest_core::catalogs::{
    base_components, base_operations, base_version_named, BASE_CATALOG_ID, BASE_CATALOG_VERSION,
};
use rekuest_core::enums::{CatalogValueKind, ComponentContext};
use rekuest_core::inputs::{
    blok_asset_path, iter_util_calls, ArgPortInputModel, AssignWidgetInputModel,
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
/// The one component a blok of a context may have as its root, for the contexts that have one.
pub fn context_root(context: ComponentContext) -> Option<&'static str> {
    match context {
        ComponentContext::SCENE => Some("Scene"),
        ComponentContext::LOGO => Some("SVG"),
        ComponentContext::ACTION_LOGO => Some("ActionLogo"),
        ComponentContext::HTML => None,
    }
}

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

/// Every component, prop and operation a tree uses is provided (`validate_components_against_catalogs`).
/// `context` is the blok's own: a scene blok has one root, the scene, a logo blok one `SVG` and an
/// action logo one `ActionLogo`, whatever is registered. Both logos are held to the size of an
/// icon, and an action logo to the palette.
pub fn validate_components_against_catalogs(
    catalogs: &[Catalog],
    components: Option<&[ComponentNodeInputModel]>,
    context: ComponentContext,
    demo_state: Option<&serde_json::Map<String, Value>>,
) -> Result<Vec<Diagnostic>, String> {
    let (operations, mut diagnostics) = resolve_operations(catalogs);
    let (specs, conflicts) = resolve_components(catalogs);
    diagnostics.extend(conflicts);
    let label = catalog_label(catalogs);
    if let Some(required) = context_root(context) {
        let kind = context_name(context);
        let roots = components.unwrap_or_default();
        let [root] = roots else {
            return Err(format!(
                "a {kind} blok has exactly one root, a {}, not {} roots",
                repr_str(required),
                roots.len()
            ));
        };
        if root.component != required {
            return Err(format!(
                "component {}: the root of a {kind} blok must be a {}, not {}",
                repr_str(&root.id),
                repr_str(required),
                repr_str(&root.component)
            ));
        }
    }
    if matches!(
        context,
        ComponentContext::LOGO | ComponentContext::ACTION_LOGO
    ) {
        check_logo_complexity(components.unwrap_or_default(), demo_state)?;
    }
    if context == ComponentContext::ACTION_LOGO {
        check_logo_palette(components.unwrap_or_default(), demo_state)?;
    }
    let mut ancestors = vec![];
    visit_components(
        components.unwrap_or_default(),
        &mut ancestors,
        ComponentContext::HTML,
        specs.as_ref(),
        &operations,
        &label,
        &mut diagnostics,
    )?;
    Ok(diagnostics)
}

/// The tree in pre-order, carrying each node's ancestors and the context in force for it.
fn visit_components<'a>(
    nodes: &'a [ComponentNodeInputModel],
    ancestors: &mut Vec<&'a str>,
    context: ComponentContext,
    specs: Option<&Components>,
    operations: &Operations,
    label: &str,
    diagnostics: &mut Vec<Diagnostic>,
) -> Result<(), String> {
    for node in nodes {
        let owner = format!("component {}", repr_str(&node.id));
        diagnostics.extend(check_component(
            &node.component,
            node.props.as_deref(),
            node.children.as_ref().is_some_and(|c| !c.is_empty()),
            specs,
            operations,
            label,
            &owner,
        )?);
        let inner = check_placement(
            &node.component,
            ancestors,
            context,
            specs,
            &owner,
            diagnostics,
        );
        ancestors.push(&node.component);
        visit_components(
            node.children.as_deref().unwrap_or_default(),
            ancestors,
            inner,
            specs,
            operations,
            label,
            diagnostics,
        )?;
        ancestors.pop();
    }
    Ok(())
}

fn context_name(context: ComponentContext) -> &'static str {
    match context {
        ComponentContext::HTML => "html",
        ComponentContext::SCENE => "scene",
        ComponentContext::LOGO => "logo",
        ComponentContext::ACTION_LOGO => "action_logo",
    }
}

/// Where a component sits in the tree, against what it declares; the context of its children
/// (`_check_placement`). A tree starts in the html context, and a component whose
/// `children_context` differs from its own bridges two renderers. One that `inherits_context`
/// sits in whichever context is in force and passes it on.
fn check_placement(
    component: &str,
    ancestors: &[&str],
    context: ComponentContext,
    specs: Option<&Components>,
    owner: &str,
    diagnostics: &mut Vec<Diagnostic>,
) -> ComponentContext {
    let Some(spec) = specs.and_then(|specs| specs.get(component)) else {
        return context;
    };
    let mut misplaced = |message: String| {
        diagnostics.push(Diagnostic::warning(
            MISPLACED_COMPONENT,
            message,
            owner.to_owned(),
        ))
    };
    if !spec.inherits_context && spec.context != context {
        misplaced(format!(
            "{owner}: component {} is a {} component and cannot be placed in the {} context",
            repr_str(component),
            context_name(spec.context),
            context_name(context)
        ));
    }
    if !spec.downstream_of.is_empty()
        && !spec
            .downstream_of
            .iter()
            .any(|name| ancestors.contains(&name.as_str()))
    {
        let mut required: Vec<&str> = spec.downstream_of.iter().map(String::as_str).collect();
        required.sort();
        required.dedup();
        misplaced(format!(
            "{owner}: component {} must be inside one of {}",
            repr_str(component),
            py_list(&required)
        ));
    }
    let mut forbidden: Vec<&str> = spec
        .not_downstream_of
        .iter()
        .map(String::as_str)
        .filter(|name| ancestors.contains(name))
        .collect();
    forbidden.sort();
    forbidden.dedup();
    if !forbidden.is_empty() {
        misplaced(format!(
            "{owner}: component {} must not be inside {}",
            repr_str(component),
            py_list(&forbidden)
        ));
    }
    if spec.inherits_context {
        return context;
    }
    spec.children_context.unwrap_or(spec.context)
}

/// `validate_manifest_against_catalog`: a blok manifest against its one catalog.
pub fn validate_manifest_against_catalog(
    catalog: &Catalog,
    components: Option<&[ComponentNodeInputModel]>,
    context: ComponentContext,
    demo_state: Option<&serde_json::Map<String, Value>>,
) -> Result<Vec<Diagnostic>, String> {
    validate_components_against_catalogs(
        std::slice::from_ref(catalog),
        components,
        context,
        demo_state,
    )
}

/// What a logo blok may hold at most: a logo is an icon.
pub const LOGO_MAX_NODES: usize = 200;
pub const LOGO_MAX_DEPTH: usize = 12;
pub const LOGO_MAX_PATH_DATA: usize = 16 * 1024;
pub const LOGO_MAX_FOREACH_ITEMS: usize = 64;
const LOGO_PATH_DATA_PROPS: [&str; 2] = ["d", "points"];

/// `check_logo_complexity`: how many components, how deep, how much path data, and how many
/// items a foreach draws of the blok's demo state (a list it does not find there draws nothing).
pub fn check_logo_complexity(
    components: &[ComponentNodeInputModel],
    demo_state: Option<&serde_json::Map<String, Value>>,
) -> Result<(), String> {
    fn visit(
        node: &ComponentNodeInputModel,
        depth: usize,
        demo_state: Option<&serde_json::Map<String, Value>>,
        nodes: &mut usize,
        path_data: &mut usize,
    ) -> Result<(), String> {
        *nodes += 1;
        if depth > LOGO_MAX_DEPTH {
            return Err(format!(
                "component {}: a logo nests at most {LOGO_MAX_DEPTH} deep",
                repr_str(&node.id)
            ));
        }
        for prop in node.props.iter().flatten() {
            if LOGO_PATH_DATA_PROPS.contains(&prop.key.as_str()) {
                if let Some(Value::String(data)) = &prop.static_value {
                    *path_data += data.chars().count();
                }
            }
            if node.component == "foreach" && prop.key == "items" {
                let items = demo_items(prop, demo_state);
                if items > LOGO_MAX_FOREACH_ITEMS {
                    return Err(format!(
                        "component {}: a foreach in a logo draws at most {LOGO_MAX_FOREACH_ITEMS} items, its demo state has {items}",
                        repr_str(&node.id)
                    ));
                }
            }
        }
        for child in node.children.iter().flatten() {
            visit(child, depth + 1, demo_state, nodes, path_data)?;
        }
        Ok(())
    }

    let (mut nodes, mut path_data) = (0, 0);
    for root in components {
        visit(root, 1, demo_state, &mut nodes, &mut path_data)?;
    }
    if nodes > LOGO_MAX_NODES {
        return Err(format!(
            "a logo has at most {LOGO_MAX_NODES} components, this one {nodes}"
        ));
    }
    if path_data > LOGO_MAX_PATH_DATA {
        return Err(format!(
            "a logo holds at most {LOGO_MAX_PATH_DATA} characters of path data, this one {path_data}"
        ));
    }
    Ok(())
}

/// What an action logo paints with: the viewer's palette, or a `url(#id)` to a gradient or clip path.
pub const LOGO_PAINT_TOKENS: [&str; 7] = [
    "ink",
    "muted",
    "brand",
    "brand-strong",
    "brand-soft",
    "surface",
    "none",
];
const LOGO_PAINT_PROPS: [&str; 3] = ["fill", "stroke", "stopColor"];

/// `check_logo_palette`: an action logo paints only with `LOGO_PAINT_TOKENS` or a `url(#id)`; a
/// static paint as written, a bound one as the demo state resolves it, for every item when it reads
/// a foreach's let. A path the demo state does not hold draws nothing and is not checked.
pub fn check_logo_palette(
    components: &[ComponentNodeInputModel],
    demo_state: Option<&serde_json::Map<String, Value>>,
) -> Result<(), String> {
    fn visit(
        node: &ComponentNodeInputModel,
        demo_state: Option<&serde_json::Map<String, Value>>,
        scope: &HashMap<String, Vec<Value>>,
    ) -> Result<(), String> {
        for prop in node.props.iter().flatten() {
            if !LOGO_PAINT_PROPS.contains(&prop.key.as_str()) {
                continue;
            }
            let values = if let Some(value) = prop.static_value.as_ref().filter(|v| !v.is_null()) {
                vec![value.clone()]
            } else if let Some(path) = prop
                .dynamic_value
                .as_ref()
                .and_then(|d| d.path.as_deref())
                .filter(|p| !p.is_empty())
            {
                resolve(path, demo_state, scope)
            } else {
                vec![]
            };
            for value in values {
                if !is_logo_paint(&value) {
                    let shown = match &value {
                        Value::String(text) => repr_str(text),
                        other => other.to_string(),
                    };
                    return Err(format!(
                        "component {}: a logo paints with {}|url(#…), not {shown} in {}",
                        repr_str(&node.id),
                        LOGO_PAINT_TOKENS.join("|"),
                        repr_str(&prop.key)
                    ));
                }
            }
        }
        let mut inner = None;
        if node.component == "foreach" {
            let props = || node.props.iter().flatten();
            let let_name = props()
                .find(|p| p.key == "let")
                .and_then(|p| p.declares_value.clone());
            let items = props()
                .find(|p| p.key == "items")
                .and_then(|p| p.dynamic_value.as_ref())
                .and_then(|d| d.path.clone())
                .filter(|p| !p.is_empty());
            if let (Some(let_name), Some(items)) = (let_name, items) {
                let mut scoped = scope.clone();
                let drawn = resolve(&items, demo_state, scope)
                    .into_iter()
                    .filter_map(|found| match found {
                        Value::Array(items) => Some(items),
                        _ => None,
                    })
                    .flatten()
                    .collect();
                scoped.insert(let_name, drawn);
                inner = Some(scoped);
            }
        }
        for child in node.children.iter().flatten() {
            visit(child, demo_state, inner.as_ref().unwrap_or(scope))?;
        }
        Ok(())
    }

    for root in components {
        visit(root, demo_state, &HashMap::new())?;
    }
    Ok(())
}

/// A palette token, or a `url(#id)` reference to a gradient or clip path.
pub fn is_logo_paint(value: &Value) -> bool {
    let Some(paint) = value.as_str().map(str::trim) else {
        return false;
    };
    LOGO_PAINT_TOKENS.contains(&paint) || (paint.starts_with("url(#") && paint.ends_with(')'))
}

/// What `path` holds: one value from the demo state, or one per item of a foreach's let.
fn resolve(
    path: &str,
    demo_state: Option<&serde_json::Map<String, Value>>,
    scope: &HashMap<String, Vec<Value>>,
) -> Vec<Value> {
    let segments: Vec<&str> = path.split(['.', '/']).filter(|s| !s.is_empty()).collect();
    let Some((first, rest)) = segments.split_first() else {
        return vec![];
    };
    let (roots, rest): (Vec<Value>, &[&str]) = match scope.get(*first) {
        Some(items) => (items.clone(), rest),
        None => (
            vec![Value::Object(demo_state.cloned().unwrap_or_default())],
            &segments[..],
        ),
    };
    roots
        .into_iter()
        .filter_map(|root| {
            let mut value = root;
            for segment in rest {
                value = value.as_object()?.get(*segment)?.clone();
            }
            Some(value)
        })
        .collect()
}

/// How many items a foreach's `items` gives in the demo state; 0 when it names none.
fn demo_items(
    prop: &ComponentPropInputModel,
    demo_state: Option<&serde_json::Map<String, Value>>,
) -> usize {
    if let Some(Value::Array(items)) = &prop.static_value {
        return items.len();
    }
    let Some(path) = prop.dynamic_value.as_ref().and_then(|d| d.path.as_deref()) else {
        return 0;
    };
    let Some(state) = demo_state else { return 0 };
    let mut value = &Value::Null;
    let mut first = true;
    for segment in path.split(['.', '/']).filter(|s| !s.is_empty()) {
        let next = if first {
            state.get(segment)
        } else {
            value.as_object().and_then(|object| object.get(segment))
        };
        first = false;
        match next {
            Some(found) => value = found,
            None => return 0,
        }
    }
    value.as_array().map_or(0, Vec::len)
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
