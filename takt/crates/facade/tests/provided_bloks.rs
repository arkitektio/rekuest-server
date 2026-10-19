//! Provided bloks: a blok an agent declares is its app's template for that content, shared by
//! every agent of the app declaring the same, held through each agent's own slot (its
//! `MaterializedBlok` for the blok's key). The slot follows the agent's declaration and keeps its
//! id; a template no slot holds is deleted. A user's custom blok is never touched. Needs
//! `TAKT_TEST_DATABASE_URL`; skipped without it.

use authentikate::base_models::StaticToken;
use facade::deletion::delete_agent;
use facade::registration::implement_agent;
use rekuest_core::inputs::ImplementAgentInputModel;
use serde_json::{json, Value};
use sqlx::PgPool;

struct Fleet {
    db: PgPool,
    unique: String,
    organization: i64,
    user: i64,
}

async fn fleet() -> Option<Fleet> {
    let url = std::env::var("TAKT_TEST_DATABASE_URL").ok()?;
    let mut fleet = Fleet {
        db: PgPool::connect(&url).await.unwrap(),
        unique: uuid::Uuid::new_v4().simple().to_string(),
        organization: 0,
        user: 0,
    };
    let (_, user, organization) = fleet.expand("first", "app", "1").await;
    fleet.user = user;
    fleet.organization = organization;
    Some(fleet)
}

impl Fleet {
    /// The client, user and organization of `client`, a client of `app` at `release`; one user
    /// and organization for the whole fleet.
    async fn expand(&self, client: &str, app: &str, release: &str) -> (i64, i64, i64) {
        let unique = &self.unique;
        let spec: StaticToken = serde_json::from_value(json!({
            "sub": format!("s-{unique}"), "iss": "provided-bloks", "org": format!("o-{unique}"),
            "client_id": format!("{client}-{unique}"), "client_app": format!("{app}-{unique}"),
            "client_release": release,
        }))
        .unwrap();
        let id = authentikate::expand::expand_token_context(
            &self.db,
            &spec.to_token(chrono::Utc::now(), "raw"),
        )
        .await
        .unwrap();
        (id.client, id.user, id.organization)
    }

    async fn agent(&self, client: &str, app: &str, release: &str) -> i64 {
        let (client, user, organization) = self.expand(client, app, release).await;
        facade::registration::ensure_agent(&self.db, client, user, organization)
            .await
            .unwrap()
    }

    async fn register(&self, agent: i64, payload: Value) {
        let mut model: ImplementAgentInputModel = serde_json::from_value(payload).unwrap();
        model.validate().unwrap();
        let mut tx = self.db.begin().await.unwrap();
        implement_agent(&mut tx, agent, &model).await.unwrap();
        tx.commit().await.unwrap();
    }

    async fn delete(&self, agent: i64) {
        let mut tx = self.db.begin().await.unwrap();
        delete_agent(&mut tx, agent).await.unwrap();
        tx.commit().await.unwrap();
    }

    async fn catalog(&self) {
        sqlx::query(
            "INSERT INTO facade_uicatalog (name, components, operations, widget_defaults, organization_id)
             VALUES ('electron', $1, '[]', '[]', $2)",
        )
        .bind(json!([
            {"name": "Box", "props": [{"key": "label", "kind": "STRING", "required": false}]},
            {"name": "ActionLogo", "children_context": "LOGO", "not_downstream_of": ["ActionLogo"]},
            {"name": "Path", "context": "LOGO", "downstream_of": ["ActionLogo"], "accepts_children": false,
             "props": [{"key": "d", "kind": "STRING"}]},
        ]))
        .bind(self.organization)
        .execute(&self.db)
        .await
        .unwrap();
    }

    /// The organization's bloks named `name`: whether provided, and their components.
    async fn templates(&self, name: &str) -> Vec<(bool, Value)> {
        sqlx::query_as(
            "SELECT app_id IS NOT NULL, components FROM facade_blok
              WHERE organization_id = $1 AND name = $2 ORDER BY id",
        )
        .bind(self.organization)
        .bind(name)
        .fetch_all(&self.db)
        .await
        .unwrap()
    }

