//! Deleting an agent or an implementation through takt: the whole cascade lands (the foreign
//! keys are deferred, so only a commit proves nothing dangles), other agents keep their rows, and
//! both are refused across organizations. Needs `TAKT_TEST_DATABASE_URL` and
//! `TAKT_TEST_REDIS_URL`; skipped without them.

use std::sync::Arc;

use authentikate::base_models::StaticToken;
use facade::backend::BackendError;
use facade::consumers::agent_queue::queue_key;
use facade::consumers::connections::Connections;
use facade::removal::{cleanup_actions, delete_agent, delete_implementation};
use facade::settings::Settings;
use facade::Context;
use serde_json::json;

async fn context() -> Option<Context> {
    let db_url = std::env::var("TAKT_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("TAKT_TEST_REDIS_URL").ok()?;
    let redis_client = redis::Client::open(redis_url).unwrap();
    let auth =
        authentikate::AuthentikateSettings::prepare(&json!({"audience": "rekuest"}), true).unwrap();
    Some(Context {
        db: sqlx::PgPool::connect(&db_url).await.unwrap(),
        redis: redis::aio::ConnectionManager::new(redis_client.clone())
            .await
            .unwrap(),
        redis_client: redis_client.clone(),
        settings: Arc::new(Settings {
            redis_key_prefix: format!("removal-tests-{}", uuid::Uuid::new_v4().simple()),
            ..Settings::default()
        }),
        verifier: Arc::new(authentikate::Verifier::new(auth)),
        connections: Connections::default(),
        channel_layer: kante::ChannelLayer::new(redis_client, kante::ChannelLayerConfig::default())
            .await
            .unwrap(),
    })
}

/// A fresh agent of `org`, registered with an implementation that depends on another agent,
/// requires a lock and manipulates a state, plus a blok: (agent, its organization).
async fn registered(ctx: &Context, org: &str) -> (i64, i64) {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "removal-tests", "org": org,
        "client_id": format!("c-{unique}"), "client_app": format!("a-{unique}"), "client_release": "1",
    }))
    .unwrap();
    let identity = authentikate::expand::expand_token_context(
        &ctx.db,
        &spec.to_token(chrono::Utc::now(), "raw"),
    )
    .await
    .unwrap();
    let agent = facade::registration::ensure_agent(
        &ctx.db,
        identity.client,
        identity.user,
        identity.organization,
    )
    .await
    .unwrap();
    let payload: rekuest_core::inputs::ImplementAgentInputModel = serde_json::from_value(json!({
        "hash": unique,
        "locks": [{"key": "stage", "definition": {"key": "stage", "description": "the stage"}}],
        "states": [{"interface": "position", "definition": {"name": "Position", "ports": [
            {"key": "x", "kind": "FLOAT", "nullable": false}]}}],
        "implementations": [{
            "interface": "move",
            "locks": ["stage"],
            "manipulates": ["position"],
            "dependencies": [{"key": "camera", "action_dependencies": [{"key": "snap"}]}],
            "definition": {"key": format!("move-{unique}"), "name": "Move", "kind": "FUNCTION"},
        }],
        "bloks": [{"key": format!("panel-{unique}"), "description": "A panel", "components": [
            {"id": "root", "component": "Text", "props": [{"key": "text", "static_value": "hi"}]}],
            "dependencies": [{"key": "camera"}], "demo_state": {}}],
    }))
    .unwrap();
    let mut tx = ctx.db.begin().await.unwrap();
    facade::registration::implement_agent(&mut tx, agent, &payload)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    (agent, identity.organization)
}

async fn implementation(ctx: &Context, agent: i64) -> (i64, i64) {
    sqlx::query_as("SELECT id, action_id FROM facade_implementation WHERE agent_id = $1")
        .bind(agent)
        .fetch_one(&ctx.db)
        .await
        .unwrap()
}

