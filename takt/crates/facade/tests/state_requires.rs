//! A task's `state_requires`: refused for an agent that does not test them and for a state or
//! path its declaration does not have, stored on the task, sent with every dispatch of it, and a
//! refusal's code kept on its FAILED event and passed to its caller. Needs
//! `TAKT_TEST_DATABASE_URL` and `TAKT_TEST_REDIS_URL` (`eval "$(scripts/test-db.sh)"`).

use std::sync::Arc;

use authentikate::base_models::StaticToken;
use chrono::Utc;
use facade::backend::{self, AssignInput, AssignOrigin};
use facade::caller_context::CallerContext;
use facade::message_router::route;
use facade::messages::{AgentFrame, StateRequirement, ToAgent};
use facade::provenance::keys::InstanceKey;
use facade::settings::Settings;
use facade::Context;
use redis::AsyncCommands;
use serde_json::{json, Value};

/// The conformance stack's instance key.
const PEM: &str = "-----BEGIN PRIVATE KEY-----\n\
    MC4CAQAwBQYDK2VwBCIEILK+rl9gVEjfKGiye+mLLjfEUGIdoP0WPC8lMZS3NYK2\n\
    -----END PRIVATE KEY-----\n";

async fn context() -> Option<Context> {
    let db_url = std::env::var("TAKT_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("TAKT_TEST_REDIS_URL").ok()?;
    let db = sqlx::postgres::PgPoolOptions::new()
        .max_connections(20)
        .connect(&db_url)
        .await
        .expect("the test database answers");
    let redis_client = redis::Client::open(redis_url).unwrap();
    let redis = redis::aio::ConnectionManager::new(redis_client.clone())
        .await
        .unwrap();
    let auth =
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
        settings: Arc::new(Settings {
            instance_key: Some(Arc::new(InstanceKey::from_pem(PEM).unwrap())),
            ..Settings::default()
        }),
        verifier: Arc::new(authentikate::Verifier::new(auth)),
        connections: Connections::default(),
        channel_layer,
    })
}

use facade::consumers::connections::Connections;

/// A live agent in its own organization, with a state `stage` that declares `busy` and `mode`.
/// It says it tests state requirements when `tests_them`.
async fn agent(ctx: &Context, tests_them: bool) -> i64 {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "state-requires-tests", "org": format!("o-{unique}"),
        "client_id": format!("c-{unique}"), "client_app": format!("a-{unique}"), "client_release": "1",
    }))
    .unwrap();
    let token = spec.to_token(Utc::now(), "raw");
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
    sqlx::query("UPDATE facade_agent SET connected = true, last_seen = now() WHERE id = $1")
        .bind(agent)
        .execute(&ctx.db)
        .await
        .unwrap();
    let features: Vec<String> = if tests_them {
        vec!["state_requires".into()]
    } else {
        vec![]
    };
    facade::registration::set_features(&ctx.db, agent, &features)
        .await
        .unwrap();
    sqlx::query(
        "WITH d AS (INSERT INTO facade_statedefinition (name, hash, ports, description, organization_id)
                    SELECT 'stage', $2, $3, '', organization_id FROM facade_agent WHERE id = $1 RETURNING id)
         INSERT INTO facade_state (interface, key, created_at, updated_at, agent_id, definition_id)
         SELECT 'stage', 'stage', now(), now(), $1, d.id FROM d",
    )
    .bind(agent)
    .bind(uuid::Uuid::new_v4().to_string())
    .bind(json!([{"key": "busy", "kind": "BOOL"}, {"key": "mode", "kind": "STRING"}]))
    .execute(&ctx.db)
    .await
    .unwrap();
    agent
}

/// An action of `agent` with one `INT` port `x`, and its implementation.
async fn implementation(ctx: &Context, agent: i64) -> i64 {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let ports =
        json!([{"key": "x", "kind": "INT", "nullable": false, "effects": [], "validators": []}]);
    let action: i64 = sqlx::query_scalar(
        "INSERT INTO facade_action (defined_at, key, version, pure, idempotent, allow_probe, stateful,
                                    kind, port_groups, name, description, scope, is_dev, hash, args, returns,
                                    arg_count, return_count, app_id, organization_id)
         SELECT now(), 'move', '1', false, false, false, false, 'FUNCTION', '[]', 'Move', '', 'GLOBAL', false, $2,
                $3, '[]', 1, 0, a.app_id, a.organization_id FROM facade_agent a WHERE a.id = $1
         RETURNING id",
    )
    .bind(agent)
    .bind(format!("hash-{unique}"))
    .bind(&ports)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    sqlx::query_scalar(
        "INSERT INTO facade_implementation (interface, higher_order_config, params, created_at,
                                            updated_at, tracks, diagnostics, needs_token, provenance_audience,
                                            effects, execution, action_id, agent_id)
         SELECT 'move', '{}', '{}', now(), now(), '[]', '[]', true, '[\"mikro\"]', 'UNKNOWN', 'PLAIN',
                $2, a.id FROM facade_agent a WHERE a.id = $1
         RETURNING id",
    )
    .bind(agent)
    .bind(action)
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