    /// The agent's slot for `name`: its id and the template it holds.
    async fn slot(&self, agent: i64, name: &str) -> Option<(i64, i64)> {
        sqlx::query_as(
            "SELECT id, blok_id FROM facade_materializedblok WHERE declared_by_id = $1 AND name = $2",
        )
        .bind(agent)
        .bind(name)
        .fetch_optional(&self.db)
        .await
        .unwrap()
    }

    async fn count(&self, sql: &str, id: i64) -> i64 {
        sqlx::query_scalar(sql)
            .bind(id)
            .fetch_one(&self.db)
            .await
            .unwrap()
    }
}

fn panel(label: &str) -> Value {
    json!({"key": "panel", "catalog": "electron", "components": [
        {"id": "root", "component": "Box", "props": [{"key": "label", "static_value": label}]}]})
}

fn declaring(bloks: Value) -> Value {
    json!({"bloks": bloks})
}

#[tokio::test]
async fn agents_of_one_app_share_a_template_and_each_slot_follows_its_agent() {
    let Some(f) = fleet().await else { return };
    f.catalog().await;
    let first = f.agent("first", "app", "1").await;
    let second = f.agent("second", "app", "1").await;

    f.register(first, declaring(json!([panel("hi")]))).await;
    f.register(second, declaring(json!([panel("hi")]))).await;
    assert_eq!(f.templates("panel").await.len(), 1, "same content, one template");
    let (first_slot, shared) = f.slot(first, "panel").await.unwrap();
    assert_eq!(f.slot(second, "panel").await.unwrap().1, shared);

    // Something placed on the first agent's slot.
    let dashboard: i64 = sqlx::query_scalar(
        "INSERT INTO facade_dashboard (name, organization_id) VALUES ('d', $1) RETURNING id",
    )
    .bind(f.organization)
    .fetch_one(&f.db)
    .await
    .unwrap();
    sqlx::query("INSERT INTO facade_dashboardplacement (dashboard_id, blok_id, created_at, updated_at) VALUES ($1, $2, now(), now())")
        .bind(dashboard)
        .bind(first_slot)
        .execute(&f.db)
        .await
        .unwrap();

    // Changed content: a new template, the same slot, the placement kept; the second agent's
    // template is not overwritten.
    f.register(first, declaring(json!([panel("changed")]))).await;
    let (slot, changed) = f.slot(first, "panel").await.unwrap();
    assert_eq!(slot, first_slot);
    assert_ne!(changed, shared);
    assert_eq!(f.slot(second, "panel").await.unwrap().1, shared);
    assert_eq!(f.templates("panel").await.len(), 2);
    let placed = "SELECT count(*) FROM facade_dashboardplacement WHERE dashboard_id = $1";
    assert_eq!(f.count(placed, dashboard).await, 1);

    // Back to the shared content: the changed template has no holder left and goes.
    f.register(first, declaring(json!([panel("hi")]))).await;
    assert_eq!(f.slot(first, "panel").await.unwrap(), (first_slot, shared));
    assert_eq!(f.templates("panel").await.len(), 1);

    // No longer declared: the slot and what was placed on it go, the template stays held.
    f.register(first, declaring(json!([]))).await;
    assert!(f.slot(first, "panel").await.is_none());
    assert_eq!(f.count(placed, dashboard).await, 0);
    assert_eq!(f.templates("panel").await.len(), 1);

    // Its last holder deleted, the template goes with its dependencies.
    f.delete(second).await;
    assert!(f.templates("panel").await.is_empty());
}

#[tokio::test]
async fn releases_of_an_app_and_other_apps_never_overwrite_each_other() {
    let Some(f) = fleet().await else { return };
    f.catalog().await;
    let old = f.agent("old", "app", "1").await;
    let new = f.agent("new", "app", "2").await;
    let other = f.agent("other", "other", "1").await;

    f.register(old, declaring(json!([panel("v1")]))).await;
    f.register(new, declaring(json!([panel("v2")]))).await;
    f.register(other, declaring(json!([panel("v1")]))).await;
    let labels: Vec<Value> = f
        .templates("panel")
        .await
        .into_iter()
        .map(|(_, components)| components[0]["props"][0]["static_value"].clone())
        .collect();
    assert_eq!(labels, [json!("v1"), json!("v2"), json!("v1")]);
}

