//! Protocol hints: an action lists the protocols its definition declares, beside the ones
//! inferred from its ports. A hint is not identity, so a re-register that changes only the
//! hints keeps the action's hash and must still rewrite its protocols. Needs
//! `TAKT_TEST_DATABASE_URL`; skipped without it.

use authentikate::base_models::StaticToken;
use facade::registration::implement_agent;
use rekuest_core::inputs::ImplementAgentInputModel;
use serde_json::{json, Value};
use sqlx::PgPool;

struct Tenant {
    db: PgPool,
    agent: i64,
}

async fn tenant() -> Option<Tenant> {
    let url = std::env::var("TAKT_TEST_DATABASE_URL").ok()?;
    let db = PgPool::connect(&url).await.unwrap();
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "protocol-tests", "org": format!("o-{unique}"),
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
    Some(Tenant { db, agent })
}

/// `attach(scene) -> bool`: one BOOL return, so it is also inferred a `predicate`.
fn payload(protocols: Value) -> Value {
    let mut definition = json!({
        "key": "attach", "version": "1", "name": "Attach", "kind": "FUNCTION",
        "args": [{"key": "scene", "kind": "STRING", "nullable": false}],
        "returns": [{"key": "return0", "kind": "BOOL", "nullable": false}],
    });
    if !protocols.is_null() {
        definition["protocols"] = protocols;
    }
    json!({"implementations": [{"interface": "attach", "definition": definition}]})
}

impl Tenant {
    async fn register(&self, payload: Value) -> Result<(), String> {
        let mut model: ImplementAgentInputModel =
            serde_json::from_value(payload).map_err(|e| e.to_string())?;
        model.validate().map_err(|e| e.0)?;
        let mut tx = self.db.begin().await.unwrap();
        implement_agent(&mut tx, self.agent, &model)
            .await
            .map_err(|e| e.to_string())?;
        tx.commit().await.unwrap();
        Ok(())
    }

    /// The action's protocols, sorted, and its hash.
    async fn listed(&self) -> (Vec<String>, String) {
        let (names, hash): (Vec<String>, String) = sqlx::query_as(
            "SELECT coalesce(array_agg(pr.name ORDER BY pr.name) FILTER (WHERE pr.name IS NOT NULL), '{}'), a.hash
             FROM facade_implementation i JOIN facade_action a ON a.id = i.action_id
             LEFT JOIN facade_action_protocols ap ON ap.action_id = a.id
             LEFT JOIN facade_protocol pr ON pr.id = ap.protocol_id
             WHERE i.agent_id = $1 AND i.interface = 'attach' GROUP BY a.hash",
        )
        .bind(self.agent)
        .fetch_one(&self.db)
        .await
        .unwrap();
        (names, hash)
    }
}

#[tokio::test]
async fn declared_hints_are_listed_beside_the_inferred_ones() {
    let Some(t) = tenant().await else { return };
    t.register(payload(json!(["on_scene_init"]))).await.unwrap();
    assert_eq!(t.listed().await.0, ["on_scene_init", "predicate"]);
}

#[tokio::test]
async fn a_change_to_the_hints_alone_lands_and_keeps_the_hash() {
    let Some(t) = tenant().await else { return };
    t.register(payload(Value::Null)).await.unwrap();
    let (names, hash) = t.listed().await;
    assert_eq!(names, ["predicate"]);

    t.register(payload(json!(["on_scene_init", "on_scene_leave"])))
        .await
        .unwrap();
    let (names, again) = t.listed().await;
    assert_eq!(names, ["on_scene_init", "on_scene_leave", "predicate"]);
    assert_eq!(again, hash, "a hint is not identity");

    t.register(payload(json!(["on_scene_leave"])))
        .await
        .unwrap();
    assert_eq!(t.listed().await.0, ["on_scene_leave", "predicate"]);
}

#[tokio::test]
async fn a_declared_hint_named_like_an_inferred_one_keeps_its_description() {
    let Some(t) = tenant().await else { return };
    t.register(payload(json!(["predicate"]))).await.unwrap();
    assert_eq!(t.listed().await.0, ["predicate"]);
    let description: String = sqlx::query_scalar(
        "SELECT pr.description FROM facade_protocol pr JOIN facade_action_protocols ap ON ap.protocol_id = pr.id
         JOIN facade_implementation i ON i.action_id = ap.action_id WHERE i.agent_id = $1",
    )
    .bind(t.agent)
    .fetch_one(&t.db)
    .await
    .unwrap();
    assert!(
        !description.starts_with("Declared"),
        "the inference's description stays: {description}"
    );
}

#[tokio::test]
async fn a_hint_is_one_word_said_once() {
    let Some(t) = tenant().await else { return };
    for bad in [
        json!(["on scene"]),
        json!([""]),
        json!(["on_scene_init", "on_scene_init"]),
    ] {
        let refused = t.register(payload(bad.clone())).await.unwrap_err();
        assert!(refused.contains("protocol hint"), "{bad}: {refused}");
    }
}