async fn principal(ctx: &Context, agent: i64) -> CallerContext {
    CallerContext::from_agent(&ctx.db, agent, vec![])
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

/// The frames queued for an agent, oldest first.
async fn queued(ctx: &Context, agent: i64) -> Vec<Value> {
    let mut redis = ctx.redis.clone();
    let frames: Vec<String> = redis
        .lrange(format!("rekuest:agent:{agent}:queue"), 0, -1)
        .await
        .unwrap();
    frames
        .iter()
        .rev()
        .map(|f| serde_json::from_str(f).unwrap())
        .collect()
}

fn requirement(path: &str, value: Value) -> StateRequirement {
    StateRequirement {
        state: "stage".into(),
        path: path.into(),
        operator: "EQUALS".into(),
        value,
    }
}

fn moving(implementation: i64, requirements: Vec<StateRequirement>) -> AssignInput {
    AssignInput {
        implementation: Some(implementation.to_string()),
        args: json!({"x": 1}).as_object().unwrap().clone(),
        state_requires: Some(requirements),
        ..AssignInput::default()
    }
}

async fn refusal(ctx: &Context, principal: &CallerContext, input: &AssignInput) -> String {
    backend::assign_with_status(ctx, principal, input, AssignOrigin::default())
        .await
        .unwrap_err()
        .to_string()
}

#[tokio::test]
async fn an_agent_that_does_not_test_requirements_is_sent_none() {
    let Some(ctx) = context().await else { return };
    let old = agent(&ctx, false).await;
    let implementation = implementation(&ctx, old).await;
    let principal = principal(&ctx, old).await;
    let refused = refusal(
        &ctx,
        &principal,
        &moving(implementation, vec![requirement("/busy", json!(false))]),
    )
    .await;
    assert!(
        refused.contains("does not test state requirements"),
        "{refused}"
    );
    assert!(queued(&ctx, old).await.is_empty());

    // Without requirements the same agent is assigned as before.
    backend::assign_with_status(
        &ctx,
        &principal,
        &moving(implementation, vec![]),
        AssignOrigin::default(),
    )
    .await
    .unwrap();
}

#[tokio::test]
async fn a_state_or_member_the_agent_does_not_declare_is_refused() {
    let Some(ctx) = context().await else { return };
    let executor = agent(&ctx, true).await;
    let implementation = implementation(&ctx, executor).await;
    let principal = principal(&ctx, executor).await;

    let mut laser = requirement("/on", json!(true));
    laser.state = "laser".into();
    let refused = refusal(&ctx, &principal, &moving(implementation, vec![laser])).await;
    assert!(refused.contains("has no such state"), "{refused}");

    let refused = refusal(
        &ctx,
        &principal,
        &moving(implementation, vec![requirement("/speed", json!(1))]),
    )
    .await;
    assert!(refused.contains("declares no \"speed\""), "{refused}");

    let mut unordered = requirement("/mode", json!("fast"));
    unordered.operator = "GTE".into();
    let refused = refusal(&ctx, &principal, &moving(implementation, vec![unordered])).await;
    assert!(refused.contains("GTE needs a numeric value"), "{refused}");
}

#[tokio::test]
async fn requirements_are_stored_and_sent_with_every_dispatch() {
    let Some(ctx) = context().await else { return };
    let executor = agent(&ctx, true).await;
    let implementation = implementation(&ctx, executor).await;
    let principal = principal(&ctx, executor).await;
    let wanted = vec![
        requirement("/busy", json!(false)),
        requirement("/mode", json!("idle")),
    ];

    let task = backend::assign_with_status(
        &ctx,
        &principal,
        &moving(implementation, wanted.clone()),
        AssignOrigin::default(),
    )
    .await
    .unwrap()
    .task;
    let sent = queued(&ctx, executor).await;
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0]["type"], "ASSIGN");
    assert_eq!(
        sent[0]["state_requires"],
        serde_json::to_value(&wanted).unwrap()
    );

    let stored: Option<Value> =
        sqlx::query_scalar("SELECT state_requires FROM facade_task WHERE id = $1")
            .bind(task)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(stored, Some(serde_json::to_value(&wanted).unwrap()));

    // A task sent again (delayed, held, redelivered) is built from the row: it carries them too.
    let again = facade::persist::reconcile::build_redispatch_assign(&ctx, task)
        .await
        .unwrap()
        .expect("the task can be sent again");
    assert_eq!(again.state_requires, wanted);

    // A task without requirements stores none and sends none.
    let plain = backend::assign_with_status(
        &ctx,
        &principal,
        &moving(implementation, vec![]),
        AssignOrigin::default(),
    )
    .await
    .unwrap()
    .task;
    let stored: Option<Value> =
        sqlx::query_scalar("SELECT state_requires FROM facade_task WHERE id = $1")
            .bind(plain)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(stored, None);
    assert!(queued(&ctx, executor).await[1]
        .get("state_requires")
        .is_none());
}

