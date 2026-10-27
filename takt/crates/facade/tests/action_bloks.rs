//! An action's own bloks (`ActionBlok`): declared inline with the action, written on every
//! registration (the latest one's), each a blok of the app held by actions only
//! (`held_by_action`), content-addressed and shared by every action declaring the same, never an
//! agent's slot. A blok no action holds is collected. They are not part of the action's hash.
//! Needs `TAKT_TEST_DATABASE_URL`; skipped without it.

use authentikate::base_models::StaticToken;
use facade::deletion::delete_actions;
use facade::registration::implement_agent;
use rekuest_core::inputs::{DefinitionInputModel, ImplementAgentInputModel};
use serde_json::{json, Value};
use sqlx::PgPool;

struct Lab {
    db: PgPool,
    unique: String,
    organization: i64,
}

async fn lab() -> Option<Lab> {
    let url = std::env::var("TAKT_TEST_DATABASE_URL").ok()?;
    let mut lab = Lab {
        db: PgPool::connect(&url).await.unwrap(),
        unique: uuid::Uuid::new_v4().simple().to_string(),
        organization: 0,
    };
    lab.organization = lab.expand("first").await.2;
    Some(lab)
}

impl Lab {
    /// The client, user and organization of `client`, a client of the lab's one app.
    async fn expand(&self, client: &str) -> (i64, i64, i64) {
        let unique = &self.unique;
        let spec: StaticToken = serde_json::from_value(json!({
            "sub": format!("s-{unique}"), "iss": "action-bloks", "org": format!("o-{unique}"),
            "client_id": format!("{client}-{unique}"), "client_app": format!("flow-{unique}"),
            "client_release": "1",
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

    async fn agent(&self, client: &str) -> i64 {
        let (client, user, organization) = self.expand(client).await;
        facade::registration::ensure_agent(&self.db, client, user, organization)
            .await
            .unwrap()
    }

    async fn register(&self, agent: i64, payload: Value) -> Result<(), String> {
        let mut model: ImplementAgentInputModel = serde_json::from_value(payload).unwrap();
        model.validate().map_err(|e| e.0)?;
        let mut tx = self.db.begin().await.unwrap();
        implement_agent(&mut tx, agent, &model)
            .await
            .map_err(|e| e.to_string())?;
        tx.commit().await.unwrap();
        Ok(())
    }

    async fn action(&self) -> (i64, String) {
        sqlx::query_as("SELECT id, hash FROM facade_action WHERE organization_id = $1 AND key = 'process_slides'")
            .bind(self.organization)
            .fetch_one(&self.db)
            .await
            .unwrap()
    }

    /// The action's bloks: `(context, blok, held_by_action, root component)`.
    async fn bloks(&self, action: i64) -> Vec<(String, i64, bool, String)> {
        sqlx::query_as(
            "SELECT ab.context, b.id, b.held_by_action, b.components->0->>'component'
               FROM facade_actionblok ab JOIN facade_blok b ON b.id = ab.blok_id
              WHERE ab.action_id = $1 ORDER BY ab.context",
        )
        .bind(action)
        .fetch_all(&self.db)
        .await
        .unwrap()
    }

    async fn held(&self) -> i64 {
        sqlx::query_scalar(
            "SELECT count(*) FROM facade_blok WHERE organization_id = $1 AND held_by_action",
        )
        .bind(self.organization)
        .fetch_one(&self.db)
        .await
        .unwrap()
    }
}

fn overlay(through: &[&str]) -> Value {
    json!({"context": "OVERLAY", "vocab": 2, "locals": [{"key": "show_path", "kind": "BOOL", "default": true, "persist": true}],
           "components": [{"id": "root", "component": "Overlay", "children": [
               {"id": "robot", "component": "Anchor", "props": [{"key": "dep", "static_value": "robot"}], "children": [
                   {"id": "glow", "component": "Glow", "props": [{"key": "on", "dynamic_value": {"path": "self.helsinki.stage"}}]}]},
               {"id": "route", "component": "Route", "props": [
                   {"key": "through", "static_value": through},
                   {"key": "progress", "dynamic_value": {"path": "task.progress"}}]}]}]})
}

fn panel() -> Value {
    json!({"context": "HTML", "vocab": 2, "source": "<Html><Card><Progress value={task.progress} /><ask /></Card></Html>"})
}

fn workflow(bloks: Value) -> Value {
    json!({"implementations": [{
        "interface": "process_slides", "execution": "WORKFLOW",
        "dependencies": [{"key": "robot"}, {"key": "frame"}],
        "definition": {"key": "process_slides", "name": "Process slides", "kind": "GENERATOR", "bloks": bloks},
    }]})
}

#[tokio::test]
async fn an_actions_bloks_are_held_by_it_shared_and_collected() {
    let Some(lab) = lab().await else { return };
    let first = lab.agent("first").await;
    let second = lab.agent("second").await;

    let bloks = json!([overlay(&["robot", "frame", "robot"]), panel()]);
    lab.register(first, workflow(bloks.clone())).await.unwrap();
    let (action, hash) = lab.action().await;
    let written = lab.bloks(action).await;
    assert_eq!(
        written
            .iter()
            .map(|(context, _, held, root)| (context.as_str(), *held, root.as_str()))
            .collect::<Vec<_>>(),
        vec![("HTML", true, "Html"), ("OVERLAY", true, "Overlay")],
        "one blok per context, the BSX parsed, both held by the action"
    );
    // Not an agent's: no slot holds it, and it is not part of the action's hash.
    let slots: i64 =
        sqlx::query_scalar("SELECT count(*) FROM facade_materializedblok WHERE blok_id = ANY($1)")
            .bind(written.iter().map(|(_, id, _, _)| *id).collect::<Vec<_>>())
            .fetch_one(&lab.db)
            .await
            .unwrap();
    assert_eq!(slots, 0);
    let mut plain: DefinitionInputModel =
        serde_json::from_value(workflow(json!([]))["implementations"][0]["definition"].clone())
            .unwrap();
    plain.validate().unwrap();
    assert_eq!(hash, plain.unique_hash());

    // A second agent declaring the same: the same action, the same rows.
    lab.register(second, workflow(bloks)).await.unwrap();
    assert_eq!(lab.bloks(action).await, written);
    assert_eq!(lab.held().await, 2);

    // A change to one blok: the action holds the new content, the old one is collected.
    lab.register(
        second,
        workflow(json!([overlay(&["robot", "frame"]), panel()])),
    )
    .await
    .unwrap();
    let changed = lab.bloks(action).await;
    assert_eq!(changed[0], written[0], "the panel did not change");
    assert_ne!(changed[1].1, written[1].1);
    assert_eq!(lab.held().await, 2);
    assert_eq!(
        lab.action().await,
        (action, hash.clone()),
        "still the same action"
    );

    // Registered without bloks: the action lets them go, and nothing else holds them.
    lab.register(first, workflow(json!([]))).await.unwrap();
    assert!(lab.bloks(action).await.is_empty());
    assert_eq!(lab.held().await, 0);

    // Deleted with the action.
    lab.register(first, workflow(json!([panel()])))
        .await
        .unwrap();
    assert_eq!(lab.held().await, 1);
    let mut tx = lab.db.begin().await.unwrap();
    delete_actions(&mut tx, &[action]).await.unwrap();
    tx.commit().await.unwrap();
    assert_eq!(lab.held().await, 0);
}

#[tokio::test]
async fn an_actions_bloks_name_only_its_dependencies() {
    let Some(lab) = lab().await else { return };
    let agent = lab.agent("first").await;

    let refused = lab
        .register(agent, workflow(json!([overlay(&["robot", "stitcher"])])))
        .await
        .unwrap_err();
    assert_eq!(
        refused,
        "Blok manifest: component 'route' names 'stitcher', which is not a dependency of the action (its dependencies: 'frame', 'robot')"
    );

    let wandering = json!([{"context": "HTML", "vocab": 2, "source": "<Html><Text text={stage.position} /></Html>"}]);
    let refused = lab.register(agent, workflow(wandering)).await.unwrap_err();
    assert!(refused.contains("[roots.unknown]"), "{refused}");

    let twice = json!([panel(), panel()]);
    let refused = lab.register(agent, workflow(twice)).await.unwrap_err();
    assert_eq!(
        refused,
        "Definition process_slides: the html blok is given twice; an action has one blok per context"
    );

    // An overlay's root is the Overlay, whatever the catalog says.
    let rooted = json!([{"context": "OVERLAY", "vocab": 2, "source": "<Scene />"}]);
    let refused = lab.register(agent, workflow(rooted)).await.unwrap_err();
    assert!(refused.contains("[context.root]"), "{refused}");
}

#[tokio::test]
async fn only_an_actions_blok_reads_a_run() {
    let Some(lab) = lab().await else { return };
    let agent = lab.agent("first").await;

    // An agent's own blok holding an Anchor or an ask is refused: it has no run to read.
    let asking = json!({"bloks": [{"key": "panel", "vocab": 2, "source": "<Html><Card><ask /></Card></Html>"}]});
    let refused = lab.register(agent, asking).await.unwrap_err();
    assert!(
        refused.contains("an 'ask' shows a task's open ask"),
        "{refused}"
    );
    let anchored = json!({"bloks": [{"key": "panel", "vocab": 2, "source": "<Scene><Anchor dep=\"robot\" /></Scene>", "context": "SCENE"}]});
    let refused = lab.register(agent, anchored).await.unwrap_err();
    assert!(refused.contains("reads the agents of a run"), "{refused}");

    // Nor does an agent declare an overlay: an overlay is an action's.
    let overlaid = json!({"bloks": [{"key": "over", "vocab": 2, "context": "OVERLAY", "source": "<Overlay />"}]});
    let refused = lab.register(agent, overlaid).await.unwrap_err();
    assert!(refused.contains("not OVERLAY. [context.kind]"), "{refused}");
}
