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
            "INSERT INTO facade_module (name, components, operations, widget_defaults, organization_id)
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

    /// Register; what came of it: a refusal as `("refused", reason)`, else every finding stored on
    /// the implementation and on the bloks the agent declares, as `(code, message)`. What the UI
    /// catalog says of a blok or a widget is a finding, never a refusal.
    async fn findings(&self, payload: Value) -> Vec<(String, String)> {
        let implementation = match self.register(payload).await {
            Ok(diagnostics) => diagnostics,
            Err(reason) => return vec![("refused".into(), reason)],
        };
        let bloks: Vec<Value> = sqlx::query_scalar(
            "SELECT b.diagnostics FROM facade_blok b JOIN facade_materializedblok m ON m.blok_id = b.id
              WHERE m.declared_by_id = $1",
        )
        .bind(self.agent)
        .fetch_all(&self.db)
        .await
        .unwrap();
        implementation
            .iter()
            .chain(
                bloks
                    .iter()
                    .flat_map(|d| d.as_array().into_iter().flatten()),
            )
            .map(|d| {
                (
                    d["code"].as_str().unwrap().to_owned(),
                    d["message"].as_str().unwrap().to_owned(),
                )
            })
            .collect()
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

/// Whether `findings` hold one with `code` whose message says `message`.
fn found(findings: &[(String, String)], code: &str, message: &str) -> bool {
    findings
        .iter()
        .any(|(c, m)| c == code && m.contains(message))
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
async fn a_base_argument_mismatch_is_a_finding() {
    let Some(t) = tenant().await else { return };
    let between = t.findings(call(&[], "between")).await;
    assert!(
        found(
            &between,
            "operation_arguments",
            "operation 'between' does not accept arguments ['a', 'b']"
        ),
        "{between:?}"
    );
    let gt = t.findings(payload(&[], "gt", ("a", "c"), json!([]))).await;
    assert!(
        found(
            &gt,
            "operation_arguments",
            "operation 'gt' does not accept arguments ['c']"
        ),
        "{gt:?}"
    );
    assert_eq!(t.count("facade_implementation").await, 1);
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
    let knob = json!([{"key": "panel", "vocab": 2, "module": "electron", "components": [
        {"id": "html", "component": "Html", "children": [{"id": "root", "component": "Knob"}]}]}]);
    let knob = t.findings(payload(&[], "gt", ("a", "b"), knob)).await;
    assert!(
        found(
            &knob,
            "catalog.unknown_component",
            "<Knob> is in no catalog in force"
        ),
        "{knob:?}"
    );

    let boxed = json!([{"key": "panel", "vocab": 2, "module": "electron", "components": [
        {"id": "html", "component": "Html", "children": [
            {"id": "root", "component": "Box", "props": [{"key": "v", "util_call": {"operation": "fmt.fizz"}}]}]}]}]);
    t.register(payload(&[], "gt", ("a", "b"), boxed))
        .await
        .unwrap();
    let (catalog, diagnostics): (String, Value) = sqlx::query_as(
        "SELECT c.name, b.diagnostics FROM facade_blok b JOIN facade_module c ON c.id = b.module_id
          WHERE b.organization_id = $1 AND b.name = 'panel'",
    )
    .bind(t.organization)
    .fetch_one(&t.db)
    .await
    .unwrap();
    assert_eq!(catalog, "electron");
    assert_eq!(
        codes(diagnostics.as_array().unwrap()),
        ["expr.unknown_function"]
    );
}

#[tokio::test]
async fn a_blok_naming_no_catalog_is_checked_against_the_standard_one() {
    let Some(t) = tenant().await else { return };
    t.catalog("orkestrator", json!([]), json!([{"name": "Box"}]))
        .await;
    let knob = json!([{"key": "panel", "vocab": 2, "components": [{"id": "html", "component": "Html", "children": [{"id": "root", "component": "Knob"}]}]}]);
    let knob = t.findings(payload(&[], "gt", ("a", "b"), knob)).await;
    assert!(
        found(
            &knob,
            "catalog.unknown_component",
            "<Knob> is in no catalog in force"
        ),
        "{knob:?}"
    );

    // What the Orkestrator registered beyond the spec's catalogs, and the spec's own components.
    let boxed = json!([{"key": "panel", "vocab": 2, "source": "<Html><Box><Card><Button>Go</Button></Card></Box></Html>"}]);
    assert_eq!(
        t.findings(payload(&[], "gt", ("a", "b"), boxed)).await,
        vec![]
    );
}

#[tokio::test]
async fn components_are_held_to_their_declared_placement() {
    let Some(t) = tenant().await else { return };
    t.catalog(
        "electron",
        json!([]),
        json!([{"name": "Box"}, {"name": "Caption", "accepts_children": false}]),
    )
    .await;
    let blok = |source: &str| json!([{"key": "panel", "vocab": 2, "module": "electron", "source": source}]);

    let accepted = r#"<Html><Box><Scene><Model src={self.assets.scope}><Move node="arm" axis="x" /><Popout><Box><Caption /></Box></Popout></Model></Scene></Box></Html>"#;
    let mut declared = payload(&[], "gt", ("a", "b"), blok(accepted));
    declared["assets"] = json!([{"name": "scope", "kind": "MODEL_3D", "ref": format!("sha256:{}", "9f".repeat(32)), "size": 1}]);
    assert_eq!(t.findings(declared).await, vec![]);

    for (source, message) in [
        (
            "<Html><Box><Move node=\"arm\" axis=\"x\" /></Box></Html>",
            "<Move> is a scene component, not one of the html context",
        ),
        (
            "<Html><Scene><Caption /></Scene></Html>",
            "<Caption> is a html component, not one of the scene context",
        ),
        (
            "<Html><Scene><Move node=\"arm\" axis=\"x\" /></Scene></Html>",
            "<Move> must be inside one of ['Model', 'Robot']",
        ),
        (
            "<Html><Box><Caption><Box /></Caption></Box></Html>",
            "<Caption> takes no children",
        ),
    ] {
        let findings = t
            .findings(payload(&[], "gt", ("a", "b"), blok(source)))
            .await;
        let code = if message.contains("takes no children") {
            "catalog.children"
        } else {
            "catalog.placement"
        };
        assert!(found(&findings, code, message), "{source}: {findings:?}");
    }
}

#[tokio::test]
async fn a_logo_blok_is_one_logo_and_control_flow_takes_its_context() {
    let Some(t) = tenant().await else { return };
    t.catalog("electron", json!([]), json!([{"name": "Box"}]))
        .await;
    let blok = |context: &str, source: &str| json!([{"key": "view", "vocab": 2, "module": "electron", "context": context, "source": source}]);

    for bloks in [
        blok(
            "LOGO",
            r#"<AgentLogo><foreach items={self.lamps} let="lamp" key={lamp}><Path d="M0 0" /></foreach></AgentLogo>"#,
        ),
        blok(
            "HTML",
            r#"<Html><Box><AgentLogo><Path d="M0 0" /></AgentLogo></Box></Html>"#,
        ),
    ] {
        assert_eq!(
            t.findings(payload(&[], "gt", ("a", "b"), bloks)).await,
            vec![]
        );
    }

    // The root a context requires refuses (an error of the vocabulary); where the catalog
    // places a component is a warning.
    for (bloks, code, message) in [
        (
            blok("LOGO", r#"<Path d="M0 0" />"#),
            "refused",
            "[context.root]",
        ),
        (
            blok(
                "LOGO",
                r#"<foreach items={self.lamps} let="lamp" key={lamp}><AgentLogo /></foreach>"#,
            ),
            "refused",
            "[context.root]",
        ),
        (
            blok("HTML", r#"<Html><Box><Path d="M0 0" /></Box></Html>"#),
            "catalog.placement",
            "<Path> is a logo component, not one of the html context",
        ),
        (
            blok(
                "LOGO",
                r#"<AgentLogo><foreach items={self.lamps} let="lamp" key={lamp}><Box /></foreach></AgentLogo>"#,
            ),
            "catalog.placement",
            "<Box> is a html component, not one of the logo context",
        ),
    ] {
        let findings = t.findings(payload(&[], "gt", ("a", "b"), bloks)).await;
        assert!(found(&findings, code, message), "{findings:?}");
    }
}

#[tokio::test]
async fn a_scene_blok_opens_a_scene_names_its_models_and_may_be_the_default() {
    let Some(t) = tenant().await else { return };
    t.catalog("electron", json!([]), json!([{"name": "Box"}]))
        .await;
    let reference = format!("sha256:{}", "9f".repeat(32));
    // A model named by an asset path, in a blok that declares the dependency the path names.
    let scene = |key: &str, path: &str, extra: Value| {
        let mut blok = json!({"key": key, "vocab": 2, "module": "electron", "context": "SCENE",
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
    let viewed = |components: Value| json!([{"key": "view", "vocab": 2, "module": "electron", "context": "SCENE", "components": components}]);

    for (bloks, message) in [
        (
            viewed(json!([{"id": "root", "component": "Box"}])),
            "[context.root]",
        ),
        (
            viewed(
                json!([{"id": "root", "component": "Box", "children": [{"id": "scene", "component": "Scene"}]}]),
            ),
            "[context.root]",
        ),
        (
            // A catalog nothing registered in: the root is still the vocabulary's.
            json!([{"key": "view", "vocab": 2, "module": "unregistered", "context": "SCENE", "components": [{"id": "root", "component": "Box"}]}]),
            "[context.root]",
        ),
        (
            viewed(
                json!([{"id": "scene", "component": "Scene"}, {"id": "other", "component": "Scene"}]),
            ),
            "a blok has one root, not 2",
        ),
        (viewed(json!([])), "a blok has one root, not 0"),
        (
            json!([scene("view", "self.assets", json!({}))]),
            "[deps.asset]",
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
    // A model named by a literal instead of an asset is a finding of the catalog's.
    let literal = viewed(json!([{"id": "scene", "component": "Scene", "children": [
        {"id": "model", "component": "Model", "props": [{"key": "src", "static_value": reference}]}]}]));
    let findings = t
        .findings(with_assets(payload(&[], "gt", ("a", "b"), literal)))
        .await;
    assert!(
        found(&findings, "catalog.asset", "src of <Model>"),
        "{findings:?}"
    );

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
        {"key": "panel", "vocab": 2, "module": "electron", "default": true, "for_model": reference, "for_node": "turret",
         "components": [{"id": "html", "component": "Html", "children": [{"id": "root", "component": "Box"}]}]},
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
        {"key": "panel", "vocab": 2, "module": "electron", "default": true, "components": [{"id": "html", "component": "Html", "children": [{"id": "root", "component": "Box"}]}]},
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
    let findings = t
        .findings(payload(&["electron", "web"], "fmt", ("x", "y"), json!([])))
        .await;
    assert!(
        found(
            &findings,
            "operation_arguments",
            "'fmt' does not accept arguments"
        ),
        "{findings:?}"
    );
}

#[tokio::test]
async fn conflicting_catalogs_are_a_finding_and_identical_ones_tolerated() {
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
    // The first catalog keeps it: `clamp` is still called as electron defines it.
    let findings = t.findings(call(&["electron", "rival"], "clamp")).await;
    assert_eq!(
        findings.iter().map(|(c, _)| c.as_str()).collect::<Vec<_>>(),
        ["catalog_conflict"],
        "{findings:?}"
    );
    assert!(
        found(
            &findings,
            "catalog_conflict",
            "operation 'clamp' is defined differently by catalogs 'electron' and 'rival'"
        ),
        "{findings:?}"
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
    let findings = t
        .findings(widget(&["electron"], custom_knob(), None, "FLOAT"))
        .await;
    assert!(
        found(
            &findings,
            "unknown_component",
            "widget of Definition scan port exposure: component 'Knob' is not registered"
        ),
        "{findings:?}"
    );
    assert_eq!(t.count("facade_implementation").await, 1);
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
    let findings = t.findings(widget(&[], wrong, None, "FLOAT")).await;
    assert!(
        found(
            &findings,
            "operation_arguments",
            "operation 'gt' does not accept arguments ['left']"
        ),
        "{findings:?}"
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
    let findings = t
        .findings(widget(&["electron"], in_filter, None, "STRUCTURE"))
        .await;
    assert!(
        found(
            &findings,
            "unknown_component",
            "widget of Definition scan port exposure filter port f: component 'Knob'"
        ),
        "{findings:?}"
    );

    let mut in_fallback = custom_knob();
    in_fallback["component"] = json!("Box");
    in_fallback["props"] = json!([]);
    in_fallback["fallback"] = custom_knob();
    let findings = t
        .findings(widget(&["electron"], in_fallback, None, "FLOAT"))
        .await;
    assert!(
        found(
            &findings,
            "unknown_component",
            "widget of Definition scan port exposure fallback 1: component 'Knob'"
        ),
        "{findings:?}"
    );

    let findings = t
        .findings(widget(
            &["electron"],
            json!({"kind": "SLIDER"}),
            Some(json!({"kind": "CUSTOM", "component": "Gauge"})),
            "FLOAT",
        ))
        .await;
    assert!(
        found(
            &findings,
            "unknown_component",
            "widget of Definition scan port out: component 'Gauge'"
        ),
        "{findings:?}"
    );
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
    t.catalog("electron", json!([]), json!([{"name": "Box"}]))
        .await;
    let logo = |key: &str, children: Value, demo: Value| {
        json!({"key": key, "vocab": 2, "module": "electron", "context": "LOGO",
        "components": [{"id": "logo", "component": "AgentLogo", "children": children}], "demo": demo})
    };
    let action_logo = |key: &str, children: Value| {
        json!({"key": key, "vocab": 2, "module": "electron", "context": "ACTION_LOGO",
        "components": [{"id": "logo", "component": "ActionLogo", "children": children}]})
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

    let crop = action_logo("crop", json!([path(0)]));
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

    let panel = json!({"key": "panel", "vocab": 2, "components": [{"id": "html", "component": "Html", "children": [{"id": "root", "component": "Box"}]}]});
    // An agent's logo draws no action.
    let app = logo(
        "app",
        json!([painted(
            "p",
            json!({"key": "fill", "static_value": "brand"})
        )]),
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

    // A logo paints with the pens, or a url(#id), and an action logo reads only its task.
    let quiet = action_logo(
        "quiet",
        json!([
            painted("soft", json!({"key": "fill", "static_value": "brand-soft"})),
            painted("glow", json!({"key": "fill", "static_value": "url(#glow)"})),
            painted(
                "tint",
                json!({"key": "fill", "dynamic_value": {"path": "task.message"}})
            )
        ]),
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
            ),
            "[palette.logo_paint]",
        ),
        (
            logo(
                "loud",
                json!([painted(
                    "p",
                    json!({"key": "fill", "static_value": "#fbbf24"})
                )]),
                json!({}),
            ),
            "[palette.logo_paint]",
        ),
        (
            action_logo(
                "nosy",
                json!([painted(
                    "p",
                    json!({"key": "fill", "dynamic_value": {"path": "self.mode"}})
                )]),
            ),
            "[roots.not_in_kind]",
        ),
        (
            json!({"key": "rooted", "vocab": 2, "module": "electron", "context": "ACTION_LOGO", "components": [{"id": "root", "component": "Box"}]}),
            "[context.root]",
        ),
    ] {
        let refused = t
            .register(payload(&[], "gt", ("a", "b"), json!([blok])))
            .await
            .unwrap_err();
        assert!(refused.contains(message), "{refused}");
    }

    let lamps = |n: usize| json!({"deps": {"self": {"lamps": (0..n).collect::<Vec<_>>()}}});
    let looped = json!([{"id": "loop", "component": "foreach", "props": [
        {"key": "items", "dynamic_value": {"path": "self.lamps"}},
        {"key": "let", "static_value": "lamp", "declares_value": "lamp"},
        {"key": "key", "dynamic_value": {"path": "lamp"}}], "children": [path(0)]}]);
    t.register(payload(
        &[],
        "gt",
        ("a", "b"),
        json!([logo("lamps", looped.clone(), lamps(64))]),
    ))
    .await
    .unwrap();
    for (blok, message) in [
        (
            logo(
                "big",
                json!((0..200).map(path).collect::<Vec<_>>()),
                json!({}),
            ),
            "a logo has at most 200 components, this one 201",
        ),
        (
            logo("lamps", looped.clone(), lamps(65)),
            "a foreach in a logo draws at most 64 items, its demo has 65",
        ),
        (
            logo(
                "long",
                json!([{"id": "p", "component": "Path", "props": [{"key": "d", "static_value": "M".repeat(16385)}]}]),
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
        json!([{"name": "Frame"}, {"name": "Caption", "accepts_children": false, "props": [{"key": "text", "kind": "STRING"}]}]),
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
    let tree = json!([{"id": "Html[0]", "component": "Html", "children": [
        {"id": "Html[0]/Frame[0]", "component": "Frame", "children": [
            {"id": "Html[0]/Frame[0]/Caption[0]", "component": "Caption", "props": [{"key": "text", "static_value": "hi"}]}]}]}]);

    t.register(payload(
        &[],
        "gt",
        ("a", "b"),
        json!([{"key": "panel", "vocab": 2, "module": "electron", "components": tree}]),
    ))
    .await
    .unwrap();
    let from_tree = stored().await;
    assert_eq!(from_tree, tree, "stored without its nulls");
    t.register(payload(&[], "gt", ("a", "b"), json!([{"key": "panel", "vocab": 2, "module": "electron", "source": "<Html><Frame><Caption text=\"hi\" /></Frame></Html>"}])))
        .await
        .unwrap();
    assert_eq!(stored().await, from_tree);

    for (blok, code, message) in [
        (
            json!({"key": "panel", "vocab": 2, "components": tree, "source": "<Html><Frame /></Html>"}),
            "refused",
            "not both",
        ),
        (
            json!({"key": "panel", "vocab": 2}),
            "refused",
            "and this one has neither",
        ),
        (
            json!({"key": "panel", "vocab": 2, "source": "<Html><Frame><Caption></Frame></Html>"}),
            "refused",
            "line 1, column",
        ),
        (
            json!({"key": "panel", "vocab": 2, "module": "electron", "source": "<Html><Frame><Knob /></Frame></Html>"}),
            "catalog.unknown_component",
            "<Knob> is in no catalog in force",
        ),
    ] {
        let findings = t
            .findings(payload(&[], "gt", ("a", "b"), json!([blok])))
            .await;
        assert!(found(&findings, code, message), "{findings:?}");
    }
}

/// A blok's args are held to the declared descriptors like an action's: a blok constrained
/// on a key nobody declares would be offered on nothing.
#[tokio::test]
async fn a_blok_arg_constrains_only_declared_descriptors() {
    let Some(t) = tenant().await else { return };
    t.catalog("orkestrator", json!([]), json!([{"name": "Box"}]))
        .await;
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let image = format!("@d{unique}/image");
    let size = format!("@d{unique}/z_size");
    declare_structure(&t.db, &image, &[&size]).await;
    let inspector = |key: &str| {
        let bloks = json!([{"key": "inspector", "vocab": 2, "demo": {"args": {"image": null}},
            "components": [{"id": "html", "component": "Html", "children": [{"id": "root", "component": "Box"}]}],
            "args": [{"key": "image", "kind": "STRUCTURE", "identifier": image, "nullable": false,
                      "requires": [{"key": key, "operator": "GTE", "value": 2}]}]}]);
        payload(&[], "gt", ("a", "b"), bloks)
    };

    let refused = t
        .register(inspector(&format!("@d{unique}/z_sise")))
        .await
        .unwrap_err();
    assert!(
        refused.contains(&format!("Port 'image' of 'inspector' constrains the descriptor '@d{unique}/z_sise', which no service declares for {image}. Declared: {size}")),
        "{refused}"
    );
    t.register(inspector(&size)).await.unwrap();
}
