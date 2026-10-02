//! The fan-out payloads, against Python's own builders on the same rows.
//!
//! The rekuest server in the test stack (`scripts/test-db.sh`) seeds a task tree, an event and a
//! patch with its test factories and prints what `facade/channel_events.py` builds for them. The
//! Rust signals then publish the same rows, on the groups Python uses, and must deliver the same
//! payloads. Needs `TAKT_TEST_DATABASE_URL` and `TAKT_TEST_REDIS_URL`; skipped without them.

use std::sync::Arc;
use std::time::Duration;

use facade::consumers::connections::Connections;
use facade::settings::Settings;
use serde_json::Value;
use tokio::sync::mpsc::UnboundedReceiver;

const STACK_CONTAINER: &str = "takt-testdb-rekuest-1";

const SEED: &str = r#"
import json, uuid
from facade import models
from facade.channel_events import TaskEventCreatedEvent, TaskEventPayload, ChildTaskEvent, TaskChangePayload, PatchEvent
from tests.factories import _seed_throwaway_agent_graph, _build_task_for_agent_caller, _build_state_for_agent
prefix = "signals-" + uuid.uuid4().hex[:8]
agent = _seed_throwaway_agent_graph(prefix)
root = _build_task_for_agent_caller(agent.pk, prefix + "-root")
child = _build_task_for_agent_caller(agent.pk, prefix + "-child", parent=root, root=root)
event = models.TaskEvent.objects.create(task=root, kind="YIELD", message="half way", progress=50, returns={"return0": [1, 2.5, "x", True, None]})
state = _build_state_for_agent(agent.pk, prefix + "-state", prefix)
patch = models.Patch.objects.create(state=state, agent=agent, interface=state.interface, op="replace", path="/barcode", value={"n": 1}, global_rev=3)
root.refresh_from_db(); child.refresh_from_db(); event.refresh_from_db(); patch.refresh_from_db()
print("SEED " + json.dumps({
    "event": event.id, "root": root.id, "child": child.id, "patch": patch.id, "state": state.id,
    "caller": root.caller_id, "org": root.caller.organization_id, "agent": agent.pk,
    "event_payload": TaskEventCreatedEvent(event=TaskEventPayload.from_event(event)).model_dump(mode="json"),
    "child_payload": ChildTaskEvent(update=TaskChangePayload.from_task(child)).model_dump(mode="json"),
    "patch_payload": PatchEvent.from_patch(patch).model_dump(mode="json"),
}))
"#;

async fn seed() -> Option<Value> {
    let output = tokio::process::Command::new("docker")
        .args([
            "exec",
            "-i",
            STACK_CONTAINER,
            "python",
            "manage.py",
            "shell",
            "-c",
            SEED,
        ])
        .output()
        .await
        .ok()?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let line = stdout.lines().find_map(|line| line.strip_prefix("SEED "));
    assert!(
        line.is_some(),
        "the seed printed nothing:\n{stdout}\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_str(line.unwrap()).ok()
}

async fn context(db: &str, redis_url: &str) -> facade::Context {
    let client = redis::Client::open(redis_url).unwrap();
    let authentikate = authentikate::AuthentikateSettings::prepare(
        &serde_json::json!({"audience": "rekuest"}),
        true,
    )
    .unwrap();
    facade::Context {
        db: sqlx::PgPool::connect(db).await.unwrap(),
        redis: redis::aio::ConnectionManager::new(client.clone())
            .await
            .unwrap(),
        redis_client: client.clone(),
        settings: Arc::new(Settings::default()),
        verifier: Arc::new(authentikate::Verifier::new(authentikate)),
        channel_layer: kante::ChannelLayer::new(
            client,
            kante::ChannelLayerConfig {
                // Not the stack's prefix: the Python side's own broadcasts of these rows stay out.
                prefix: format!("signals-contract-{}", uuid::Uuid::new_v4().simple()),
                capacity: 5000,
                ..kante::ChannelLayerConfig::default()
            },
        )
        .await
        .unwrap(),
        connections: Connections::default(),
    }
}

async fn listen(ctx: &facade::Context, groups: &[String]) -> UnboundedReceiver<Value> {
    let (channel, inbox) = ctx.channel_layer.subscribe("specific").await.unwrap();
    for group in groups {
        ctx.channel_layer.group_add(group, &channel).await.unwrap();
    }
    inbox
}

async fn next(inbox: &mut UnboundedReceiver<Value>) -> Value {
    tokio::time::timeout(Duration::from_secs(10), inbox.recv())
        .await
        .expect("a message in time")
        .expect("a message")
}

#[tokio::test]
async fn the_rust_fan_out_is_the_python_fan_out() {
    let (Ok(db), Ok(redis_url)) = (
        std::env::var("TAKT_TEST_DATABASE_URL"),
        std::env::var("TAKT_TEST_REDIS_URL"),
    ) else {
        return;
    };
    let Some(seed) = seed().await else { return };
    let ctx = context(&db, &redis_url).await;
    let id = |key: &str| seed[key].as_i64().unwrap();

    // An event on a root task: its caller, and the caller's and the organization's root feeds.
    for group in [
        format!("task_caller_{}", id("caller")),
        format!("root_tasks_caller_{}", id("caller")),
        format!("root_tasks_org_{}", id("org")),
    ] {
        let mut inbox = listen(&ctx, std::slice::from_ref(&group)).await;
        facade::signals::task_event_created(&ctx, id("event")).await;
        let got = next(&mut inbox).await;
        assert_eq!(got["type"], "channel.TaskEventCreatedEvent", "{group}");
        assert_eq!(got["message"], seed["event_payload"], "{group}");
    }

    // An updated child: its agent's feed, and its parent's (here also its root's) detail feed.
    let mut agent_feed = listen(&ctx, &[format!("agent_tasks_{}", id("agent"))]).await;
    let mut child_feed = listen(&ctx, &[format!("child_tasks_{}", id("root"))]).await;
    facade::signals::task_saved(&ctx, id("child"), false).await;
    let got = next(&mut agent_feed).await;
    assert_eq!(got["type"], "channel.agent_task_feed");
    assert_eq!(got["message"], seed["child_payload"]);
    let got = next(&mut child_feed).await;
    assert_eq!(got["type"], "channel.child_task_feed");
    assert_eq!(got["message"], seed["child_payload"]);
    assert!(
        tokio::time::timeout(Duration::from_millis(300), child_feed.recv())
            .await
            .is_err(),
        "parent and root are one group when they are one task"
    );

    // A patch: its state's feed.
    let mut patches = listen(&ctx, &[format!("patches_state_{}", id("state"))]).await;
    facade::signals::patch_created(&ctx, id("patch")).await;
    let got = next(&mut patches).await;
    assert_eq!(got["type"], "channel.PatchEvent");
    assert_eq!(got["message"], seed["patch_payload"]);
}
