//! Definitions and bloks validated against the base catalog plus the UI catalogs they name, at
//! registration: unknown operations and catalogs are stored warnings, argument mismatches and
//! unregistered components refuse the whole registration. Ported from the Python server's
//! `tests/models/test_definition_catalog.py` when registration moved here; the messages are the
//! Python server's. Needs `TAKT_TEST_DATABASE_URL`; skipped without it.

use authentikate::base_models::StaticToken;
use facade::registration::implement_agent;
use rekuest_core::inputs::ImplementAgentInputModel;
use serde_json::{json, Value};
use sqlx::PgPool;

const CLAMP: &str = r#"{"name": "clamp", "arguments": [{"key": "a", "kind": "FLOAT"}, {"key": "b", "kind": "FLOAT"}], "returns": "FLOAT"}"#;

struct Tenant {
    db: PgPool,
    agent: i64,
    organization: i64,
}

async fn tenant() -> Option<Tenant> {
    let url = std::env::var("TAKT_TEST_DATABASE_URL").ok()?;
    let db = PgPool::connect(&url).await.unwrap();
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "catalog-tests", "org": format!("o-{unique}"),
        "client_id": format!("c-{unique}"), "client_app": format!("a-{unique}"), "client_release": "1",
    }))
    .unwrap();
    let id =
        authentikate::expand::expand_token_context(&db, &spec.to_token(chrono::Utc::now(), "raw"))
            .await
            .unwrap();
    let agent = facade::registration::ensure_agent(&db, id.client, id.user, id.organization)
        .await
        .unwrap();
    Some(Tenant {
        db,
        agent,
        organization: id.organization,
    })
}

impl Tenant {
    async fn catalog(&self, name: &str, operations: Value, components: Value) {
        sqlx::query(
            "INSERT INTO facade_uicatalog (name, components, operations, widget_defaults, organization_id)
             VALUES ($1, $2, $3, '[]', $4)
             ON CONFLICT (organization_id, name) DO UPDATE SET components = excluded.components, operations = excluded.operations",
        )
        .bind(name)
        .bind(components)
        .bind(operations)
        .bind(self.organization)
        .execute(&self.db)
        .await
        .unwrap();
    }

    /// Register; the implementation's stored diagnostics, or the refusal.
    async fn register(&self, payload: Value) -> Result<Vec<Value>, String> {
        let mut model: ImplementAgentInputModel = serde_json::from_value(payload).unwrap();
        model.validate().map_err(|e| e.0)?;
        let mut tx = self.db.begin().await.unwrap();
        implement_agent(&mut tx, self.agent, &model)
            .await
            .map_err(|e| e.to_string())?;
        tx.commit().await.unwrap();
        let diagnostics: Value = sqlx::query_scalar("SELECT diagnostics FROM facade_implementation WHERE agent_id = $1 AND interface = 'scan'")
            .bind(self.agent)
            .fetch_one(&self.db)
            .await
            .unwrap();
        Ok(diagnostics.as_array().cloned().unwrap_or_default())
    }

    async fn count(&self, table: &str) -> i64 {
        let column = if table == "facade_blok" {
            "organization_id"
        } else {
            "agent_id"
        };
        let owner = if table == "facade_blok" {
            self.organization
        } else {
            self.agent
        };
        sqlx::query_scalar(&format!("SELECT count(*) FROM {table} WHERE {column} = $1"))
            .bind(owner)
            .fetch_one(&self.db)
            .await
            .unwrap()
    }
}

fn catalogs(named: &[&str]) -> Value {
    if named.is_empty() {
        Value::Null
    } else {
        json!(named)
    }
}

/// One implementation whose arg port validator calls `operation(value, 0)` with argument keys `keys`.
fn payload(named: &[&str], operation: &str, keys: (&str, &str), bloks: Value) -> Value {
    json!({
        "implementations": [{
            "interface": "scan",
            "definition": {"key": "scan", "version": "1", "name": "Scan", "kind": "FUNCTION", "catalogs": catalogs(named),
                "args": [{"key": "exposure", "kind": "FLOAT", "nullable": false, "validators": [{
                    "call": {"operation": operation, "arguments": [{"key": keys.0, "value_path": "/value"}, {"key": keys.1, "value_literal": 0}]},
                    "source": format!("{operation}(value, 0)")}]}],
                "returns": []},
        }],
        "bloks": bloks,
    })
}

fn call(named: &[&str], operation: &str) -> Value {
    payload(named, operation, ("a", "b"), json!([]))
}

/// One implementation whose single arg port (and optionally its return) carries `widget`.
fn widget(named: &[&str], widget: Value, return_widget: Option<Value>, port_kind: &str) -> Value {
    let returns = match return_widget {
        Some(w) => json!([{"key": "out", "kind": "STRING", "nullable": false, "widget": w}]),
        None => json!([]),
    };
    let identifier = if port_kind == "STRUCTURE" {
        json!("@x/thing")
    } else {
        Value::Null
    };
    json!({"implementations": [{"interface": "scan", "definition": {
        "key": "scan", "version": "1", "name": "Scan", "kind": "FUNCTION", "catalogs": catalogs(named),
        "args": [{"key": "exposure", "kind": port_kind, "identifier": identifier, "nullable": false, "widget": widget}],
        "returns": returns}}]})
}

fn codes(diagnostics: &[Value]) -> Vec<&str> {
    diagnostics
        .iter()
        .map(|d| d["code"].as_str().unwrap())
        .collect()
}

fn custom_knob() -> Value {
    json!({"kind": "CUSTOM", "component": "Knob", "props": [{"key": "value", "dynamic_value": {"path": "/value"}}]})
}

#[tokio::test]
async fn unknown_operation_is_stored_as_a_warning() {
    let Some(t) = tenant().await else { return };
    t.catalog(
        "electron",
        json!([serde_json::from_str::<Value>(CLAMP).unwrap()]),
        json!([]),
    )
    .await;
    let diagnostics = t.register(call(&["electron"], "fizz")).await.unwrap();
    assert_eq!(codes(&diagnostics), ["unknown_operation"]);
    let message = diagnostics[0]["message"].as_str().unwrap();
    assert!(
        message.contains("'fizz'") && message.contains("base@1 + electron"),
        "{message}"
    );
    assert_eq!(diagnostics[0]["level"], "WARNING");
}

