//! An agent's reports, persisted: positions, the transition claim, terminals once, LOST and the
//! late report, effect keys, patches. Against a database the Python server migrated; needs
//! `TAKT_TEST_DATABASE_URL` and `TAKT_TEST_REDIS_URL` (`eval "$(scripts/test-db.sh)"`).

use std::sync::Arc;

use authentikate::base_models::StaticToken;
use facade::consumers::connections::Connections;
use facade::message_router::{route, RouteError};
use facade::messages::{AgentFrame, ToAgent};
use facade::persist::positions::{self, Position};
use facade::persist::transitions::{self, Claim};
use facade::settings::Settings;
use facade::Context;
use serde_json::{json, Value};

async fn context() -> Option<Context> {
    let db_url = std::env::var("TAKT_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("TAKT_TEST_REDIS_URL").ok()?;
    let db = sqlx::PgPool::connect(&db_url)
        .await
        .expect("the test database answers");
    let redis_client = redis::Client::open(redis_url).unwrap();
    let redis = redis::aio::ConnectionManager::new(redis_client.clone())
        .await
        .unwrap();
    let settings =
        authentikate::AuthentikateSettings::prepare(&json!({"audience": "rekuest"}), true).unwrap();
    let channel_layer = kante::ChannelLayer::new(
        redis_client.clone(),
        kante::ChannelLayerConfig {
            prefix: "rekuest".into(),
            ..kante::ChannelLayerConfig::default()
        },
    )
    .await
    .unwrap();
    Some(Context {
        db,
        redis,
        redis_client,
        settings: Arc::new(Settings::default()),
        verifier: Arc::new(authentikate::Verifier::new(settings)),
        connections: Connections::default(),
        channel_layer,
    })
}

/// A fresh agent (through the real token expansion and `ensure_agent`) and an action.
async fn agent(ctx: &Context) -> (i64, i64) {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "reports-tests", "org": format!("o-{unique}"),
        "client_id": format!("c-{unique}"), "client_app": format!("a-{unique}"), "client_release": "1",
    }))
    .unwrap();
    let token = spec.to_token(chrono::Utc::now(), "raw");
    let identity = authentikate::expand::expand_token_context(&ctx.db, &token)
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
    let action: i64 = sqlx::query_scalar(
        "INSERT INTO facade_action (defined_at, key, version, pure, idempotent, allow_probe, stateful,
                                    kind, port_groups, name, description, scope, is_dev, hash, args, returns,
                                    arg_count, return_count, app_id, organization_id)
         SELECT now(), $2, '1', false, false, false, false, 'FUNCTION', '[]', 'act', '', 'GLOBAL', false, $2,
                '[]', '[]', 0, 0, a.app_id, a.organization_id FROM facade_agent a WHERE a.id = $1
         RETURNING id",
    )
    .bind(agent)
    .bind(format!("act-{unique}"))
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    (agent, action)
}

/// An open task of `agent`, QUEUED and dispatched, as `assign` leaves it.
async fn task(ctx: &Context, agent: i64, action: i64) -> i64 {
    sqlx::query_scalar(
        "INSERT INTO facade_task (acted_on, ephemeral, reference, resumes, capture, is_higher_order_child,
                                  latest_event_kind, latest_instruct_kind, is_done, created_at,
                                  updated_at, revision, step, dispatch_attempts, trigger_depth, action_id, agent_id,
                                  dispatched_at)
         VALUES ('{}', false, $3, 0, false, false, 'QUEUED', 'ASSIGN', false, now(), now(), 0, false, 1, 0,
                 $2, $1, now())
         RETURNING id",
    )
    .bind(agent)
    .bind(action)
    .bind(uuid::Uuid::new_v4().to_string())
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

fn frame(value: Value) -> AgentFrame {
    let mut value = value;
    value
        .as_object_mut()
        .unwrap()
        .entry("id")
        .or_insert_with(|| json!(uuid::Uuid::new_v4().to_string()));
    serde_json::from_value(value).unwrap()
}

