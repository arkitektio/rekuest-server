//! Assign, control, guards and probes against a database the Python server migrated: the
//! idempotency of an assign (a concurrent duplicate included), control ownership and its
//! escalation columns, a workflow's guard, and a probe's life in redis. Needs
//! `AGENTD_TEST_DATABASE_URL` and `AGENTD_TEST_REDIS_URL` (`eval "$(scripts/test-db.sh)"`).

use std::sync::Arc;

use authentikate::base_models::StaticToken;
use chrono::{DateTime, Utc};
use facade::backend::{self, AssignInput, AssignOrigin, BackendError, Control};
use facade::caller_context::CallerContext;
use facade::caller_events::mirror_of_channel_message;
use facade::consumers::agent_protocol::child_mirrors;
use facade::consumers::connections::Connections;
use facade::message_router::route;
use facade::messages::{AgentFrame, ToAgent};
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
    let db_url = std::env::var("AGENTD_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("AGENTD_TEST_REDIS_URL").ok()?;
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

/// A live agent (through the real token expansion and `ensure_agent`) in its own organization,
/// or in `organization` when given.
async fn agent(ctx: &Context, organization: Option<&str>) -> i64 {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let org = organization.map_or(format!("o-{unique}"), str::to_owned);
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "assign-tests", "org": org,
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
    agent
}

/// An action of `agent` with one `INT` port `x`, and its implementation: `(action, implementation)`.
async fn action(ctx: &Context, agent: i64, allow_probe: bool) -> (i64, i64) {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let ports =
        json!([{"key": "x", "kind": "INT", "nullable": false, "effects": [], "validators": []}]);
    let action: i64 = sqlx::query_scalar(
        "INSERT INTO facade_action (defined_at, embedding_model, key, version, pure, idempotent, allow_probe, stateful,
                                    kind, port_groups, name, description, scope, is_dev, hash, args, returns,
                                    arg_count, return_count, app_id, organization_id)
         SELECT now(), '', 'echo', '1', false, false, $3, false, 'FUNCTION', '[]', 'Echo', '', 'GLOBAL', false, $2,
                $4, '[]', 1, 0, a.app_id, a.organization_id FROM facade_agent a WHERE a.id = $1
         RETURNING id",
    )
    .bind(agent)
    .bind(format!("hash-{unique}"))
    .bind(allow_probe)
    .bind(&ports)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    let implementation: i64 = sqlx::query_scalar(
        "INSERT INTO facade_implementation (interface, name, policy, higher_order_config, params, created_at,
                                            updated_at, tracks, diagnostics, needs_token, provenance_audience,
                                            effects, execution, action_id, agent_id, release_id)
         SELECT 'echo', 'echo', '{}', '{}', '{}', now(), now(), '[]', '[]', true, '[\"mikro\"]', 'UNKNOWN', 'PLAIN',
                $2, a.id, a.release_id FROM facade_agent a WHERE a.id = $1
         RETURNING id",
    )
    .bind(agent)
    .bind(action)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    (action, implementation)
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

fn echo(implementation: i64, x: i64, reference: Option<&str>) -> AssignInput {
    AssignInput {
        implementation: Some(implementation.to_string()),
        args: json!({"x": x}).as_object().unwrap().clone(),
        reference: reference.map(str::to_owned),
        ..AssignInput::default()
    }
}

#[derive(Debug, sqlx::FromRow)]
struct Row {
    latest_event_kind: String,
    latest_instruct_kind: String,
    is_done: bool,
    dispatched_at: Option<DateTime<Utc>>,
    dispatch_attempts: i16,
    interrupt_at: Option<DateTime<Utc>>,
    root_id: Option<i64>,
    parent_step: Option<i64>,
    caller_id: Option<i64>,
    args_hash: Option<String>,
    revision: i64,
}