/// A started task of the agent that holds its lock and has an event and a child.
async fn work(ctx: &Context, agent: i64) -> i64 {
    let (implementation, action) = implementation(ctx, agent).await;
    let insert = "INSERT INTO facade_task (acted_on, ephemeral, reference, resumes, capture, is_higher_order_child,
                                  latest_event_kind, latest_instruct_kind, is_done, created_at,
                                  updated_at, revision, step, dispatch_attempts, trigger_depth, action_id, agent_id,
                                  implementation_id, parent_id, root_id)
         VALUES ('{}', false, $4, 0, false, false, 'STARTED', 'ASSIGN', false, now(), now(), 0, false, 1, 0, $2, $1, $3, $5, $5)
         RETURNING id";
    let task: i64 = sqlx::query_scalar(insert)
        .bind(agent)
        .bind(action)
        .bind(implementation)
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(None::<i64>)
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    let _child: i64 = sqlx::query_scalar(insert)
        .bind(agent)
        .bind(action)
        .bind(implementation)
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(Some(task))
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    facade::persist::state::on_agent_lock(ctx, agent, "stage", &task.to_string())
        .await
        .unwrap();
    facade::persist::positions::get_or_create_session(&ctx.db, agent, "session-1")
        .await
        .unwrap();
    facade::registration::shelve(&ctx.db, agent, "@x/y", "r1", None, None, false)
        .await
        .unwrap();
    task
}

/// How many rows of each agent-owned table still name the agent.
async fn remains(ctx: &Context, agent: i64) -> Vec<(String, i64)> {
    let mut left = vec![];
    for (table, column) in [
        ("facade_agent", "id"),
        ("facade_task", "agent_id"),
        ("facade_implementation", "agent_id"),
        ("facade_state", "agent_id"),
        ("facade_lock", "agent_id"),
        ("facade_session", "agent_id"),
        ("facade_memoryshelve", "agent_id"),
        ("facade_materializedblok", "declared_by_id"),
        ("facade_blokagentmapping", "agent_id"),
    ] {
        let count: i64 =
            sqlx::query_scalar(&format!("SELECT count(*) FROM {table} WHERE {column} = $1"))
                .bind(agent)
                .fetch_one(&ctx.db)
                .await
                .unwrap();
        if count > 0 {
            left.push((table.to_string(), count));
        }
    }
    left
}

fn forbidden<T: std::fmt::Debug>(result: Result<T, BackendError>) -> String {
    match result {
        Err(BackendError::Forbidden(message)) => message,
        other => panic!("expected a refusal, got {other:?}"),
    }
}

#[tokio::test]
async fn an_agent_goes_with_everything_below_it_and_only_within_its_organization() {
    let Some(ctx) = context().await else { return };
    let org = format!("removal-{}", uuid::Uuid::new_v4().simple());
    let (agent, organization) = registered(&ctx, &org).await;
    let (neighbour, _) = registered(&ctx, &org).await;
    let (_, elsewhere) = registered(&ctx, &format!("{org}-other")).await;
    work(&ctx, agent).await;
    let kept = work(&ctx, neighbour).await;
    // Away, with work queued for a socket that will never drain it.
    let mut redis = ctx.redis.clone();
    let _: i64 = redis::cmd("RPUSH")
        .arg(queue_key(&ctx.settings, agent))
        .arg("{}")
        .query_async(&mut redis)
        .await
        .unwrap();
    assert!(
        remains(&ctx, agent).await.len() >= 8,
        "the fixture reaches the cascade"
    );

    assert_eq!(
        forbidden(delete_agent(&ctx, elsewhere, &agent.to_string()).await),
        format!("No Agent {agent} in this organization.")
    );
    assert_eq!(
        remains(&ctx, agent).await.len(),
        remains(&ctx, neighbour).await.len()
    );

    assert_eq!(
        delete_agent(&ctx, organization, &agent.to_string())
            .await
            .unwrap(),
        agent
    );

    assert_eq!(
        remains(&ctx, agent).await,
        vec![],
        "nothing of the agent is left"
    );
    let queued: i64 = redis::cmd("EXISTS")
        .arg(queue_key(&ctx.settings, agent))
        .query_async(&mut redis)
        .await
        .unwrap();
    assert_eq!(queued, 0, "its queue is dropped");
    // The neighbour, of the same organization, keeps its rows, its task and its lock's holder.
    assert!(remains(&ctx, neighbour).await.len() >= 8);
    let holder: Option<i64> =
        sqlx::query_scalar("SELECT hold_by_id FROM facade_lock WHERE agent_id = $1")
            .bind(neighbour)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(holder, Some(kept));
    // Gone is gone: a second delete finds nothing.
    forbidden(delete_agent(&ctx, organization, &agent.to_string()).await);
}