async fn kinds(ctx: &Context, task: i64) -> Vec<String> {
    sqlx::query_scalar("SELECT kind FROM facade_taskevent WHERE task_id = $1 ORDER BY id")
        .bind(task)
        .fetch_all(&ctx.db)
        .await
        .unwrap()
}

async fn latest(ctx: &Context, task: i64) -> (String, bool, i64) {
    sqlx::query_as("SELECT latest_event_kind, is_done, revision FROM facade_task WHERE id = $1")
        .bind(task)
        .fetch_one(&ctx.db)
        .await
        .unwrap()
}

#[tokio::test]
async fn positions_claim_once_skip_resends_and_release_on_failure() {
    let Some(ctx) = context().await else { return };
    let (agent, _) = agent(&ctx).await;
    let session = format!("js-{}", uuid::Uuid::new_v4());

    assert_eq!(
        positions::claim_position(&ctx.db, agent, &session, 1)
            .await
            .unwrap(),
        Position::Project
    );
    positions::confirm_position(&ctx.db, agent, &session, 1)
        .await
        .unwrap();
    assert_eq!(
        positions::claim_position(&ctx.db, agent, &session, 1)
            .await
            .unwrap(),
        Position::Duplicate
    );

    // A gap: 2 never arrives, 3 is projected and the watermark moves past it.
    assert_eq!(
        positions::claim_position(&ctx.db, agent, &session, 3)
            .await
            .unwrap(),
        Position::Project
    );
    positions::release_position(&ctx.db, agent, &session, 3)
        .await
        .unwrap();
    assert_eq!(
        positions::projected_position(&ctx.db, agent, &session)
            .await
            .unwrap(),
        2
    );
    assert_eq!(
        positions::claim_position(&ctx.db, agent, &session, 3)
            .await
            .unwrap(),
        Position::Project
    );
    positions::confirm_position(&ctx.db, agent, &session, 3)
        .await
        .unwrap();
    assert_eq!(
        positions::projected_position(&ctx.db, agent, &session)
            .await
            .unwrap(),
        3
    );
}

#[tokio::test]
async fn concurrent_claims_have_one_winner_and_one_event() {
    let Some(ctx) = context().await else { return };
    let (agent, action) = agent(&ctx).await;
    let task = task(&ctx, agent, action).await;

    let claims = (0..8).map(|_| {
        let ctx = ctx.clone();
        tokio::spawn(async move {
            transitions::claim(
                &ctx,
                task,
                Claim {
                    mark_done: true,
                    ..Claim::to("COMPLETED")
                },
            )
            .await
            .unwrap()
        })
    });
    let won: Vec<bool> = futures::future::join_all(claims)
        .await
        .into_iter()
        .map(Result::unwrap)
        .collect();

    assert_eq!(won.iter().filter(|w| **w).count(), 1);
    assert_eq!(kinds(&ctx, task).await, vec!["COMPLETED"]);
    let (kind, done, revision) = latest(&ctx, task).await;
    assert_eq!(
        (kind.as_str(), done, revision),
        ("COMPLETED", true, 1),
        "one save, one revision bump"
    );
}

#[tokio::test]
async fn reports_are_persisted_once_and_acked() {
    let Some(ctx) = context().await else { return };
    let (agent, action) = agent(&ctx).await;
    let task = task(&ctx, agent, action).await;
    let t = task.to_string();

    assert!(matches!(
        route(
            &ctx,
            agent,
            &frame(json!({"type": "STARTED", "task": t, "seq": 1})),
            None
        )
        .await
        .unwrap(),
        Some(ToAgent::EventAck { .. })
    ));
    route(
        &ctx,
        agent,
        &frame(json!({"type": "PROGRESS", "task": t, "progress": 40})),
        None,
    )
    .await
    .unwrap();
    route(
        &ctx,
        agent,
        &frame(json!({"type": "YIELD", "task": t, "returns": {"return0": 2}})),
        None,
    )
    .await
    .unwrap();
    let completed = frame(json!({"type": "COMPLETED", "task": t, "seq": 4}));
    assert!(matches!(
        route(&ctx, agent, &completed, None).await.unwrap(),
        Some(ToAgent::EventAck { .. })
    ));
    // The agent retries an unacked terminal: acked again, recorded once.
    assert!(matches!(
        route(&ctx, agent, &completed, None).await.unwrap(),
        Some(ToAgent::EventAck { .. })
    ));

    assert_eq!(
        kinds(&ctx, task).await,
        vec!["STARTED", "PROGRESS", "YIELD", "COMPLETED"]
    );
    let picked: bool =
        sqlx::query_scalar("SELECT picked_up_at IS NOT NULL FROM facade_task WHERE id = $1")
            .bind(task)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert!(picked, "the first report stamps picked_up_at");
    assert_eq!(latest(&ctx, task).await.0, "COMPLETED");
}