async fn row(ctx: &Context, task: i64) -> Row {
    sqlx::query_as(
        "SELECT latest_event_kind, latest_instruct_kind, is_done, dispatched_at, dispatch_attempts,
                interrupt_at, root_id, parent_step, caller_id, args_hash, revision
           FROM facade_task WHERE id = $1",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

async fn events(ctx: &Context, task: i64) -> Vec<(String, Option<String>)> {
    sqlx::query_as("SELECT kind, message FROM facade_taskevent WHERE task_id = $1 ORDER BY id")
        .bind(task)
        .fetch_all(&ctx.db)
        .await
        .unwrap()
}

async fn instructs(ctx: &Context, task: i64) -> Vec<(String, Option<i64>)> {
    sqlx::query_as("SELECT kind, caller_id FROM facade_taskinstruct WHERE task_id = $1 ORDER BY id")
        .bind(task)
        .fetch_all(&ctx.db)
        .await
        .unwrap()
}

#[tokio::test]
async fn an_assign_is_persisted_dispatched_once_and_deduped() {
    let Some(ctx) = context().await else { return };
    let executor = agent(&ctx, None).await;
    let (_, implementation) = action(&ctx, executor, false).await;
    let principal = principal(&ctx, executor).await;
    let reference = uuid::Uuid::new_v4().to_string();

    let first = backend::assign_with_status(
        &ctx,
        &principal,
        &echo(implementation, 3, Some(&reference)),
        AssignOrigin::default(),
    )
    .await
    .unwrap();
    assert!(first.created);
    assert_eq!(first.reference, reference);
    let task = row(&ctx, first.task).await;
    assert_eq!(
        (
            task.latest_event_kind.as_str(),
            task.latest_instruct_kind.as_str(),
            task.is_done,
            task.dispatch_attempts,
            task.revision
        ),
        ("QUEUED", "ASSIGN", false, 1, 1)
    );
    assert!(task.dispatched_at.is_some() && task.caller_id.is_some());
    // python: hashlib.sha256(b'{"x":3}').hexdigest()
    assert_eq!(
        task.args_hash.as_deref(),
        Some("54afd0d590e6b277d9cd1ce46e5480f683682e43b6b480f7d24906d9e935e44c")
    );

    let frames = queued(&ctx, executor).await;
    assert_eq!(frames.len(), 1, "{frames:?}");
    let assign = &frames[0];
    assert_eq!(assign["type"], "ASSIGN");
    assert_eq!(assign["task"], first.task.to_string());
    assert_eq!(assign["args"], json!({"x": 3}));
    assert_eq!(assign["user"], principal.user_sub);
    let token = assign["token"].as_str().expect("a provenance token");
    let (header, claims) = ctx
        .settings
        .instance_key
        .as_ref()
        .unwrap()
        .verify_jwt(token)
        .unwrap();
    assert_eq!(header["alg"], "Ed25519");
    assert_eq!(claims["tsk"], first.task.to_string());
    assert_eq!(claims["rtk"], first.task.to_string());
    assert_eq!(claims["aud"], json!(["mikro"]));
    assert_eq!(claims["ahs"], json!(task.args_hash));

    let again = backend::assign_with_status(
        &ctx,
        &principal,
        &echo(implementation, 3, Some(&reference)),
        AssignOrigin::default(),
    )
    .await
    .unwrap();
    assert_eq!((again.task, again.created), (first.task, false));
    assert_eq!(
        queued(&ctx, executor).await.len(),
        1,
        "a resend is not dispatched"
    );
}

#[tokio::test]
async fn a_concurrent_duplicate_has_one_winner() {
    let Some(ctx) = context().await else { return };
    let executor = agent(&ctx, None).await;
    let (_, implementation) = action(&ctx, executor, false).await;
    let principal = principal(&ctx, executor).await;
    let reference = uuid::Uuid::new_v4().to_string();

    let racers = (0..8).map(|_| {
        let (ctx, principal, reference) = (ctx.clone(), principal.clone(), reference.clone());
        tokio::spawn(async move {
            backend::assign_with_status(
                &ctx,
                &principal,
                &echo(implementation, 1, Some(&reference)),
                AssignOrigin::default(),
            )
            .await
            .unwrap()
        })
    });
    let assigned: Vec<_> = futures::future::join_all(racers)
        .await
        .into_iter()
        .map(Result::unwrap)
        .collect();
    assert_eq!(
        assigned.iter().filter(|a| a.created).count(),
        1,
        "{assigned:?}"
    );
    assert!(assigned.iter().all(|a| a.task == assigned[0].task));
    assert_eq!(
        queued(&ctx, executor).await.len(),
        1,
        "the losers do not dispatch"
    );
}

#[tokio::test]
async fn children_dedupe_by_step_and_key_and_a_changed_call_is_refused() {
    let Some(ctx) = context().await else { return };
    let executor = agent(&ctx, None).await;
    let (_, implementation) = action(&ctx, executor, false).await;
    let principal = principal(&ctx, executor).await;
    let parent = backend::assign_with_status(
        &ctx,
        &principal,
        &echo(implementation, 1, None),
        AssignOrigin::default(),
    )
    .await
    .unwrap()
    .task;

    let child = |reference: &str, step: Option<i64>, key: Option<&str>| AssignInput {
        parent: Some(parent.to_string()),
        parent_step: step,
        call_key: key.map(str::to_owned),
        ..echo(implementation, 2, Some(reference))
    };
    let stepped = backend::assign_with_status(
        &ctx,
        &principal,
        &child("a", Some(3), None),
        AssignOrigin::default(),
    )
    .await
    .unwrap();
    assert!(stepped.created);
    let task = row(&ctx, stepped.task).await;
    assert_eq!((task.root_id, task.parent_step), (Some(parent), Some(3)));
    // Re-issued after a restart: a fresh reference, the same step.
    let again = backend::assign_with_status(
        &ctx,
        &principal,
        &child("b", Some(3), None),
        AssignOrigin::default(),
    )
    .await
    .unwrap();
    assert_eq!((again.task, again.created), (stepped.task, false));

    let keyed = backend::assign_with_status(
        &ctx,
        &principal,
        &child("c", None, Some("k")),
        AssignOrigin::default(),
    )
    .await
    .unwrap();
    assert!(keyed.created);
    let elsewhere = AssignInput {
        action_hash: Some("another-action".into()),
        ..child("d", None, Some("k"))
    };
    let refused =
        backend::assign_with_status(&ctx, &principal, &elsewhere, AssignOrigin::default())
            .await
            .unwrap_err();
    assert!(
        refused
            .to_string()
            .starts_with("Nondeterministic workflow: call 'k' of task"),
        "{refused}"
    );
}

#[tokio::test]
async fn args_are_validated_and_roots_come_only_from_humans() {
    let Some(ctx) = context().await else { return };
    let executor = agent(&ctx, None).await;
    let (_, implementation) = action(&ctx, executor, false).await;
    let principal = principal(&ctx, executor).await;

    let unknown = AssignInput {
        args: json!({"x": 1, "y": 2}).as_object().unwrap().clone(),
        ..echo(implementation, 1, None)
    };
    assert_eq!(
        backend::assign_with_status(&ctx, &principal, &unknown, AssignOrigin::default())
            .await
            .unwrap_err()
            .to_string(),
        "Unknown arguments ['y']; this action accepts ['x']"
    );

    // Over the socket, a parentless assign is refused, and answered rather than fatal.
    let reply = route(
        &ctx,
        executor,
        &frame(json!({"type": "ASSIGN_REQUEST", "reference": "r", "implementation": implementation.to_string(), "args": {"x": 1}})),
        None,
    )
    .await
    .unwrap();
    let Some(ToAgent::AssignResponse {
        task: None,
        created: false,
        error: Some(error),
        reference,
        ..
    }) = reply
    else {
        panic!("{reply:?}")
    };
    assert_eq!(reference, "r");
    assert!(
        error.starts_with("An agent may only assign dependent work"),
        "{error}"
    );
}

#[tokio::test]
async fn an_assign_request_answers_and_a_resend_is_not_created_again() {
    let Some(ctx) = context().await else { return };
    let caller = agent(&ctx, None).await;
    let executor = agent(&ctx, None).await;
    let (_, parent_implementation) = action(&ctx, caller, false).await;
    // Addressed by agent + interface, the executor's implementation of `echo`.
    action(&ctx, executor, false).await;
    let parent = backend::assign_with_status(
        &ctx,
        &principal(&ctx, caller).await,
        &echo(parent_implementation, 1, None),
        AssignOrigin::default(),
    )
    .await
    .unwrap()
    .task;

    let request = json!({
        "type": "ASSIGN_REQUEST", "reference": "child-1", "parent": parent.to_string(),
        "agent": executor.to_string(), "interface": "echo", "args": {"x": 5},
    });
    let reply = route(&ctx, caller, &frame(request.clone()), None)
        .await
        .unwrap();
    let Some(ToAgent::AssignResponse {
        task: Some(task),
        created: true,
        error: None,
        ..
    }) = reply
    else {
        panic!("{reply:?}")
    };
    let frames = queued(&ctx, executor).await;
    assert_eq!(frames.last().unwrap()["task"], task);
    assert_eq!(frames.last().unwrap()["parent"], parent.to_string());

    // The executor reports; the caller, restarted, asks for the same child again.
    for kind in ["STARTED", "COMPLETED"] {
        route(
            &ctx,
            executor,
            &frame(json!({"type": kind, "task": task})),
            None,
        )
        .await
        .unwrap();
    }
    let reply = route(&ctx, caller, &frame(request), None).await.unwrap();
    assert!(
        matches!(&reply, Some(ToAgent::AssignResponse { task: Some(t), created: false, .. }) if *t == task),
        "{reply:?}"
    );
    // What follows the answer: the child's events so far, as the mirrors the live ones were.
    let ids: Vec<i64> =
        sqlx::query_scalar("SELECT id FROM facade_taskevent WHERE task_id = $1 ORDER BY id")
            .bind(task.parse::<i64>().unwrap())
            .fetch_all(&ctx.db)
            .await
            .unwrap();
    let replayed: Vec<Value> = child_mirrors(&ctx, &task)
        .await
        .unwrap()
        .iter()
        .map(|m| serde_json::to_value(m).unwrap())
        .collect();
    assert_eq!(
        replayed,
        vec![
            json!({"type": "STARTED_EVENT", "task": task, "event": ids[0].to_string(), "seq": ids[0]}),
            json!({"type": "COMPLETED_EVENT", "task": task, "event": ids[1].to_string(), "seq": ids[1]}),
        ]
    );
}

#[tokio::test]
async fn only_the_caller_controls_and_the_deadlines_are_columns() {
    let Some(ctx) = context().await else { return };
    let organization = format!("o-{}", uuid::Uuid::new_v4().simple());
    let caller = agent(&ctx, Some(&organization)).await;
    let stranger = agent(&ctx, Some(&organization)).await;
    let executor = agent(&ctx, Some(&organization)).await;
    let (_, parent_implementation) = action(&ctx, caller, false).await;
    let (_, implementation) = action(&ctx, executor, false).await;
    let parent = backend::assign_with_status(
        &ctx,
        &principal(&ctx, caller).await,
        &echo(parent_implementation, 1, None),
        AssignOrigin::default(),
    )
    .await
    .unwrap()
    .task;
    let assign = |reference: &str| {
        frame(json!({
            "type": "ASSIGN_REQUEST", "reference": reference, "parent": parent.to_string(),
            "implementation": implementation.to_string(), "args": {"x": 1},
        }))
    };
    let Some(ToAgent::AssignResponse {
        task: Some(child), ..
    }) = route(&ctx, caller, &assign("c1"), None).await.unwrap()
    else {
        panic!()
    };

    // Not its caller.
    let reply = route(
        &ctx,
        stranger,
        &frame(json!({"type": "CANCEL_REQUEST", "task": child})),
        None,
    )
    .await
    .unwrap();
    assert_eq!(
        reply,
        Some(ToAgent::ControlResponse {
            request: match &reply {
                Some(ToAgent::ControlResponse { request, .. }) => request.clone(),
                _ => unreachable!(),
            },
            task: Some(child.clone()),
            accepted: false,
            error: Some("Not authorized to control this task (not its caller).".into()),
        })
    );

    // The caller's socket sits in its caller group: subscribe as it does.
    let caller_id = facade::persist::caller_ops::get_or_create_caller_id(&ctx.db, caller)
        .await
        .unwrap();
    let (channel, mut inbox) = ctx.channel_layer.subscribe("specific").await.unwrap();
    ctx.channel_layer
        .group_add(&format!("task_caller_{caller_id}"), &channel)
        .await
        .unwrap();

    // Its caller: CANCELLING, the instruct row, a deadline of auto_interrupt, the frame sent.
    let before = Utc::now();
    let reply = route(
        &ctx,
        caller,
        &frame(json!({"type": "CANCEL_REQUEST", "task": child, "auto_interrupt": 5.0})),
        None,
    )
    .await
    .unwrap();
    assert!(
        matches!(&reply, Some(ToAgent::ControlResponse { accepted: true, task: Some(t), .. }) if *t == child),
        "{reply:?}"
    );
    let child_id: i64 = child.parse().unwrap();
    let task = row(&ctx, child_id).await;
    assert_eq!(task.latest_instruct_kind, "CANCEL");
    let deadline = task.interrupt_at.unwrap() - before;
    assert!(
        deadline > chrono::Duration::seconds(4) && deadline < chrono::Duration::seconds(7),
        "{deadline}"
    );
    assert_eq!(
        events(&ctx, child_id).await,
        vec![("CANCELLING".into(), None)]
    );
    // ... and mirrored to the caller as CANCELLING_EVENT, `seq` the event's id.
    let event_id: i64 = sqlx::query_scalar("SELECT id FROM facade_taskevent WHERE task_id = $1")
        .bind(child_id)
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    let mirror = loop {
        let message = tokio::time::timeout(std::time::Duration::from_secs(5), inbox.recv())
            .await
            .expect("the event reaches the caller group")
            .unwrap();
        if let Some(mirror) = mirror_of_channel_message(&message) {
            break mirror;
        }
    };
    assert_eq!(
        serde_json::to_value(&mirror).unwrap(),
        json!({"type": "CANCELLING_EVENT", "task": child, "event": event_id.to_string(), "seq": event_id})
    );
    ctx.channel_layer.unsubscribe(&channel);
    let caller_row: i64 = sqlx::query_scalar("SELECT caller_id FROM facade_task WHERE id = $1")
        .bind(child_id)
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    assert_eq!(
        instructs(&ctx, child_id).await,
        vec![("CANCEL".into(), Some(caller_row))]
    );
    let frames = queued(&ctx, executor).await;
    assert_eq!(frames.last().unwrap()["type"], "CANCEL");

    // A pause arms no deadline (it clears one).
    route(
        &ctx,
        caller,
        &frame(json!({"type": "PAUSE_REQUEST", "task": child})),
        None,
    )
    .await
    .unwrap();
    assert_eq!(row(&ctx, child_id).await.interrupt_at, None);

    // An interrupt of the root reaches the open descendants, with the global deadline.
    backend::request_control(&ctx, &parent.to_string(), Control::Interrupt, None)
        .await
        .unwrap();
    for task in [parent, child_id] {
        let row = row(&ctx, task).await;
        assert_eq!(row.latest_instruct_kind, "INTERRUPT");
        let deadline = row.interrupt_at.unwrap() - Utc::now();
        assert!(deadline > chrono::Duration::seconds(55), "{deadline}");
        assert_eq!(events(&ctx, task).await.last().unwrap().0, "INTERRUPTING");
    }

    sqlx::query("UPDATE facade_task SET is_done = true WHERE id = $1")
        .bind(child_id)
        .execute(&ctx.db)
        .await
        .unwrap();
    let reply = route(
        &ctx,
        caller,
        &frame(json!({"type": "RESUME_REQUEST", "task": child})),
        None,
    )
    .await
    .unwrap();
    assert!(
        matches!(&reply, Some(ToAgent::ControlResponse { accepted: false, error: Some(e), .. }) if e == "Task is already terminal"),
        "{reply:?}"
    );
}

#[tokio::test]
async fn a_delayed_task_waits_and_a_cancel_settles_it() {
    let Some(ctx) = context().await else { return };
    let executor = agent(&ctx, None).await;
    let (_, implementation) = action(&ctx, executor, false).await;
    let principal = principal(&ctx, executor).await;
    let delayed = AssignInput {
        not_before: Some(Utc::now() + chrono::Duration::hours(1)),
        ..echo(implementation, 1, None)
    };
    let assigned = backend::assign_with_status(&ctx, &principal, &delayed, AssignOrigin::default())
        .await
        .unwrap();
    let task = row(&ctx, assigned.task).await;
    assert_eq!((task.dispatched_at, task.dispatch_attempts), (None, 0));
    assert!(
        queued(&ctx, executor).await.is_empty(),
        "not handed over yet"
    );

    let with_hooks = AssignInput {
        hooks: Some(vec![backend::HookInput {
            kind: "INIT".into(),
            hash: "h".into(),
        }]),
        ..delayed
    };
    assert_eq!(
        backend::assign_with_status(&ctx, &principal, &with_hooks, AssignOrigin::default())
            .await
            .unwrap_err()
            .to_string(),
        "A delayed task (not_before in the future) cannot carry hooks"
    );

    backend::cancel(&ctx, &assigned.task.to_string(), None)
        .await
        .unwrap();
    let task = row(&ctx, assigned.task).await;
    assert_eq!(
        (
            task.latest_event_kind.as_str(),
            task.latest_instruct_kind.as_str(),
            task.is_done
        ),
        ("CANCELLED", "CANCEL", true)
    );
    assert_eq!(
        events(&ctx, assigned.task).await,
        vec![(
            "CANCELLED".into(),
            Some("Settled before it was due — never dispatched.".into())
        )]
    );
    assert_eq!(
        instructs(&ctx, assigned.task).await,
        vec![("CANCEL".into(), None)]
    );
    assert!(queued(&ctx, executor).await.is_empty(), "nothing is sent");
    assert!(matches!(
        backend::cancel(&ctx, &assigned.task.to_string(), None).await,
        Err(BackendError::Refused(_))
    ));
}

#[tokio::test]
async fn a_guard_sees_changes_but_not_its_own() {
    let Some(ctx) = context().await else { return };
    let workflow_agent = agent(&ctx, None).await;
    let owner = agent(&ctx, None).await;
    let (_, workflow) = action(&ctx, workflow_agent, false).await;
    let (_, dispense) = action(&ctx, owner, false).await;
    let session = format!("s-{}", uuid::Uuid::new_v4());
    sqlx::query("UPDATE facade_agent SET active_session_id = $2 WHERE id = $1")
        .bind(owner)
        .bind(&session)
        .execute(&ctx.db)
        .await
        .unwrap();
    sqlx::query(
        "WITH d AS (INSERT INTO facade_statedefinition (name, hash, ports, description, organization_id)
                    SELECT 'plate', $2, '[]', '', organization_id FROM facade_agent WHERE id = $1 RETURNING id)
         INSERT INTO facade_state (interface, key, value, created_at, updated_at, retention_policy, agent_id, definition_id)
         SELECT 'plate', 'plate', '{}', now(), now(), 'KEEP', $1, d.id FROM d",
    )
    .bind(owner)
    .bind(uuid::Uuid::new_v4().to_string())
    .execute(&ctx.db)
    .await
    .unwrap();

    let parent = backend::assign_with_status(
        &ctx,
        &principal(&ctx, workflow_agent).await,
        &echo(workflow, 1, None),
        AssignOrigin::default(),
    )
    .await
    .unwrap()
    .task;
    sqlx::query("UPDATE facade_task SET dependencies = $2 WHERE id = $1")
        .bind(parent)
        .bind(json!({"robot": [{"agent": owner.to_string(), "actions": {}}]}))
        .execute(&ctx.db)
        .await
        .unwrap();
    let Some(ToAgent::AssignResponse { task: Some(own_call), .. }) = route(
        &ctx,
        workflow_agent,
        &frame(json!({"type": "ASSIGN_REQUEST", "parent": parent.to_string(), "implementation": dispense.to_string(), "args": {"x": 1}})),
        None,
    )
    .await
    .unwrap() else {
        panic!()
    };

    let guard = |since: Option<Value>, paths: Vec<&str>| {
        frame(
            json!({"type": "STATE_REVISION_REQUEST", "parent": parent.to_string(), "dependency": "robot",
                     "state": "plate", "since": since, "paths": paths}),
        )
    };
    let patch = |rev: u64, path: &str, task: Option<&str>| {
        frame(
            json!({"type": "STATE_PATCH", "session_id": session, "global_rev": rev, "state_name": "plate",
                     "ts": 1.0, "op": "replace", "path": path, "value": 1, "old_value": null, "task_id": task}),
        )
    };
    route(&ctx, owner, &patch(1, "/wells/a1", None), None)
        .await
        .unwrap();

    let Some(ToAgent::StateRevisionResponse {
        revision: Some(revision),
        changed: None,
        error: None,
        ..
    }) = route(&ctx, workflow_agent, &guard(None, vec![]), None)
        .await
        .unwrap()
    else {
        panic!()
    };
    assert_eq!(revision, json!({"session": session, "global_rev": 1}));

    // The workflow's own call changes the plate: not news.
    route(&ctx, owner, &patch(2, "/wells/a2", Some(&own_call)), None)
        .await
        .unwrap();
    let reply = route(
        &ctx,
        workflow_agent,
        &guard(Some(revision.clone()), vec![]),
        None,
    )
    .await
    .unwrap();
    assert!(
        matches!(
            &reply,
            Some(ToAgent::StateRevisionResponse {
                changed: Some(false),
                ..
            })
        ),
        "{reply:?}"
    );

    // Someone else's change, outside the watched paths, then inside.
    route(&ctx, owner, &patch(3, "/lid", None), None)
        .await
        .unwrap();
    let reply = route(
        &ctx,
        workflow_agent,
        &guard(Some(revision.clone()), vec!["wells"]),
        None,
    )
    .await
    .unwrap();
    assert!(
        matches!(
            &reply,
            Some(ToAgent::StateRevisionResponse {
                changed: Some(false),
                ..
            })
        ),
        "{reply:?}"
    );
    let reply = route(
        &ctx,
        workflow_agent,
        &guard(Some(revision.clone()), vec!["lid"]),
        None,
    )
    .await
    .unwrap();
    let Some(ToAgent::StateRevisionResponse {
        changed: Some(true),
        detail: Some(detail),
        ..
    }) = reply
    else {
        panic!("{reply:?}")
    };
    assert_eq!(
        detail,
        "'plate' changed at /lid (by the agent itself) since the workflow last saw it."
    );

    // Another session: set up again.
    let stale = json!({"session": "an-earlier-one", "global_rev": 1});
    let reply = route(&ctx, workflow_agent, &guard(Some(stale), vec![]), None)
        .await
        .unwrap();
    assert!(
        matches!(&reply, Some(ToAgent::StateRevisionResponse { detail: Some(d), .. }) if d == "'plate' was set up again: its agent restarted since."),
        "{reply:?}"
    );

    // Only the workflow's own agent asks.
    let reply = route(&ctx, owner, &guard(None, vec![]), None)
        .await
        .unwrap();
    assert!(
        matches!(&reply, Some(ToAgent::StateRevisionResponse { error: Some(e), .. }) if e == "A guard is asked by the agent running the workflow."),
        "{reply:?}"
    );
}

#[tokio::test]
async fn a_probe_lives_in_redis_and_mirrors_to_its_requester() {
    let Some(ctx) = context().await else { return };
    let organization = format!("o-{}", uuid::Uuid::new_v4().simple());
    let requester = agent(&ctx, Some(&organization)).await;
    let executor = agent(&ctx, Some(&organization)).await;
    let (action, _) = action(&ctx, executor, true).await;
    let caller = facade::persist::caller_ops::get_or_create_caller_id(&ctx.db, requester)
        .await
        .unwrap();
    let (channel, mut inbox) = ctx.channel_layer.subscribe("specific").await.unwrap();
    ctx.channel_layer
        .group_add(&format!("task_caller_{caller}"), &channel)
        .await
        .unwrap();

    let reply = route(
        &ctx,
        requester,
        &frame(json!({"type": "PROBE_REQUEST", "action": action.to_string(), "args": {"x": 2}})),
        None,
    )
    .await
    .unwrap();
    let Some(ToAgent::ProbeResponse {
        probe: Some(probe),
        error: None,
        ..
    }) = reply
    else {
        panic!("{reply:?}")
    };
    assert!(probe.starts_with("p-"));
    let assign = queued(&ctx, executor).await;
    let assign = assign
        .iter()
        .find(|f| f["task"] == probe)
        .expect("the probe's ASSIGN");
    assert_eq!(
        (assign["probe"].clone(), assign["args"].clone()),
        (json!(true), json!({"x": 2}))
    );
    let claims = ctx
        .settings
        .instance_key
        .as_ref()
        .unwrap()
        .verify_jwt(assign["token"].as_str().unwrap())
        .unwrap()
        .1;
    assert_eq!(
        (
            claims["tsk"].clone(),
            claims["rtk"].clone(),
            claims["ptk"].clone()
        ),
        (json!(probe), json!(probe), Value::Null)
    );

    let report = |kind: &str| frame(json!({"type": kind, "task": probe}));
    assert!(matches!(
        route(&ctx, executor, &report("STARTED"), None)
            .await
            .unwrap(),
        Some(ToAgent::EventAck { .. })
    ));
    assert!(matches!(
        route(&ctx, executor, &report("COMPLETED"), None)
            .await
            .unwrap(),
        Some(ToAgent::EventAck { .. })
    ));
    // The resent terminal is acked again, and not published again.
    assert!(matches!(
        route(&ctx, executor, &report("COMPLETED"), None)
            .await
            .unwrap(),
        Some(ToAgent::EventAck { .. })
    ));
    // An agent cannot control a probe.
    let reply = route(
        &ctx,
        requester,
        &frame(json!({"type": "CANCEL_REQUEST", "task": probe})),
        None,
    )
    .await
    .unwrap();
    assert!(
        matches!(&reply, Some(ToAgent::ControlResponse { accepted: false, error: Some(e), .. }) if e.starts_with("Probes are controlled by their caller")),
        "{reply:?}"
    );

    let mut seen = vec![];
    while seen.len() < 2 {
        let message = tokio::time::timeout(std::time::Duration::from_secs(5), inbox.recv())
            .await
            .expect("the probe's events reach the requester's caller group")
            .unwrap();
        let payload = kante::channel::payload_of(&message, "probe_event_broadcast").unwrap();
        seen.push((
            payload["kind"].as_str().unwrap().to_owned(),
            payload["seq"].as_i64().unwrap(),
        ));
    }
    assert_eq!(seen, vec![("STARTED".into(), 1), ("COMPLETED".into(), 2)]);
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(300), inbox.recv())
            .await
            .is_err(),
        "one COMPLETED"
    );

    let mut redis = ctx.redis.clone();
    let state: std::collections::HashMap<String, String> = redis
        .hgetall(format!("rekuest:probe:{probe}"))
        .await
        .unwrap();
    assert_eq!(state.get("done").map(String::as_str), Some("COMPLETED"));
    let inflight: i64 = redis
        .get(format!("rekuest:probe-inflight:{caller}"))
        .await
        .unwrap();
    assert_eq!(inflight, 0, "the slot is given back");

    // A second probe dies with its agent.
    let Some(ToAgent::ProbeResponse {
        probe: Some(second),
        ..
    }) = route(
        &ctx,
        requester,
        &frame(json!({"type": "PROBE_REQUEST", "action": action.to_string(), "args": {"x": 3}})),
        None,
    )
    .await
    .unwrap()
    else {
        panic!()
    };
    assert_eq!(
        facade::probes::persist::fail_all_for_agent(&ctx, executor)
            .await
            .unwrap(),
        1
    );
    let state: std::collections::HashMap<String, String> = redis
        .hgetall(format!("rekuest:probe:{second}"))
        .await
        .unwrap();
    assert_eq!(
        (
            state.get("done").map(String::as_str),
            state.get("err").map(String::as_str)
        ),
        (Some("CRITICAL"), Some("Agent disconnected"))
    );
    ctx.channel_layer.unsubscribe(&channel);
}