#[tokio::test]
async fn a_child_asked_for_over_the_socket_carries_its_requirements() {
    let Some(ctx) = context().await else { return };
    let caller = agent(&ctx, true).await;
    let executor = agent(&ctx, true).await;
    let parent_implementation = implementation(&ctx, caller).await;
    let implementation = implementation(&ctx, executor).await;
    let parent = backend::assign_with_status(
        &ctx,
        &principal(&ctx, caller).await,
        &AssignInput {
            policy: Some("AUTO".into()),
            ..moving(parent_implementation, vec![])
        },
        AssignOrigin::default(),
    )
    .await
    .unwrap()
    .task;
    let answer = route(
        &ctx,
        caller,
        &frame(json!({
            "type": "ASSIGN_REQUEST", "parent": parent.to_string(),
            "implementation": implementation.to_string(), "args": {"x": 1},
            "state_requires": [{"state": "stage", "path": "/busy", "operator": "EQUALS", "value": false}],
        })),
        None,
    )
    .await
    .unwrap();
    assert!(
        matches!(answer, Some(ToAgent::AssignResponse { task: Some(_), .. })),
        "{answer:?}"
    );
    let sent = queued(&ctx, executor).await;
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0]["state_requires"][0]["path"], "/busy");
}

#[tokio::test]
async fn a_refusal_keeps_its_code_on_the_failed_event() {
    let Some(ctx) = context().await else { return };
    let executor = agent(&ctx, true).await;
    let implementation = implementation(&ctx, executor).await;
    let task = backend::assign_with_status(
        &ctx,
        &principal(&ctx, executor).await,
        &moving(implementation, vec![requirement("/busy", json!(false))]),
        AssignOrigin::default(),
    )
    .await
    .unwrap()
    .task;
    route(
        &ctx,
        executor,
        &frame(json!({"type": "STARTED", "task": task.to_string()})),
        None,
    )
    .await
    .unwrap();
    route(
        &ctx,
        executor,
        &frame(json!({
            "type": "FAILED", "task": task.to_string(),
            "error": "stage/busy EQUALS false (actual true)", "code": "STATE_PRECONDITION",
        })),
        None,
    )
    .await
    .unwrap();
    let (message, value): (Option<String>, Option<Value>) = sqlx::query_as(
        "SELECT message, value FROM facade_taskevent WHERE task_id = $1 AND kind = 'FAILED'",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!(
        message.as_deref(),
        Some("stage/busy EQUALS false (actual true)")
    );
    assert_eq!(value, Some(json!({"code": "STATE_PRECONDITION"})));

    let mirror = facade::caller_events::build_execution_event(&facade::caller_events::EventLike {
        id: 1,
        task: task.to_string(),
        kind: "FAILED".into(),
        message,
        value,
        ..Default::default()
    });
    assert!(matches!(
        mirror,
        Some(ToAgent::FailedEvent { code: Some(ref code), .. }) if code == "STATE_PRECONDITION"
    ));
}

#[tokio::test]
async fn an_agent_saying_nothing_on_register_clears_what_it_said_before() {
    let Some(ctx) = context().await else { return };
    let executor = agent(&ctx, true).await;
    facade::registration::set_features(&ctx.db, executor, &[])
        .await
        .unwrap();
    let features: Option<Value> =
        sqlx::query_scalar("SELECT features FROM facade_agent WHERE id = $1")
            .bind(executor)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(features, None);
}