#[tokio::test]
async fn a_custom_blok_of_the_same_name_is_left_alone() {
    let Some(f) = fleet().await else { return };
    f.catalog().await;
    let agent = f.agent("first", "app", "1").await;
    let custom: i64 = sqlx::query_scalar(
        "INSERT INTO facade_blok (name, components, demo_state, diagnostics, context, creator_id, organization_id, catalog_id)
         VALUES ('panel', '[\"mine\"]', '{}', '[]', 'HTML', $1, $2,
                 (SELECT id FROM facade_uicatalog WHERE organization_id = $2 AND name = 'electron'))
         RETURNING id",
    )
    .bind(f.user)
    .bind(f.organization)
    .fetch_one(&f.db)
    .await
    .unwrap();

    f.register(agent, declaring(json!([panel("hi")]))).await;
    f.register(agent, declaring(json!([]))).await;
    f.delete(agent).await;
    assert_eq!(f.templates("panel").await, [(false, json!(["mine"]))]);
    let kept = "SELECT count(*) FROM facade_blok WHERE id = $1";
    assert_eq!(f.count(kept, custom).await, 1);
}

#[tokio::test]
async fn an_action_keeps_its_logo_while_any_agent_still_provides_it() {
    let Some(f) = fleet().await else { return };
    f.catalog().await;
    let first = f.agent("first", "app", "1").await;
    let second = f.agent("second", "app", "1").await;
    let declaration = json!({
        "implementations": [{"interface": "scan", "logo": "crop",
            "definition": {"key": "scan", "version": "1", "name": "Scan", "kind": "FUNCTION", "args": [], "returns": []}}],
        "bloks": [{"key": "crop", "catalog": "electron", "context": "ACTION_LOGO",
            "components": [{"id": "logo", "component": "ActionLogo", "children": [
                {"id": "p", "component": "Path", "props": [{"key": "d", "static_value": "M0 0"}]}]}]}],
    });
    f.register(first, declaration.clone()).await;
    f.register(second, declaration).await;
    let action: i64 = sqlx::query_scalar(
        "SELECT action_id FROM facade_implementation WHERE agent_id = $1",
    )
    .bind(first)
    .fetch_one(&f.db)
    .await
    .unwrap();
    let logo = "SELECT count(*) FROM facade_action WHERE id = $1 AND latest_logo_id IS NOT NULL";
    assert_eq!(f.count(logo, action).await, 1);

    f.delete(first).await;
    assert_eq!(f.count(logo, action).await, 1);
    f.delete(second).await;
    assert_eq!(f.count(logo, action).await, 0);
}

#[tokio::test]
async fn an_action_falls_back_to_another_releases_logo() {
    let Some(f) = fleet().await else { return };
    f.catalog().await;
    let old = f.agent("old", "app", "1").await;
    let new = f.agent("new", "app", "2").await;
    let drawn = |d: &str| {
        json!({
            "implementations": [{"interface": "scan", "logo": "crop",
                "definition": {"key": "scan", "version": "1", "name": "Scan", "kind": "FUNCTION", "args": [], "returns": []}}],
            "bloks": [{"key": "crop", "catalog": "electron", "context": "ACTION_LOGO",
                "components": [{"id": "logo", "component": "ActionLogo", "children": [
                    {"id": "p", "component": "Path", "props": [{"key": "d", "static_value": d}]}]}]}],
        })
    };
    f.register(old, drawn("M0 0")).await;
    f.register(new, drawn("M1 1")).await;
    let (_, old_logo) = f.slot(old, "crop").await.unwrap();
    let (_, new_logo) = f.slot(new, "crop").await.unwrap();
    let latest = || async {
        sqlx::query_scalar::<_, Option<i64>>(
            "SELECT a.latest_logo_id FROM facade_action a JOIN facade_implementation i ON i.action_id = a.id
              WHERE i.agent_id = $1",
        )
        .bind(old)
        .fetch_one(&f.db)
        .await
        .unwrap()
    };
    assert_eq!(latest().await, Some(new_logo));

    // The agent whose logo the action shows goes: the other release's logo takes its place.
    f.delete(new).await;
    assert_eq!(latest().await, Some(old_logo));
}
