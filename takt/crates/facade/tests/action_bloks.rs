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

fn asking(asks: Value) -> Value {
    json!({"implementations": [{
        "interface": "process_slides", "execution": "WORKFLOW",
        "definition": {"key": "process_slides", "name": "Process slides", "kind": "FUNCTION"},
        "asks": asks,
    }]})
}

fn pick() -> Value {
    json!({"key": "asks.pick", "description": "Pick one.", "vocab": 2,
           "source": "<Html><Button label={args.question} onClick={task.answer(values: {name: args.question})} /></Html>",
           "args": [{"key": "question", "kind": "STRING", "nullable": false, "effects": [], "validators": []}],
           "returns": [{"key": "name", "kind": "STRING", "nullable": false, "effects": []}]})
}

/// An implementation's declared asks: a hidden template of the app per content, its args as
/// port rows and its returns stored, linked under its key; shared by the app's agents, let go
/// with the key and collected once nothing holds it.
#[tokio::test]
async fn an_implementations_asks_are_held_by_it_shared_and_collected() {
    let Some(lab) = lab().await else { return };
    let first = lab.agent("first").await;
    let second = lab.agent("second").await;
    lab.register(first, asking(json!([pick()]))).await.unwrap();
    lab.register(second, asking(json!([pick()]))).await.unwrap();

    let linked: Vec<(String, i64, bool, serde_json::Value)> = sqlx::query_as(
        "SELECT ia.key, b.id, b.held_by_action, b.returns
           FROM facade_implementationinteraction ia JOIN facade_blok b ON b.id = ia.blok_id
           JOIN facade_implementation i ON i.id = ia.implementation_id
          WHERE i.agent_id = ANY($1) ORDER BY i.agent_id",
    )
    .bind(vec![first, second])
    .fetch_all(&lab.db)
    .await
    .unwrap();
    assert_eq!(linked.len(), 2);
    assert_eq!(linked[0].1, linked[1].1, "one template for the app");
    assert!(linked
        .iter()
        .all(|(key, _, held, _)| key == "asks.pick" && *held));
    assert_eq!(linked[0].3[0]["key"], "name");
    let ports: i64 =
        sqlx::query_scalar("SELECT count(*) FROM facade_blokargport WHERE blok_id = $1")
            .bind(linked[0].1)
            .fetch_one(&lab.db)
            .await
            .unwrap();
    assert_eq!(ports, 1);

    lab.register(first, asking(json!([]))).await.unwrap();
    assert_eq!(lab.held().await, 1, "the second still holds it");
    lab.register(second, asking(json!([]))).await.unwrap();
    assert_eq!(lab.held().await, 0, "collected once nothing holds it");
}

/// An ask is checked as a task blok at registration, not when a task asks: one that does not
/// parse, a key given twice, and dynamic calls on what is no workflow are refused there.
#[tokio::test]
async fn an_ask_that_is_no_task_blok_is_refused_at_registration() {
    let Some(lab) = lab().await else { return };
    let agent = lab.agent("only").await;
    let broken = json!({"key": "asks.pick", "vocab": 2, "source": "<Html><Button"});
    assert!(lab.register(agent, asking(json!([broken]))).await.is_err());

    let twice = json!([pick(), pick()]);
    let refused = lab.register(agent, asking(twice)).await.unwrap_err();
    assert!(refused.contains("asks.pick"), "{refused}");

    let calling = json!({"implementations": [{
        "interface": "plain", "dynamic_calls": true,
        "definition": {"key": "plain", "name": "Plain", "kind": "FUNCTION"},
    }]});
    let refused = lab.register(agent, calling).await.unwrap_err();
    assert!(refused.contains("only a WORKFLOW"), "{refused}");
}

fn progress() -> Value {
    json!({"key": "asks.progress", "description": "Progress.", "vocab": 2,
           "source": "<Html><Text text={args.step} /></Html>",
           "args": [{"key": "step", "kind": "INT", "nullable": false, "effects": [], "validators": []}]})
}

/// An implementation's declared informs are stored beside its asks, told apart by role, each
/// its own template; an inform answers nothing (no returns), and a key is one ask or one inform.
#[tokio::test]
async fn an_implementations_informs_are_held_beside_its_asks() {
    let Some(lab) = lab().await else { return };
    let agent = lab.agent("informing").await;
    let mut declared = asking(json!([pick()]));
    declared["implementations"][0]["informs"] = json!([progress()]);
    lab.register(agent, declared).await.unwrap();

    let linked: Vec<(String, String, i64)> = sqlx::query_as(
        "SELECT ii.key, ii.role, ii.blok_id FROM facade_implementationinteraction ii
           JOIN facade_implementation i ON i.id = ii.implementation_id
          WHERE i.agent_id = $1 ORDER BY ii.key",
    )
    .bind(agent)
    .fetch_all(&lab.db)
    .await
    .unwrap();
    assert_eq!(
        linked.iter().map(|(k, r, _)| (k.as_str(), r.as_str())).collect::<Vec<_>>(),
        [("asks.pick", "ASK"), ("asks.progress", "INFORM")]
    );
    assert_ne!(linked[0].2, linked[1].2);

    // An inform with returns is no inform at all: the declaration does not parse.
    let mut answering = asking(json!([]));
    let mut with_returns = progress();
    with_returns["returns"] = json!([{"key": "x", "kind": "INT", "nullable": false, "effects": []}]);
    answering["implementations"][0]["informs"] = json!([with_returns]);
    let parsed = serde_json::from_value::<ImplementAgentInputModel>(answering);
    assert!(parsed.unwrap_err().to_string().contains("unknown field `returns`"));

    let mut twice = asking(json!([pick()]));
    let mut same_key = progress();
    same_key["key"] = json!("asks.pick");
    twice["implementations"][0]["informs"] = json!([same_key]);
    let refused = lab.register(agent, twice).await.unwrap_err();
    assert!(refused.contains("asks.pick"), "{refused}");
}