#[tokio::test]
async fn base_and_extension_operations_are_accepted() {
    let Some(t) = tenant().await else { return };
    t.catalog(
        "electron",
        json!([serde_json::from_str::<Value>(CLAMP).unwrap()]),
        json!([]),
    )
    .await;
    assert!(t
        .register(call(&["electron"], "gt"))
        .await
        .unwrap()
        .is_empty());
    assert!(t
        .register(call(&["electron"], "clamp"))
        .await
        .unwrap()
        .is_empty());
    assert_eq!(t.count("facade_implementation").await, 1);
}

#[tokio::test]
async fn base_applies_without_a_catalog() {
    let Some(t) = tenant().await else { return };
    assert!(t.register(call(&[], "gt")).await.unwrap().is_empty());
    let unknown = t.register(call(&["nonexistent"], "gt")).await.unwrap();
    assert_eq!(codes(&unknown), ["unknown_catalog"]);
    assert!(unknown[0]["message"]
        .as_str()
        .unwrap()
        .contains("'nonexistent'"));
    t.catalog("empty", json!([]), json!([])).await;
    assert!(t.register(call(&["empty"], "gt")).await.unwrap().is_empty());
}

#[tokio::test]
async fn a_base_argument_mismatch_refuses_the_registration() {
    let Some(t) = tenant().await else { return };
    let between = t.register(call(&[], "between")).await.unwrap_err();
    assert!(
        between.contains("operation 'between' does not accept arguments ['a', 'b']"),
        "{between}"
    );
    let gt = t
        .register(payload(&[], "gt", ("a", "c"), json!([])))
        .await
        .unwrap_err();
    assert!(
        gt.contains("operation 'gt' does not accept arguments ['c']"),
        "{gt}"
    );
    assert_eq!(t.count("facade_implementation").await, 0);
}

