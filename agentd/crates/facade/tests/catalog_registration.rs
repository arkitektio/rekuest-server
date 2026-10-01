//! Definitions and bloks validated against the base catalog plus the UI catalogs they name, at
//! registration: unknown operations and catalogs are stored warnings, argument mismatches and
//! unregistered components refuse the whole registration. Ported from the Python server's
//! `tests/models/test_definition_catalog.py` when registration moved here; the messages are the
//! Python server's. Needs `AGENTD_TEST_DATABASE_URL`; skipped without it.

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
    let url = std::env::var("AGENTD_TEST_DATABASE_URL").ok()?;
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
                      "requires": [{"key": "k", "operator": "EXISTS"}]}]}},
    ]});
    let refused = t.register(declaration).await.unwrap_err();
    assert!(refused.contains("requires a boolean value"), "{refused}");
    assert_eq!(t.count("facade_implementation").await, 0);
}