#[tokio::test]
async fn an_implementation_is_deleted_only_by_its_organization() {
    let Some(ctx) = context().await else { return };
    let org = format!("removal-{}", uuid::Uuid::new_v4().simple());
    let (agent, organization) = registered(&ctx, &org).await;
    let (_, elsewhere) = registered(&ctx, &format!("{org}-other")).await;
    let task = work(&ctx, agent).await;
    let (implementation, _) = implementation(&ctx, agent).await;

    assert_eq!(
        forbidden(delete_implementation(&ctx, elsewhere, &implementation.to_string()).await),
        format!("No Implementation {implementation} in this organization.")
    );

    assert_eq!(
        delete_implementation(&ctx, organization, &implementation.to_string())
            .await
            .unwrap(),
        implementation
    );
    let left: i64 =
        sqlx::query_scalar("SELECT count(*) FROM facade_implementation WHERE agent_id = $1")
            .bind(agent)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(left, 0);
    // Its task stays, no longer naming it (SET_NULL); the agent and its lock stay.
    let names: Option<i64> =
        sqlx::query_scalar("SELECT implementation_id FROM facade_task WHERE id = $1")
            .bind(task)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(names, None);
    assert!(remains(&ctx, agent)
        .await
        .iter()
        .any(|(table, _)| table == "facade_lock"));
}

/// `cleanupActions`: the organization's actions nothing implements go, with their tasks; an
/// implemented action and another organization's stay.
#[tokio::test]
async fn only_the_organizations_unimplemented_actions_are_cleaned_up() {
    let Some(ctx) = context().await else { return };
    let org = format!("removal-{}", uuid::Uuid::new_v4().simple());
    let (orphaned, organization) = registered(&ctx, &org).await;
    let (kept, _) = registered(&ctx, &org).await;
    let (foreign, elsewhere) = registered(&ctx, &format!("{org}-other")).await;
    let task = work(&ctx, orphaned).await;
    let (orphaned_implementation, orphaned_action) = implementation(&ctx, orphaned).await;
    let (_, kept_action) = implementation(&ctx, kept).await;
    let (foreign_implementation, foreign_action) = implementation(&ctx, foreign).await;
    // Their implementations go: the first organization's action and the other's are unimplemented.
    delete_implementation(&ctx, organization, &orphaned_implementation.to_string())
        .await
        .unwrap();
    delete_implementation(&ctx, elsewhere, &foreign_implementation.to_string())
        .await
        .unwrap();

    // Asked for an action that is still implemented: nothing.
    assert_eq!(
        cleanup_actions(&ctx, organization, Some(&[kept_action]))
            .await
            .unwrap(),
        0
    );
    assert_eq!(cleanup_actions(&ctx, organization, None).await.unwrap(), 1);

    let left: Vec<i64> =
        sqlx::query_scalar("SELECT id FROM facade_action WHERE id = ANY($1) ORDER BY id")
            .bind(vec![orphaned_action, kept_action, foreign_action])
            .fetch_all(&ctx.db)
            .await
            .unwrap();
    let mut expected = vec![kept_action, foreign_action];
    expected.sort();
    assert_eq!(left, expected);
    let tasks: i64 = sqlx::query_scalar("SELECT count(*) FROM facade_task WHERE id = $1")
        .bind(task)
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    assert_eq!(tasks, 0, "an action's task history goes with it");
}