#[tokio::test]
async fn a_warning_is_replaced_on_re_registration() {
    let Some(t) = tenant().await else { return };
    assert_eq!(
        codes(&t.register(call(&["electron"], "clamp")).await.unwrap()),
        ["unknown_operation", "unknown_catalog"]
    );
    t.catalog(
        "electron",
        json!([serde_json::from_str::<Value>(CLAMP).unwrap()]),
        json!([]),
    )
    .await;
    assert!(t
        .register(call(&["electron"], "clamp"))
        .await
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn agent_declared_bloks_are_validated_against_their_catalog() {
    let Some(t) = tenant().await else { return };
    t.catalog(
        "electron",
        json!([]),
        json!([{"name": "Box", "props": [{"key": "v", "kind": "ANY"}]}]),
    )
    .await;
    let knob = json!([{"key": "panel", "catalog": "electron", "components": [{"id": "root", "component": "Knob"}]}]);
    let refused = t
        .register(payload(&[], "gt", ("a", "b"), knob))
        .await
        .unwrap_err();
    assert!(
        refused.contains("component 'Knob' is not registered"),
        "{refused}"
    );
    assert_eq!(t.count("facade_blok").await, 0);

    let boxed = json!([{"key": "panel", "catalog": "electron", "components": [
        {"id": "root", "component": "Box", "props": [{"key": "v", "util_call": {"operation": "fizz"}}]}]}]);
    t.register(payload(&[], "gt", ("a", "b"), boxed))
        .await
        .unwrap();
    let (catalog, diagnostics): (String, Value) = sqlx::query_as(
        "SELECT c.name, b.diagnostics FROM facade_blok b JOIN facade_uicatalog c ON c.id = b.catalog_id
          WHERE b.organization_id = $1 AND b.name = 'panel'",
    )
    .bind(t.organization)
    .fetch_one(&t.db)
    .await
    .unwrap();
    assert_eq!(catalog, "electron");
    assert_eq!(
        codes(diagnostics.as_array().unwrap()),
        ["unknown_operation"]
    );
}

#[tokio::test]
async fn a_blok_naming_no_catalog_is_checked_against_the_standard_one() {
    let Some(t) = tenant().await else { return };
    t.catalog("orkestrator", json!([]), json!([{"name": "Box"}]))
        .await;
    let knob = json!([{"key": "panel", "components": [{"id": "root", "component": "Knob"}]}]);
    let refused = t
        .register(payload(&[], "gt", ("a", "b"), knob))
        .await
        .unwrap_err();
    assert!(
        refused.contains("component 'Knob' is not registered"),
        "{refused}"
    );

    let boxed = json!([{"key": "panel", "components": [{"id": "root", "component": "Box"}]}]);
    t.register(payload(&[], "gt", ("a", "b"), boxed))
        .await
        .unwrap();
}

#[tokio::test]
async fn components_are_held_to_their_declared_placement() {
    let Some(t) = tenant().await else { return };
    t.catalog(
        "electron",
        json!([]),
        json!([
            {"name": "Box"},
            {"name": "Label", "accepts_children": false},
            {"name": "Scene", "children_context": "SCENE", "not_downstream_of": ["Scene"]},
            {"name": "Model", "context": "SCENE", "downstream_of": ["Scene"]},
            {"name": "Move", "context": "SCENE", "downstream_of": ["Model", "Robot"], "accepts_children": false},
            {"name": "Popout", "context": "SCENE", "children_context": "HTML", "downstream_of": ["Model", "Robot"]},
        ]),
    )
    .await;
    fn node(component: &str, children: Vec<Value>) -> Value {
        static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let id = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        json!({"id": format!("{component}{id}"), "component": component, "children": children})
    }
    let blok = |tree: Value| json!([{"key": "panel", "catalog": "electron", "components": [tree]}]);

    let accepted = node(
        "Box",
        vec![node(
            "Scene",
            vec![node(
                "Model",
                vec![
                    node("Move", vec![]),
                    node("Popout", vec![node("Box", vec![node("Label", vec![])])]),
                ],
            )],
        )],
    );
    t.register(payload(&[], "gt", ("a", "b"), blok(accepted)))
        .await
        .unwrap();

    for (tree, message) in [
        (
            node("Model", vec![]),
            "component 'Model' is a scene component and cannot be placed in the html context",
        ),
        (
            node("Scene", vec![node("Label", vec![])]),
            "component 'Label' is a html component and cannot be placed in the scene context",
        ),
        (
            node("Scene", vec![node("Move", vec![])]),
            "component 'Move' must be inside one of ['Model', 'Robot']",
        ),
        (
            node(
                "Scene",
                vec![node(
                    "Model",
                    vec![node("Popout", vec![node("Scene", vec![])])],
                )],
            ),
            "component 'Scene' must not be inside ['Scene']",
        ),
    ] {
        let refused = t
            .register(payload(&[], "gt", ("a", "b"), blok(tree)))
            .await
            .unwrap_err();
        assert!(refused.contains(message), "{refused}");
    }
}

#[tokio::test]
async fn a_logo_blok_is_one_svg_and_control_flow_takes_its_context() {
    let Some(t) = tenant().await else { return };
    t.catalog(
        "electron",
        json!([]),
        json!([
            {"name": "Box"},
            {"name": "foreach", "inherits_context": true},
            {"name": "SVG", "children_context": "LOGO", "not_downstream_of": ["SVG"]},
            {"name": "Path", "context": "LOGO", "downstream_of": ["SVG"], "accepts_children": false},
            {"name": "Scene", "children_context": "SCENE", "not_downstream_of": ["Scene"]},
            {"name": "Model", "context": "SCENE", "downstream_of": ["Scene"]},
        ]),
    )
    .await;
    let blok = |context: &str, components: Value| json!([{"key": "view", "catalog": "electron", "context": context, "components": components}]);
    let looped = |child: Value| json!({"id": "loop", "component": "foreach", "children": [child]});
    let path = json!({"id": "p", "component": "Path"});

    for bloks in [
        blok(
            "LOGO",
            json!([{"id": "logo", "component": "SVG", "children": [looped(path.clone())]}]),
        ),
        blok(
            "SCENE",
            json!([{"id": "scene", "component": "Scene", "children": [
            looped(json!({"id": "m", "component": "Model"}))]}]),
        ),
        blok(
            "HTML",
            json!([{"id": "root", "component": "Box", "children": [
            {"id": "logo", "component": "SVG", "children": [path.clone()]}]}]),
        ),
    ] {
        t.register(payload(&[], "gt", ("a", "b"), bloks))
            .await
            .unwrap();
    }

    for (bloks, message) in [
        (
            blok("LOGO", json!([path.clone()])),
            "the root of a logo blok must be a 'SVG', not 'Path'",
        ),
        (
            blok(
                "LOGO",
                json!([{"id": "logo", "component": "SVG"}, {"id": "other", "component": "SVG"}]),
            ),
            "a logo blok has exactly one root, a 'SVG', not 2 roots",
        ),
        (
            blok(
                "LOGO",
                json!([looped(json!({"id": "logo", "component": "SVG"}))]),
            ),
            "the root of a logo blok must be a 'SVG', not 'foreach'",
        ),
        (
            blok(
                "HTML",
                json!([{"id": "root", "component": "Box", "children": [path.clone()]}]),
            ),
            "component 'Path' is a logo component and cannot be placed in the html context",
        ),
        (
            blok(
                "LOGO",
                json!([{"id": "logo", "component": "SVG", "children": [
                looped(json!({"id": "b", "component": "Box"}))]}]),
            ),
            "component 'Box' is a html component and cannot be placed in the logo context",
        ),
    ] {
        let refused = t
            .register(payload(&[], "gt", ("a", "b"), bloks))
            .await
            .unwrap_err();
        assert!(refused.contains(message), "{refused}");
    }
}

#[tokio::test]
async fn a_scene_blok_opens_a_scene_names_its_models_and_may_be_the_default() {
    let Some(t) = tenant().await else { return };
    t.catalog(
        "electron",
        json!([]),
        json!([
            {"name": "Box"},
            {"name": "Scene", "children_context": "SCENE", "not_downstream_of": ["Scene"]},
            {"name": "Model", "context": "SCENE", "downstream_of": ["Scene"], "props": [
                {"key": "src", "kind": "ASSET", "required": true}]},
        ]),
    )
    .await;
    let reference = format!("sha256:{}", "9f".repeat(32));
    // A model named by an asset path, in a blok that declares the dependency the path names.
    let scene = |key: &str, path: &str, extra: Value| {
        let mut blok = json!({"key": key, "catalog": "electron", "context": "SCENE",
            "dependencies": [{"key": "self"}], "components": [
            {"id": "scene", "component": "Scene", "children": [
                {"id": "model", "component": "Model", "props": [
                    {"key": "src", "dynamic_value": {"path": path}}]}]}]});
        blok.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        blok
    };
    let named = "self.assets.scope";
    let with_assets = |mut payload: Value| {
        payload["assets"] =
            json!([{"name": "scope", "kind": "MODEL_3D", "ref": reference, "size": 1024}]);
        payload
    };

    for (bloks, message) in [
        (
            json!([{"key": "view", "catalog": "electron", "context": "SCENE", "components": [{"id": "root", "component": "Box"}]}]),
            "the root of a scene blok must be a 'Scene', not 'Box'",
        ),
        (
            json!([{"key": "view", "catalog": "electron", "context": "SCENE", "components": [
                {"id": "root", "component": "Box", "children": [{"id": "scene", "component": "Scene"}]}]}]),
            "the root of a scene blok must be a 'Scene', not 'Box'",
        ),
        (
            // No component is registered in this catalog, so nothing else could tell a scene from a box.
            json!([{"key": "view", "catalog": "unregistered", "context": "SCENE", "components": [{"id": "root", "component": "Box"}]}]),
            "the root of a scene blok must be a 'Scene', not 'Box'",
        ),
        (
            json!([{"key": "view", "catalog": "electron", "context": "SCENE", "components": [
                {"id": "scene", "component": "Scene"}, {"id": "other", "component": "Scene"}]}]),
            "a scene blok has exactly one root, a 'Scene', not 2 roots",
        ),
        (
            json!([{"key": "view", "catalog": "electron", "context": "SCENE", "components": []}]),
            "a scene blok has exactly one root, a 'Scene', not 0 roots",
        ),
        (
            json!([{"key": "view", "catalog": "electron", "context": "SCENE", "components": [
                {"id": "scene", "component": "Scene", "children": [
                    {"id": "model", "component": "Model", "props": [{"key": "src", "static_value": reference}]}]}]}]),
            "is an ASSET prop and must name an asset",
        ),
        (
            json!([scene("view", "self.assets", json!({}))]),
            "is an ASSET prop and must name an asset",
        ),
        (
            json!([scene("view", "self.assets.absent", json!({}))]),
            "names the asset 'absent', which the agent does not declare (declares [\"scope\"])",
        ),
        (
            json!([scene("view", named, json!({"for_model": reference}))]),
            "only an html blok can decorate a model",
        ),
    ] {
        let refused = t
            .register(with_assets(payload(&[], "gt", ("a", "b"), bloks)))
            .await
            .unwrap_err();
        assert!(refused.contains(message), "{refused}");
    }
    for (assets, message) in [
        (
            json!([{"name": "my-scope", "kind": "MODEL_3D", "ref": reference, "size": 1}]),
            "is not an identifier",
        ),
        (
            json!([{"name": "scope", "kind": "MODEL_3D", "ref": "9f2c", "size": 1}]),
            "the reference must be 'sha256:'",
        ),
        (
            json!([{"name": "scope", "kind": "MODEL_3D", "ref": reference, "size": 1},
                {"name": "scope", "kind": "IMAGE", "ref": reference, "size": 1}]),
            "is declared twice",
        ),
        (
            json!([{"name": "scope", "kind": "MODEL_3D", "ref": reference}]),
            "an asset has a size",
        ),
        (
            json!([{"name": "scope", "kind": "MODEL_3D", "ref": reference, "source": "http://vendor.example/scope.glb"}]),
            "a source is an https URL",
        ),
    ] {
        let mut declaration = payload(&[], "gt", ("a", "b"), json!([]));
        declaration["assets"] = assets;
        let refused = t.register(declaration).await.unwrap_err();
        assert!(refused.contains(message), "{refused}");
    }

    let defaults = |t: &Tenant| {
        let db = t.db.clone();
        let organization = t.organization;
        async move {
            sqlx::query_as::<_, (String, String, Option<String>)>(
                "SELECT b.name, b.context, b.for_node FROM facade_materializedblok m JOIN facade_blok b ON b.id = m.blok_id
                  WHERE b.organization_id = $1 AND m.is_default ORDER BY b.name",
            )
            .bind(organization)
            .fetch_all(&db)
            .await
            .unwrap()
        }
    };
    let first = json!([
        scene("view", named, json!({"default": true})),
        {"key": "panel", "catalog": "electron", "default": true, "for_model": reference, "for_node": "turret",
         "components": [{"id": "root", "component": "Box"}]},
    ]);
    t.register(with_assets(payload(&[], "gt", ("a", "b"), first.clone())))
        .await
        .unwrap();
    let assets = |t: &Tenant| {
        let (db, agent) = (t.db.clone(), t.agent);
        async move {
            sqlx::query_as::<_, (String, String, String, i64)>(
                "SELECT name, kind, ref, size FROM facade_agentasset WHERE agent_id = $1 ORDER BY name",
            )
            .bind(agent)
            .fetch_all(&db)
            .await
            .unwrap()
        }
    };
    assert_eq!(
        assets(&t).await,
        [(
            "scope".to_owned(),
            "MODEL_3D".to_owned(),
            reference.clone(),
            1024
        )]
    );
    // A file the agent does not hold: it names where to fetch it, and no size.
    let mut sourced = payload(&[], "gt", ("a", "b"), first.clone());
    sourced["assets"] = json!([{"name": "scope", "kind": "MODEL_3D", "ref": reference,
        "source": "https://vendor.example/scope.glb"}]);
    t.register(sourced).await.unwrap();
    let named_source: (Option<i64>, Option<String>) = sqlx::query_as(
        "SELECT size, source FROM facade_agentasset WHERE agent_id = $1 AND name = 'scope'",
    )
    .bind(t.agent)
    .fetch_one(&t.db)
    .await
    .unwrap();
    assert_eq!(
        named_source,
        (None, Some("https://vendor.example/scope.glb".to_owned()))
    );
    t.register(with_assets(payload(&[], "gt", ("a", "b"), first.clone())))
        .await
        .unwrap();
    assert_eq!(assets(&t).await[0].3, 1024);
    // The blok's only use of its agent is the asset, and the key is still mapped to it.
    let mapped: Vec<String> = sqlx::query_scalar(
        "SELECT m.key FROM facade_blokagentmapping m JOIN facade_materializedblok b ON b.id = m.materialized_blok_id
          WHERE m.agent_id = $1 AND b.name = 'view'",
    )
    .bind(t.agent)
    .fetch_all(&t.db)
    .await
    .unwrap();
    assert_eq!(mapped, ["self"]);
    assert_eq!(
        defaults(&t).await,
        [
            (
                "panel".to_owned(),
                "HTML".to_owned(),
                Some("turret".to_owned())
            ),
            ("view".to_owned(), "SCENE".to_owned(), None)
        ]
    );

    // A newly declared default replaces the previous one of its context, and only that one.
    let second = json!([
        scene("view", "self.assets.stage", json!({})),
        scene("other_view", "self.assets.stage", json!({"default": true})),
        {"key": "panel", "catalog": "electron", "default": true, "components": [{"id": "root", "component": "Box"}]},
    ]);
    // A re-registration that names its file differently: the old name is gone, not kept.
    let mut renamed = payload(&[], "lt", ("a", "b"), second);
    renamed["assets"] =
        json!([{"name": "stage", "kind": "MODEL_3D", "ref": reference, "size": 2048}]);
    t.register(renamed).await.unwrap();
    let names: Vec<String> = defaults(&t).await.into_iter().map(|row| row.0).collect();
    assert_eq!(names, ["other_view", "panel"]);
    assert_eq!(
        assets(&t).await,
        [(
            "stage".to_owned(),
            "MODEL_3D".to_owned(),
            reference.clone(),
            2048
        )]
    );
}

#[tokio::test]
async fn multiple_catalogs_are_unioned_with_base() {
    let Some(t) = tenant().await else { return };
    t.catalog(
        "electron",
        json!([serde_json::from_str::<Value>(CLAMP).unwrap()]),
        json!([]),
    )
    .await;
    t.catalog(
        "web",
        json!([{"name": "fmt", "arguments": [{"key": "a", "kind": "ANY"}, {"key": "b", "kind": "ANY"}], "returns": "STRING"}]),
        json!([]),
    )
    .await;
    for operation in ["clamp", "fmt", "gt"] {
        assert!(
            t.register(call(&["electron", "web"], operation))
                .await
                .unwrap()
                .is_empty(),
            "{operation}"
        );
    }
    let refused = t
        .register(payload(&["electron", "web"], "fmt", ("x", "y"), json!([])))
        .await
        .unwrap_err();
    assert!(
        refused.contains("'fmt' does not accept arguments"),
        "{refused}"
    );
}

#[tokio::test]
async fn conflicting_catalogs_are_refused_and_identical_ones_tolerated() {
    let Some(t) = tenant().await else { return };
    let clamp: Value = serde_json::from_str(CLAMP).unwrap();
    t.catalog("electron", json!([clamp.clone()]), json!([]))
        .await;
    t.catalog("twin", json!([clamp.clone()]), json!([])).await;
    let mut rival = clamp.clone();
    rival["arguments"] = json!([{"key": "v", "kind": "FLOAT"}]);
    t.catalog("rival", json!([rival]), json!([])).await;
    assert!(t
        .register(call(&["electron", "twin"], "clamp"))
        .await
        .unwrap()
        .is_empty());
    let refused = t
        .register(call(&["electron", "rival"], "clamp"))
        .await
        .unwrap_err();
    assert!(
        refused.contains(
            "operation 'clamp' is defined differently by catalogs 'electron' and 'rival'"
        ),
        "{refused}"
    );
}

#[tokio::test]
async fn base_may_be_named_explicitly() {
    let Some(t) = tenant().await else { return };
    assert!(t
        .register(call(&["base", "base@1", "base@1"], "gt"))
        .await
        .unwrap()
        .is_empty());
    let other = t.register(call(&["base@2"], "gt")).await.unwrap();
    assert_eq!(codes(&other), ["unknown_catalog"]);
    assert!(other[0]["message"].as_str().unwrap().contains("base@1"));
}

#[tokio::test]
async fn a_custom_widget_component_is_checked_once_the_catalog_registers_components() {
    let Some(t) = tenant().await else { return };
    assert!(t
        .register(widget(&[], custom_knob(), None, "FLOAT"))
        .await
        .unwrap()
        .is_empty());
    t.catalog("electron", json!([]), json!([{"name": "Box"}]))
        .await;
    let refused = t
        .register(widget(&["electron"], custom_knob(), None, "FLOAT"))
        .await
        .unwrap_err();
    assert!(
        refused.contains(
            "widget of Definition scan port exposure: component 'Knob' is not registered"
        ),
        "{refused}"
    );
    assert_eq!(
        t.count("facade_implementation").await,
        1,
        "the earlier registration survived"
    );
    t.catalog(
        "electron",
        json!([]),
        json!([{"name": "Knob", "props": [{"key": "value", "kind": "FLOAT"}]}]),
    )
    .await;
    assert!(t
        .register(widget(&["electron"], custom_knob(), None, "FLOAT"))
        .await
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn widget_calls_are_checked_like_validator_calls() {
    let Some(t) = tenant().await else { return };
    let custom = json!({"kind": "CUSTOM", "component": "Knob", "props": [{"key": "label", "util_call": {"operation": "fizz"}}]});
    let diagnostics = t
        .register(widget(&[], custom, None, "FLOAT"))
        .await
        .unwrap();
    assert_eq!(codes(&diagnostics), ["unknown_operation"]);
    assert!(diagnostics[0]["path"]
        .as_str()
        .unwrap()
        .contains("widget of Definition scan port exposure"));

    let state = json!({"kind": "STATE_CHOICE", "state_call": {"operation": "buzz"},
                       "state_accessors": [{"option_key": "LABEL", "call": {"operation": "fizz"}}]});
    assert_eq!(
        codes(&t.register(widget(&[], state, None, "FLOAT")).await.unwrap()),
        ["unknown_operation", "unknown_operation"]
    );

    let wrong = json!({"kind": "CUSTOM", "component": "Knob", "props": [{"key": "label",
                       "util_call": {"operation": "gt", "arguments": [{"key": "left", "value_path": "/value"}]}}]});
    let refused = t
        .register(widget(&[], wrong, None, "FLOAT"))
        .await
        .unwrap_err();
    assert!(
        refused.contains("operation 'gt' does not accept arguments ['left']"),
        "{refused}"
    );
}

#[tokio::test]
async fn filter_ports_fallbacks_and_return_widgets_are_walked() {
    let Some(t) = tenant().await else { return };
    t.catalog("electron", json!([]), json!([{"name": "Box"}]))
        .await;
    let query = "query S($search: String, $values: [ID!], $f: String) { x }";
    let in_filter = json!({"kind": "SEARCH", "query": query, "ward": "mikro",
                           "filters": [{"key": "f", "kind": "STRING", "nullable": false, "widget": custom_knob()}]});
    let refused = t
        .register(widget(&["electron"], in_filter, None, "STRUCTURE"))
        .await
        .unwrap_err();
    assert!(
        refused.contains("widget of Definition scan port exposure filter port f: component 'Knob'"),
        "{refused}"
    );

    let mut in_fallback = custom_knob();
    in_fallback["component"] = json!("Box");
    in_fallback["props"] = json!([]);
    in_fallback["fallback"] = custom_knob();
    let refused = t
        .register(widget(&["electron"], in_fallback, None, "FLOAT"))
        .await
        .unwrap_err();
    assert!(
        refused.contains("widget of Definition scan port exposure fallback 1: component 'Knob'"),
        "{refused}"
    );

    let refused = t
        .register(widget(
            &["electron"],
            json!({"kind": "SLIDER"}),
            Some(json!({"kind": "CUSTOM", "component": "Gauge"})),
            "FLOAT",
        ))
        .await
        .unwrap_err();
    assert!(
        refused.contains("widget of Definition scan port out: component 'Gauge'"),
        "{refused}"
    );
    assert_eq!(t.count("facade_implementation").await, 0);
}

#[tokio::test]
async fn optimistic_pointer_calls_are_checked() {
    let Some(t) = tenant().await else { return };
    let mut payload = widget(&[], json!({"kind": "SLIDER"}), None, "FLOAT");
    payload["implementations"][0]["optimistics"] = json!([{"state": "stage", "path_call": {"operation": "fizz", "arguments": [{"key": "a", "value_path": "/args/axis"}]}}]);
    let diagnostics = t.register(payload).await.unwrap();
    assert_eq!(codes(&diagnostics), ["unknown_operation"]);
    assert!(diagnostics[0]["message"]
        .as_str()
        .unwrap()
        .contains("'fizz'"));
}

/// A malformed descriptor on a later implementation refuses the whole registration: nothing of
/// it lands (`test_implement_agent_rolls_back_on_malformed_descriptor`).
#[tokio::test]
async fn a_refused_registration_writes_nothing() {
    let Some(t) = tenant().await else { return };
    let declaration = json!({"implementations": [
        {"interface": "scan", "definition": {"key": "scan", "version": "1", "name": "Scan", "kind": "FUNCTION"}},
        {"interface": "zz_bad", "definition": {"key": "zz_bad", "version": "1", "name": "Bad", "kind": "FUNCTION",
            "args": [{"key": "x", "kind": "STRUCTURE", "identifier": "@x/y", "nullable": false,
                      "requires": [{"key": "$.", "operator": "EXISTS"}]}]}},
    ]});
    let refused = t.register(declaration).await.unwrap_err();
    assert!(refused.contains("Invalid descriptor key"), "{refused}");
    assert_eq!(t.count("facade_implementation").await, 0);
}

/// A dependency an implementation no longer declares is deleted with the next registration: an
/// assign resolves every dependency row it finds.
#[tokio::test]
async fn a_dependency_no_longer_declared_is_deleted() {
    let Some(t) = tenant().await else { return };
    let declaration = |dependencies: Value| {
        json!({"implementations": [{"interface": "scan", "dependencies": dependencies,
            "definition": {"key": "scan", "version": "1", "name": "Scan", "kind": "FUNCTION"}}]})
    };
    let keys = || async {
        sqlx::query_scalar::<_, String>(
            "SELECT d.key FROM facade_dependency d JOIN facade_implementation i ON i.id = d.implementation_id
              WHERE i.agent_id = $1 ORDER BY d.key",
        )
        .bind(t.agent)
        .fetch_all(&t.db)
        .await
        .unwrap()
    };
    t.register(declaration(
        json!([{"key": "camera"}, {"key": "stage", "optional": true}]),
    ))
    .await
    .unwrap();
    assert_eq!(keys().await, ["camera", "stage"]);
    t.register(declaration(json!([{"key": "stage"}])))
        .await
        .unwrap();
    assert_eq!(keys().await, ["stage"]);
    t.register(declaration(json!([]))).await.unwrap();
    assert!(keys().await.is_empty());
}

/// Declare, as a service's manifest would, that `identifier` is hosted and its objects carry `keys`.
async fn declare_structure(db: &PgPool, identifier: &str, keys: &[&str]) {
    sqlx::query(
        "WITH service AS (
             INSERT INTO facade_service (name) VALUES ('takt-tests')
             ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id),
         structure AS (
             INSERT INTO facade_structuredeclaration (service_id, identifier)
             SELECT id, $1 FROM service
             ON CONFLICT (identifier) DO UPDATE SET identifier = EXCLUDED.identifier RETURNING id)
         INSERT INTO facade_descriptor (structure_id, key, position)
         SELECT structure.id, key, position
           FROM structure, unnest($2::varchar[]) WITH ORDINALITY AS declared(key, position)
         ON CONFLICT (structure_id, key) DO NOTHING",
    )
    .bind(identifier)
    .bind(keys)
    .execute(db)
    .await
    .unwrap();
}

/// One action taking `port`, and returning `returns`.
fn constrained(port: Value, returns: Value) -> Value {
    json!({"implementations": [{"interface": "scan", "definition": {
        "key": "measure", "version": "1", "name": "Measure", "kind": "FUNCTION",
        "args": [port], "returns": returns}}]})
}

/// A port constrains only what the service hosting its structure declares: a mistyped key is
/// refused with what is declared, instead of never matching anything.
#[tokio::test]
async fn a_descriptor_no_service_declares_is_refused() {
    let Some(t) = tenant().await else { return };
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let image = format!("@d{unique}/image");
    let spaces = format!("@d{unique}/n_space_axes");
    let kind = format!("@d{unique}/value_kind");
    declare_structure(&t.db, &image, &[&spaces, &kind]).await;
    let port = |key: &str| {
        json!({"key": "image", "kind": "STRUCTURE", "identifier": image, "nullable": false,
                                  "requires": [{"key": key, "operator": "EQUALS", "value": 2}]})
    };

    let refused = t
        .register(constrained(
            port(&format!("@d{unique}/n_spce_axes")),
            json!([]),
        ))
        .await
        .unwrap_err();
    assert!(
        refused.contains(&format!("Port 'image' of 'measure' constrains the descriptor '@d{unique}/n_spce_axes', which no service declares for {image}. Declared: {spaces}, {kind}")),
        "{refused}"
    );
    assert_eq!(t.count("facade_implementation").await, 0);

    // Declared keys pass, on arguments and on returns; a declared key nothing computes
    // (provenance) is a key like any other here.
    let returns = json!([{"key": "labels", "kind": "STRUCTURE", "identifier": image, "nullable": false,
                          "provides": [{"key": kind, "operator": "EQUALS", "value": "categorical"}]}]);
    t.register(constrained(port(&spaces), returns))
        .await
        .unwrap();
    assert_eq!(t.count("facade_implementation").await, 1);
}

/// A key is declared per structure: another structure's key does not count, a structure no
/// service hosts has none, and a nested port is named by its path.
#[tokio::test]
async fn a_descriptor_is_declared_for_its_structure() {
    let Some(t) = tenant().await else { return };
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let (image, table) = (format!("@d{unique}/image"), format!("@d{unique}/table"));
    let (spaces, rows) = (
        format!("@d{unique}/n_space_axes"),
        format!("@d{unique}/n_rows"),
    );
    declare_structure(&t.db, &image, &[&spaces]).await;
    declare_structure(&t.db, &table, &[&rows]).await;
    let structure = |identifier: &str, key: &str| {
        json!({"key": "...", "kind": "STRUCTURE", "identifier": identifier, "nullable": false,
                                                        "requires": [{"key": key, "operator": "GTE", "value": 1}]})
    };
    let list = |child: Value, requires: Value| json!({"key": "items", "kind": "LIST", "nullable": false, "children": [child], "requires": requires});

    let refused = t
        .register(constrained(
            list(structure(&image, &rows), json!(null)),
            json!([]),
        ))
        .await
        .unwrap_err();
    assert!(refused.contains(&format!("Port 'items....' of 'measure' constrains the descriptor '{rows}', which no service declares for {image}")), "{refused}");

    let unhosted = format!("@d{unique}/nothing");
    let refused = t
        .register(constrained(
            list(structure(&unhosted, &spaces), json!(null)),
            json!([]),
        ))
        .await
        .unwrap_err();
    assert!(
        refused.contains("Declared: none (no service hosts this structure"),
        "{refused}"
    );

    // A list constrained as a whole is not one structure's: any declared key will do, an
    // undeclared one will not.
    let plain = json!({"key": "...", "kind": "STRUCTURE", "identifier": image, "nullable": false});
    let refused = t
        .register(constrained(
            list(
                plain.clone(),
                json!([{"key": format!("@d{unique}/made_up"), "operator": "EXISTS"}]),
            ),
            json!([]),
        ))
        .await
        .unwrap_err();
    assert!(
        refused.contains("which no service declares for any structure"),
        "{refused}"
    );
    t.register(constrained(
        list(plain, json!([{"key": rows, "operator": "EXISTS"}])),
        json!([]),
    ))
    .await
    .unwrap();
}

#[tokio::test]
async fn an_implementation_is_drawn_by_the_action_logo_blok_it_names() {
    let Some(t) = tenant().await else { return };
    t.catalog(
        "electron",
        json!([]),
        json!([
            {"name": "SVG", "children_context": "LOGO", "not_downstream_of": ["SVG", "ActionLogo"]},
            {"name": "ActionLogo", "children_context": "LOGO", "not_downstream_of": ["SVG", "ActionLogo"]},
            {"name": "Path", "context": "LOGO", "downstream_of": ["SVG", "ActionLogo"], "accepts_children": false,
             "props": [{"key": "d", "kind": "STRING"}, {"key": "fill", "kind": "STRING", "required": false}]},
            {"name": "foreach", "inherits_context": true, "props": [{"key": "items", "kind": "LIST"}]},
        ]),
    )
    .await;
    let logo = |key: &str, components: Value, demo_state: Value| json!({"key": key, "catalog": "electron", "context": "LOGO", "components": components, "demo_state": demo_state});
    let svg = |children: Value| json!([{"id": "logo", "component": "SVG", "children": children}]);
    let action_logo = |key: &str, children: Value, demo_state: Value| {
        json!({"key": key, "catalog": "electron", "context": "ACTION_LOGO",
        "components": [{"id": "logo", "component": "ActionLogo", "children": children}], "demo_state": demo_state})
    };
    let painted = |id: &str, fill: Value| json!({"id": id, "component": "Path", "props": [{"key": "d", "static_value": "M0 0"}, fill]});
    let path = |i: usize| json!({"id": format!("p{i}"), "component": "Path", "props": [{"key": "d", "static_value": "M0 0"}]});
    let drawn_by = |mut payload: Value, name: Option<&str>| {
        payload["implementations"][0]["logo"] = json!(name);
        payload
    };
    let logo_of_scan = || async {
        sqlx::query_scalar::<_, Option<String>>(
            "SELECT b.name FROM facade_implementation i LEFT JOIN facade_blok b ON b.id = i.logo_id
              WHERE i.agent_id = $1 AND i.interface = 'scan'",
        )
        .bind(t.agent)
        .fetch_one(&t.db)
        .await
        .unwrap()
    };

    let action_logo_of_scan = || async {
        sqlx::query_scalar::<_, Option<String>>(
            "SELECT b.name FROM facade_implementation i JOIN facade_action a ON a.id = i.action_id
               LEFT JOIN facade_blok b ON b.id = a.latest_logo_id
              WHERE i.agent_id = $1 AND i.interface = 'scan'",
        )
        .bind(t.agent)
        .fetch_one(&t.db)
        .await
        .unwrap()
    };

    let crop = action_logo("crop", json!([path(0)]), json!({"task": null}));
    t.register(drawn_by(
        payload(&[], "gt", ("a", "b"), json!([crop.clone()])),
        Some("crop"),
    ))
    .await
    .unwrap();
    assert_eq!(logo_of_scan().await.as_deref(), Some("crop"));
    // The action keeps the logo of its implementation last registered with one.
    assert_eq!(action_logo_of_scan().await.as_deref(), Some("crop"));
    // Registered again without one, the implementation is drawn by none.
    t.register(drawn_by(
        payload(&[], "gt", ("a", "b"), json!([crop.clone()])),
        None,
    ))
    .await
    .unwrap();
    assert_eq!(logo_of_scan().await, None);
    assert_eq!(action_logo_of_scan().await, None);

    let panel = json!({"key": "panel", "components": [{"id": "root", "component": "Box"}]});
    // An agent's logo keeps any SVG colour, but draws no action.
    let app = logo(
        "app",
        svg(json!([painted(
            "p",
            json!({"key": "fill", "static_value": "#fbbf24"})
        )])),
        json!({}),
    );
    for name in ["nowhere", "panel", "app"] {
        let refused = t
            .register(drawn_by(
                payload(
                    &[],
                    "gt",
                    ("a", "b"),
                    json!([crop.clone(), panel.clone(), app.clone()]),
                ),
                Some(name),
            ))
            .await
            .unwrap_err();
        assert!(
            refused.contains(&format!(
                "is drawn by the logo '{name}', which is not an action logo blok this agent declares"
            )),
            "{refused}"
        );
    }

    // An action logo paints with the palette: as written, and as its demo state resolves a binding.
    let quiet = action_logo(
        "quiet",
        json!([
            painted("soft", json!({"key": "fill", "static_value": "brand-soft"})),
            painted("glow", json!({"key": "fill", "static_value": "url(#glow)"})),
            painted(
                "tint",
                json!({"key": "fill", "dynamic_value": {"path": "task.tint"}})
            )
        ]),
        json!({"task": null}),
    );
    t.register(payload(&[], "gt", ("a", "b"), json!([quiet, app.clone()])))
        .await
        .unwrap();
    for (blok, message) in [
        (
            action_logo(
                "loud",
                json!([painted(
                    "p",
                    json!({"key": "fill", "static_value": "#fbbf24"})
                )]),
                json!({"task": null}),
            ),
            "not '#fbbf24' in 'fill'",
        ),
        (
            action_logo(
                "tinted",
                json!([painted(
                    "p",
                    json!({"key": "fill", "dynamic_value": {"path": "task.tint"}})
                )]),
                json!({"task": {"tint": "red"}}),
            ),
            "not 'red' in 'fill'",
        ),
        (
            json!({"key": "rooted", "catalog": "electron", "context": "ACTION_LOGO", "components": svg(json!([])), "demo_state": {}}),
            "the root of a action_logo blok must be a 'ActionLogo', not 'SVG'",
        ),
    ] {
        let refused = t
            .register(payload(&[], "gt", ("a", "b"), json!([blok])))
            .await
            .unwrap_err();
        assert!(refused.contains(message), "{refused}");
    }

    let lamps = |n: usize| json!({"lamps": (0..n).collect::<Vec<_>>()});
    let looped = json!([{"id": "loop", "component": "foreach", "props": [{"key": "items", "dynamic_value": {"path": "lamps"}}],
                         "children": [path(0)]}]);
    t.register(payload(
        &[],
        "gt",
        ("a", "b"),
        json!([logo("lamps", svg(looped.clone()), lamps(64))]),
    ))
    .await
    .unwrap();
    for (blok, message) in [
        (
            logo(
                "big",
                svg(json!((0..200).map(path).collect::<Vec<_>>())),
                json!({}),
            ),
            "a logo has at most 200 components, this one 201",
        ),
        (
            logo("lamps", svg(looped.clone()), lamps(65)),
            "a foreach in a logo draws at most 64 items, its demo state has 65",
        ),
        (
            logo(
                "long",
                svg(
                    json!([{"id": "p", "component": "Path", "props": [{"key": "d", "static_value": "M".repeat(16385)}]}]),
                ),
                json!({}),
            ),
            "a logo holds at most 16384 characters of path data, this one 16385",
        ),
    ] {
        let refused = t
            .register(payload(&[], "gt", ("a", "b"), json!([blok])))
            .await
            .unwrap_err();
        assert!(refused.contains(message), "{refused}");
    }
}

#[tokio::test]
async fn a_blok_may_be_registered_as_its_bsx_source() {
    let Some(t) = tenant().await else { return };
    t.catalog(
        "electron",
        json!([]),
        json!([{"name": "Box"}, {"name": "Label", "accepts_children": false, "props": [{"key": "text", "kind": "STRING"}]}]),
    )
    .await;
    let stored = || async {
        sqlx::query_scalar::<_, Value>(
            "SELECT components FROM facade_blok WHERE organization_id = $1 AND name = 'panel'",
        )
        .bind(t.organization)
        .fetch_one(&t.db)
        .await
        .unwrap()
    };
    let tree = json!([{"id": "Box", "component": "Box", "children": [
        {"id": "Box/Label[0]", "component": "Label", "props": [{"key": "text", "static_value": "hi"}]}]}]);

    t.register(payload(
        &[],
        "gt",
        ("a", "b"),
        json!([{"key": "panel", "catalog": "electron", "components": tree}]),
    ))
    .await
    .unwrap();
    let from_tree = stored().await;
    t.register(payload(&[], "gt", ("a", "b"), json!([{"key": "panel", "catalog": "electron", "source": "<Box><Label text=\"hi\" /></Box>"}])))
        .await
        .unwrap();
    assert_eq!(stored().await, from_tree);

    for (blok, message) in [
        (
            json!({"key": "panel", "components": tree, "source": "<Box />"}),
            "not both",
        ),
        (
            json!({"key": "panel"}),
            "a blok is sent as its components or as its BSX source",
        ),
        (
            json!({"key": "panel", "source": "<Box><Label></Box>"}),
            "Failed to parse BSX/XML at line 1",
        ),
        (
            json!({"key": "panel", "catalog": "electron", "source": "<Box><Knob /></Box>"}),
            "component 'Knob' is not registered",
        ),
    ] {
        let refused = t
            .register(payload(&[], "gt", ("a", "b"), json!([blok])))
            .await
            .unwrap_err();
        assert!(refused.contains(message), "{refused}");
    }
}