#[tokio::test]
async fn numbered_frames_are_projected_once_and_never_event_acked() {
    let Some(ctx) = context().await else { return };
    let (agent, action) = agent(&ctx).await;
    let task = task(&ctx, agent, action).await;
    let session = format!("js-{}", uuid::Uuid::new_v4());
    let numbered = |pos: u64, body: Value| {
        let mut body = body;
        body["pos"] = json!(pos);
        body["journal_session"] = json!(session);
        body["task_step"] = json!(pos);
        body["agent_ts"] = json!(1790715513.5);
        frame(body)
    };
    let t = task.to_string();
    for (pos, body) in [
        (1, json!({"type": "STARTED", "task": t})),
        (
            2,
            json!({"type": "LOG", "task": t, "message": "hi", "level": "INFO"}),
        ),
    ] {
        assert!(route(&ctx, agent, &numbered(pos, body), None)
            .await
            .unwrap()
            .is_none());
    }
    // A resend of position 2 after a reconnect: skipped.
    route(
        &ctx,
        agent,
        &numbered(
            2,
            json!({"type": "LOG", "task": t, "message": "hi", "level": "INFO"}),
        ),
        None,
    )
    .await
    .unwrap();
    assert_eq!(kinds(&ctx, task).await, vec!["STARTED", "LOG"]);
    let (pos, step): (Option<i64>, Option<i64>) = sqlx::query_as(
        "SELECT agent_pos, step FROM facade_taskevent WHERE task_id = $1 AND kind = 'LOG'",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!((pos, step), (Some(2), Some(2)));
    assert_eq!(
        positions::projected_position(&ctx.db, agent, &session)
            .await
            .unwrap(),
        2
    );

    // Another agent's task, numbered: a refusal counts as handled, the watermark moves on.
    let (stranger, stranger_action) = self::agent(&ctx).await;
    let foreign = self::task(&ctx, stranger, stranger_action).await;
    route(
        &ctx,
        agent,
        &numbered(3, json!({"type": "COMPLETED", "task": foreign.to_string()})),
        None,
    )
    .await
    .unwrap();
    assert!(
        kinds(&ctx, foreign).await.is_empty(),
        "another agent's task is not touched"
    );
    assert_eq!(
        positions::projected_position(&ctx.db, agent, &session)
            .await
            .unwrap(),
        3
    );
}

#[tokio::test]
async fn a_report_after_lost_is_a_late_report_and_lost_stays() {
    let Some(ctx) = context().await else { return };
    let (agent, action) = agent(&ctx).await;
    let task = task(&ctx, agent, action).await;
    let t = task.to_string();
    route(
        &ctx,
        agent,
        &frame(json!({"type": "PROGRESS", "task": t, "progress": 60})),
        None,
    )
    .await
    .unwrap();

    assert!(transitions::finalize_lost(
        &ctx,
        task,
        "Its agent died while it ran; how it ended is unknown.",
        true,
        None,
        false
    )
    .await
    .unwrap());
    let details: Value = sqlx::query_scalar(
        "SELECT value FROM facade_taskevent WHERE task_id = $1 AND kind = 'LOST'",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!(details["started"], json!(true));
    assert_eq!(details["last_progress"], json!(60));
    assert_eq!(details["effects"], json!("UNKNOWN"));

    route(
        &ctx,
        agent,
        &frame(json!({"type": "YIELD", "task": t, "returns": {"return0": 1}})),
        None,
    )
    .await
    .unwrap();
    route(
        &ctx,
        agent,
        &frame(json!({"type": "COMPLETED", "task": t})),
        None,
    )
    .await
    .unwrap();

    assert_eq!(latest(&ctx, task).await.0, "LOST");
    assert_eq!(
        kinds(&ctx, task).await,
        vec!["PROGRESS", "LOST", "LATE_REPORT", "LATE_REPORT"]
    );
    let late: Value = sqlx::query_scalar("SELECT value FROM facade_taskevent WHERE task_id = $1 AND kind = 'LATE_REPORT' ORDER BY id DESC LIMIT 1")
        .bind(task)
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    assert_eq!(late, json!({"kind": "COMPLETED"}));
}

#[tokio::test]
async fn an_effect_key_is_recorded_once() {
    let Some(ctx) = context().await else { return };
    let (agent, action) = agent(&ctx).await;
    let task = task(&ctx, agent, action).await;
    let t = task.to_string();
    for value in [1.0, 2.0] {
        route(&ctx, agent, &frame(json!({"type": "EFFECT", "task": t, "effect": "NOW", "value": value, "key": "NOW:1"})), None).await.unwrap();
    }
    let values: Vec<Value> = sqlx::query_scalar(
        "SELECT value FROM facade_taskevent WHERE task_id = $1 AND kind = 'EFFECT'",
    )
    .bind(task)
    .fetch_all(&ctx.db)
    .await
    .unwrap();
    assert_eq!(values, vec![json!(1.0)], "the value recorded first stands");
}

#[tokio::test]
async fn patches_link_own_tasks_and_drop_duplicate_revisions() {
    let Some(ctx) = context().await else { return };
    let (agent, action) = agent(&ctx).await;
    let own = task(&ctx, agent, action).await;
    let (stranger, stranger_action) = self::agent(&ctx).await;
    let foreign = task(&ctx, stranger, stranger_action).await;
    sqlx::query(
        "WITH d AS (
            INSERT INTO facade_statedefinition (name, hash, ports, description, organization_id)
            SELECT 'plate', $2, '[]', 'A state definition', organization_id FROM facade_agent WHERE id = $1 RETURNING id)
         INSERT INTO facade_state (interface, created_at, updated_at, agent_id, definition_id)
         SELECT 'plate', now(), now(), $1, d.id FROM d",
    )
    .bind(agent)
    .bind(uuid::Uuid::new_v4().to_string())
    .execute(&ctx.db)
    .await
    .unwrap();
    let patch = |rev: u64, task: i64| {
        frame(
            json!({"type": "STATE_PATCH", "session_id": "s1", "global_rev": rev, "state_name": "plate", "ts": 1.0,
                     "op": "replace", "path": "/barcode", "value": "B1", "old_value": null, "task_id": task.to_string()}),
        )
    };
    route(&ctx, agent, &patch(1, own), None).await.unwrap();
    route(&ctx, agent, &patch(1, own), None).await.unwrap(); // the same revision again: dropped
    route(&ctx, agent, &patch(2, foreign), None).await.unwrap();

    let rows: Vec<(i32, Option<i64>)> = sqlx::query_as(
        "SELECT p.global_rev, p.task_id FROM facade_patch p JOIN facade_state s ON s.id = p.state_id WHERE s.agent_id = $1 ORDER BY p.global_rev",
    )
    .bind(agent)
    .fetch_all(&ctx.db)
    .await
    .unwrap();
    assert_eq!(
        rows,
        vec![(1, Some(own)), (2, None)],
        "another agent's task is never linked"
    );

    // A patch for a state the agent never declared closes an unnumbered connection, as in Python.
    let unknown = frame(
        json!({"type": "STATE_PATCH", "session_id": "s1", "global_rev": 3, "state_name": "nope", "ts": 1.0,
                               "op": "replace", "path": "/x", "value": 1, "old_value": null}),
    );
    assert!(matches!(
        route(&ctx, agent, &unknown, None).await,
        Err(RouteError::Refused(_))
    ));
}
