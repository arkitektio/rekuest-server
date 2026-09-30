//! The sweeps, against a database the Python server migrated: the reclaim grace, the stale-agent
//! revoke, the pickup watchdog, workflow resume, control escalation, delayed tasks, expiry and
//! the reaper pass (the behaviour of `tests/agent/test_reclaim_grace.py`, `test_liveness.py`,
//! `test_pickup_watchdog.py`, `test_workflow_resume.py`, `test_auto_interrupt.py` and
//! `test_delayed_tasks.py`). Needs `AGENTD_TEST_DATABASE_URL` and `AGENTD_TEST_REDIS_URL`
//! (`eval "$(scripts/test-db.sh)"`).
//!
//! Every sweep scans the whole database, so the tests take turns ([`serial`]) and assert on
//! their own rows, never on a sweep's count unless nothing else can be due. Deadlines are
//! passed by backdating rows, not by sleeping.

use std::sync::Arc;
use std::time::Duration;

use authentikate::base_models::StaticToken;
use facade::consumers::agent_queue;
use facade::consumers::connections::Connections;
use facade::persist::{leases, reconcile};
use facade::settings::Settings;
use facade::{clock, reaper, Context};
use serde_json::{json, Value};

/// Large enough that rows other tests (or runs) left behind cannot crowd ours out.
const LIMIT: i64 = 100_000;

static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

async fn serial() -> tokio::sync::MutexGuard<'static, ()> {
    SERIAL.lock().await
}

/// Deadlines short enough to pass by backdating rows ten seconds, and long enough that a row
/// made during the test never passes one on its own.
fn settings() -> Settings {
    Settings {
        grace: Duration::ZERO,
        pickup_deadline: Duration::from_secs(5),
        disconnected_expiry: Duration::from_secs(5),
        control_deadline: Duration::from_secs(60),
        ..Settings::default()
    }
}

async fn context(settings: Settings) -> Option<Context> {
    let db_url = std::env::var("AGENTD_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("AGENTD_TEST_REDIS_URL").ok()?;
    let db = sqlx::PgPool::connect(&db_url)
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
        settings: Arc::new(settings),
        verifier: Arc::new(authentikate::Verifier::new(auth)),
        connections: Connections::default(),
        channel_layer,
    })
}

/// A fresh agent (through the real token expansion and `ensure_agent`) and an action.
async fn agent(ctx: &Context) -> (i64, i64) {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "sweeps-tests", "org": format!("o-{unique}"),
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
        "INSERT INTO facade_action (defined_at, embedding_model, key, version, pure, idempotent, allow_probe, stateful,
                                    kind, port_groups, name, description, scope, is_dev, hash, args, returns,
                                    arg_count, return_count, app_id, organization_id)
         SELECT now(), '', $2, '1', false, false, false, false, 'FUNCTION', '[]', 'act', '', 'GLOBAL', false, $2,
                '[]', '[]', 0, 0, a.app_id, a.organization_id FROM facade_agent a WHERE a.id = $1
         RETURNING id",
    )
    .bind(agent)
    .bind(format!("act-{unique}"))
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    // Agent ids come back after the test database is reset: so could a queue left behind.
    let mut redis = ctx.redis.clone();
    let _: () = redis::cmd("DEL")
        .arg(agent_queue::queue_key(&ctx.settings, agent))
        .query_async(&mut redis)
        .await
        .unwrap();
    (agent, action)
}

/// The agent's implementation of the action, run as `execution`, at `code_hash`.
async fn implementation(ctx: &Context, agent: i64, action: i64, execution: &str) -> i64 {
    sqlx::query_scalar(
        "INSERT INTO facade_implementation (interface, name, policy, higher_order_config, params, created_at,
                                            updated_at, tracks, diagnostics, needs_token, effects, execution,
                                            code_hash, action_id, agent_id, release_id)
         SELECT 'do_it', 'do_it', '{}', '{}', '{}', now(), now(), '[]', '[]', false, 'UNKNOWN', $3, 'h1',
                $2, a.id, a.release_id FROM facade_agent a WHERE a.id = $1
         RETURNING id",
    )
    .bind(agent)
    .bind(action)
    .bind(execution)
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

/// An open task of `agent`, QUEUED and dispatched once, as `assign` leaves it. No caller and no
/// implementation: its Assign cannot be rebuilt.
async fn task(ctx: &Context, agent: i64, action: i64) -> i64 {
    sqlx::query_scalar(
        "INSERT INTO facade_task (acted_on, ephemeral, hooks, reference, resumes, capture, is_higher_order_child,
                                  latest_event_kind, latest_instruct_kind, statusmessage, is_done, created_at,
                                  updated_at, revision, step, dispatch_attempts, trigger_depth, action_id, agent_id,
                                  dispatched_at, args)
         VALUES ('{}', false, '[]', $3, 0, false, false, 'QUEUED', 'ASSIGN', '', false, now(), now(), 0, false, 1, 0,
                 $2, $1, now(), '{\"x\": 1}')
         RETURNING id",
    )
    .bind(agent)
    .bind(action)
    .bind(uuid::Uuid::new_v4().to_string())
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

/// A task whose Assign can be rebuilt: the agent's own caller, through `implementation`.
async fn assignable(ctx: &Context, agent: i64, action: i64, implementation: i64) -> i64 {
    let task = task(ctx, agent, action).await;
    let caller = facade::persist::caller_ops::get_or_create_caller_id(&ctx.db, agent)
        .await
        .unwrap();
    sqlx::query(
        "UPDATE facade_task SET caller_id = $2, implementation_id = $3, code_hash = 'h1' WHERE id = $1",
    )
    .bind(task)
    .bind(caller)
    .bind(implementation)
    .execute(&ctx.db)
    .await
    .unwrap();
    task
}

/// The agent picked the task up and started it.
async fn start(ctx: &Context, task: i64) {
    sqlx::query(
        "UPDATE facade_task SET picked_up_at = now(), latest_event_kind = 'STARTED' WHERE id = $1",
    )
    .bind(task)
    .execute(&ctx.db)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO facade_taskevent (task_id, kind, created_at, step) VALUES ($1, 'STARTED', now(), 1)",
    )
    .bind(task)
    .execute(&ctx.db)
    .await
    .unwrap();
}

/// Set the agent's liveness: `connected`, last seen `seconds_ago`.
async fn seen(ctx: &Context, agent: i64, connected: bool, seconds_ago: i64) {
    sqlx::query(
        "UPDATE facade_agent SET connected = $2, last_seen = now() - make_interval(secs => $3) WHERE id = $1",
    )
    .bind(agent)
    .bind(connected)
    .bind(seconds_ago as f64)
    .execute(&ctx.db)
    .await
    .unwrap();
}

/// Move the task's pickup clock `seconds` into the past.
async fn backdate(ctx: &Context, task: i64, seconds: i64) {
    sqlx::query(
        "UPDATE facade_task SET dispatched_at = dispatched_at - make_interval(secs => $2),
                                created_at = created_at - make_interval(secs => $2) WHERE id = $1",
    )
    .bind(task)
    .bind(seconds as f64)
    .execute(&ctx.db)
    .await
    .unwrap();
}

async fn kinds(ctx: &Context, task: i64) -> Vec<String> {
    sqlx::query_scalar("SELECT kind FROM facade_taskevent WHERE task_id = $1 ORDER BY id")
        .bind(task)
        .fetch_all(&ctx.db)
        .await
        .unwrap()
}

/// The last event of `kind`: its message and value.
async fn event(ctx: &Context, task: i64, kind: &str) -> (Option<String>, Option<Value>) {
    let (message, value): (Option<String>, Option<sqlx::types::Json<Value>>) = sqlx::query_as(
        "SELECT message, value FROM facade_taskevent WHERE task_id = $1 AND kind = $2 ORDER BY id DESC LIMIT 1",
    )
    .bind(task)
    .bind(kind)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    (message, value.map(|v| v.0))
}

async fn state(ctx: &Context, task: i64) -> (String, bool) {
    sqlx::query_as("SELECT latest_event_kind, is_done FROM facade_task WHERE id = $1")
        .bind(task)
        .fetch_one(&ctx.db)
        .await
        .unwrap()
}

/// The frames waiting in the agent's queue, oldest first.
async fn queued(ctx: &Context, agent: i64) -> Vec<Value> {
    let mut redis = ctx.redis.clone();
    let frames: Vec<String> = redis::cmd("LRANGE")
        .arg(agent_queue::queue_key(&ctx.settings, agent))
        .arg(0)
        .arg(-1)
        .query_async(&mut redis)
        .await
        .unwrap();
    frames
        .iter()
        .rev()
        .map(|frame| serde_json::from_str(frame).unwrap())
        .collect()
}

// -- the reclaim grace (test_reclaim_grace.py) ------------------------------------------------

#[tokio::test]
async fn a_fresh_process_leaves_the_old_work_lost_and_the_undelivered_queued() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    let running = task(&ctx, agent, action).await;
    start(&ctx, running).await;
    let waiting = task(&ctx, agent, action).await;

    leases::on_agent_connected(&ctx.db, &ctx.settings, agent, "c1", Some("S1"), false)
        .await
        .unwrap();
    leases::release_lease(&ctx.db, agent, "c1").await.unwrap();
    let claim = leases::on_agent_connected(&ctx.db, &ctx.settings, agent, "c2", Some("S2"), false)
        .await
        .unwrap();
    assert_eq!(
        claim.orphaned,
        vec![running],
        "only what it had picked up is orphaned"
    );
    assert!(
        claim.inquiries.is_empty(),
        "a new process is not asked about the old one's work"
    );
    reconcile::fail_and_cascade_inflight(&ctx, &claim.orphaned)
        .await
        .unwrap();

    assert_eq!(state(&ctx, running).await, ("LOST".into(), true));
    let (message, value) = event(&ctx, running, "LOST").await;
    assert_eq!(
        message.as_deref(),
        Some("Its agent died while it ran; how it ended is unknown.")
    );
    assert_eq!(value.unwrap()["started"], json!(true));
    assert_eq!(kinds(&ctx, waiting).await, Vec::<String>::new());
    assert_eq!(state(&ctx, waiting).await, ("QUEUED".into(), false));
}

#[tokio::test]
async fn a_same_session_reconnect_is_asked_about_its_work() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    let running = task(&ctx, agent, action).await;
    start(&ctx, running).await;
    let waiting = task(&ctx, agent, action).await;

    leases::on_agent_connected(&ctx.db, &ctx.settings, agent, "c1", Some("S1"), false)
        .await
        .unwrap();
    leases::release_lease(&ctx.db, agent, "c1").await.unwrap();
    let claim = leases::on_agent_connected(&ctx.db, &ctx.settings, agent, "c2", Some("S1"), false)
        .await
        .unwrap();

    assert_eq!(claim.inquiries, vec![running]);
    assert!(
        !claim.inquiries.contains(&waiting),
        "an Assign still in redis is not in flight"
    );
    assert!(claim.orphaned.is_empty());
    assert_eq!(state(&ctx, running).await, ("STARTED".into(), false));
}

#[tokio::test]
async fn the_grace_window_ends_the_work_lost_once() {
    let _serial = serial().await;
    let Some(ctx) = context(Settings {
        grace: Duration::from_secs(30),
        ..settings()
    })
    .await
    else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    let running = task(&ctx, agent, action).await;
    start(&ctx, running).await;

    // Disconnected ten seconds ago: still inside the grace, it may come back.
    seen(&ctx, agent, false, 10).await;
    reconcile::reconcile_disconnected_agents(&ctx)
        .await
        .unwrap();
    assert_eq!(state(&ctx, running).await, ("STARTED".into(), false));

    // Five minutes: gone. Work without a caller ends LOST too, and sweeping again changes nothing.
    seen(&ctx, agent, false, 300).await;
    reconcile::reconcile_disconnected_agents(&ctx)
        .await
        .unwrap();
    reconcile::reconcile_disconnected_agents(&ctx)
        .await
        .unwrap();
    assert_eq!(state(&ctx, running).await, ("LOST".into(), true));
    let lost = kinds(&ctx, running)
        .await
        .iter()
        .filter(|k| *k == "LOST")
        .count();
    assert_eq!(lost, 1);
}

#[tokio::test]
async fn a_live_agent_keeps_its_work() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    let running = task(&ctx, agent, action).await;
    start(&ctx, running).await;
    seen(&ctx, agent, true, 1).await;

    reconcile::reconcile_orphaned_executor_work(&ctx, agent)
        .await
        .unwrap();
    assert_eq!(state(&ctx, running).await, ("STARTED".into(), false));

    seen(&ctx, agent, false, 3600).await;
    reconcile::reconcile_orphaned_executor_work(&ctx, agent)
        .await
        .unwrap();
    assert_eq!(state(&ctx, running).await, ("LOST".into(), true));
}

// -- the stale-agent revoke (test_liveness.py, the reaper pass) --------------------------------

#[tokio::test]
async fn a_stuck_connected_agent_is_revoked_and_its_work_lost() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (stuck, action) = agent(&ctx).await;
    let running = task(&ctx, stuck, action).await;
    start(&ctx, running).await;
    seen(&ctx, stuck, true, 3600).await;
    let (live, live_action) = self::agent(&ctx).await;
    let live_task = task(&ctx, live, live_action).await;
    start(&ctx, live_task).await;
    seen(&ctx, live, true, 1).await;
    let epoch = |agent| {
        let db = ctx.db.clone();
        async move {
            sqlx::query_as::<_, (bool, i64)>(
                "SELECT connected, lease_epoch FROM facade_agent WHERE id = $1",
            )
            .bind(agent)
            .fetch_one(&db)
            .await
            .unwrap()
        }
    };
    let (_, before) = epoch(stuck).await;

    reconcile::reconcile_stale_agents(&ctx).await.unwrap();

    assert_eq!(
        epoch(stuck).await,
        (false, before + 1),
        "revoked and fenced"
    );
    assert_eq!(state(&ctx, running).await, ("LOST".into(), true));
    assert!(epoch(live).await.0, "a live agent is not touched");
    assert_eq!(state(&ctx, live_task).await, ("STARTED".into(), false));

    // Another backend's sweep finds nothing stale any more.
    assert!(!leases::revoke_lease(&ctx, stuck).await.unwrap());
    reconcile::reconcile_stale_agents(&ctx).await.unwrap();
    assert_eq!(epoch(stuck).await, (false, before + 1));
}

// -- the pickup watchdog (test_pickup_watchdog.py) ---------------------------------------------

#[tokio::test]
async fn a_silent_live_agent_gets_one_redelivery_then_the_task_is_lost_unstarted() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    seen(&ctx, agent, true, 1).await;
    let implementation = implementation(&ctx, agent, action, "PLAIN").await;
    let task = assignable(&ctx, agent, action, implementation).await;
    backdate(&ctx, task, 10).await;

    reconcile::reconcile_unpicked_tasks(&ctx, LIMIT)
        .await
        .unwrap();

    let (attempts, dispatched): (i16, bool) = sqlx::query_as(
        "SELECT dispatch_attempts, dispatched_at > now() - interval '5 seconds' FROM facade_task WHERE id = $1",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!((attempts, dispatched), (2, true));
    assert_eq!(
        event(&ctx, task, "QUEUED").await.0.as_deref(),
        Some("No report from the agent within the pickup deadline — Assign redelivered.")
    );
    let frames = queued(&ctx, agent).await;
    let assign = frames.last().expect("the Assign was redelivered");
    assert_eq!(assign["type"], "ASSIGN");
    assert_eq!(assign["task"], task.to_string());
    assert_eq!(assign["implementation"], implementation.to_string());
    assert_eq!(assign["interface"], "do_it");
    assert_eq!(assign["args"], json!({"x": 1}));
    assert_eq!(assign["step"], Value::Null, "a false step is sent as none");
    assert!(
        assign.get("resume").is_none(),
        "a plain task has no journal"
    );
    assert!(
        assign.get("token").is_none(),
        "no token until provenance is wired"
    );

    // Silent again: the budget is spent.
    backdate(&ctx, task, 10).await;
    reconcile::reconcile_unpicked_tasks(&ctx, LIMIT)
        .await
        .unwrap();
    assert_eq!(state(&ctx, task).await, ("LOST".into(), true));
    let (message, value) = event(&ctx, task, "LOST").await;
    assert_eq!(
        message.as_deref(),
        Some("Never picked up: the agent did not report on this task after it was redelivered.")
    );
    assert_eq!(value.unwrap()["started"], json!(false));
}

#[tokio::test]
async fn the_watchdog_leaves_picked_up_offline_and_undue_work_alone() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    seen(&ctx, agent, true, 1).await;
    let reported = task(&ctx, agent, action).await;
    sqlx::query("UPDATE facade_task SET picked_up_at = now() WHERE id = $1")
        .bind(reported)
        .execute(&ctx.db)
        .await
        .unwrap();
    backdate(&ctx, reported, 10).await;
    let fresh = task(&ctx, agent, action).await;
    let (offline, offline_action) = self::agent(&ctx).await;
    seen(&ctx, offline, false, 1).await;
    let stranded = task(&ctx, offline, offline_action).await;
    backdate(&ctx, stranded, 10).await;

    reconcile::reconcile_unpicked_tasks(&ctx, LIMIT)
        .await
        .unwrap();

    for task in [reported, fresh, stranded] {
        assert_eq!(kinds(&ctx, task).await, Vec::<String>::new(), "task {task}");
    }

    // Disabled, nothing at all is looked at.
    let disabled = context(Settings {
        pickup_deadline: Duration::ZERO,
        ..settings()
    })
    .await
    .unwrap();
    let silent = task(&disabled, agent, action).await;
    backdate(&disabled, silent, 10).await;
    assert_eq!(
        reconcile::reconcile_unpicked_tasks(&disabled, LIMIT)
            .await
            .unwrap(),
        0
    );
    assert_eq!(state(&ctx, silent).await, ("QUEUED".into(), false));
}

#[tokio::test]
async fn a_control_on_unpicked_work_is_honoured_and_a_task_without_a_caller_ends_lost() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    seen(&ctx, agent, true, 1).await;
    let cancelled = task(&ctx, agent, action).await;
    let interrupted = task(&ctx, agent, action).await;
    let orphan = task(&ctx, agent, action).await;
    for (task, instruct) in [(cancelled, "CANCEL"), (interrupted, "INTERRUPT")] {
        sqlx::query("UPDATE facade_task SET latest_instruct_kind = $2 WHERE id = $1")
            .bind(task)
            .bind(instruct)
            .execute(&ctx.db)
            .await
            .unwrap();
    }
    for task in [cancelled, interrupted, orphan] {
        backdate(&ctx, task, 10).await;
    }

    reconcile::reconcile_unpicked_tasks(&ctx, LIMIT)
        .await
        .unwrap();

    assert_eq!(state(&ctx, cancelled).await, ("CANCELLED".into(), true));
    assert_eq!(
        event(&ctx, cancelled, "CANCELLED").await.0.as_deref(),
        Some("Cancelled before any agent picked the task up.")
    );
    assert_eq!(state(&ctx, interrupted).await, ("INTERRUPTED".into(), true));
    assert_eq!(state(&ctx, orphan).await, ("LOST".into(), true));
    assert_eq!(
        event(&ctx, orphan, "LOST").await.0.as_deref(),
        Some("Never picked up, and the Assign could not be rebuilt for redelivery.")
    );
    assert!(queued(&ctx, agent).await.is_empty(), "nothing was sent");
}

#[tokio::test]
async fn concurrent_watchdogs_redeliver_once() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    seen(&ctx, agent, true, 1).await;
    let implementation = implementation(&ctx, agent, action, "PLAIN").await;
    let task = assignable(&ctx, agent, action, implementation).await;
    backdate(&ctx, task, 10).await;

    let sweeps = (0..4).map(|_| {
        let ctx = ctx.clone();
        tokio::spawn(async move { reconcile::reconcile_unpicked_tasks(&ctx, LIMIT).await })
    });
    for sweep in sweeps {
        sweep.await.unwrap().unwrap();
    }

    assert_eq!(kinds(&ctx, task).await, vec!["QUEUED".to_owned()]);
    assert_eq!(queued(&ctx, agent).await.len(), 1);
}

#[tokio::test]
async fn an_undeliverable_webhook_task_is_retried_once_then_lost() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    sqlx::query("UPDATE facade_agent SET kind = 'WEBHOOK' WHERE id = $1")
        .bind(agent)
        .execute(&ctx.db)
        .await
        .unwrap();
    let implementation = implementation(&ctx, agent, action, "PLAIN").await;
    let task = assignable(&ctx, agent, action, implementation).await;
    backdate(&ctx, task, 10).await;

    reconcile::reconcile_unpicked_tasks(&ctx, LIMIT)
        .await
        .unwrap();
    let (attempts, dispatched_at, done): (i16, Option<chrono::DateTime<chrono::Utc>>, bool) =
        sqlx::query_as(
            "SELECT dispatch_attempts, dispatched_at, is_done FROM facade_task WHERE id = $1",
        )
        .bind(task)
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    assert_eq!(
        (attempts, dispatched_at, done),
        (2, None, false),
        "the failed handoff never left"
    );

    sqlx::query(
        "UPDATE facade_task SET created_at = created_at - interval '10 seconds' WHERE id = $1",
    )
    .bind(task)
    .execute(&ctx.db)
    .await
    .unwrap();
    reconcile::reconcile_unpicked_tasks(&ctx, LIMIT)
        .await
        .unwrap();
    assert_eq!(state(&ctx, task).await, ("LOST".into(), true));
}

// -- expiry (test_pickup_watchdog.py TestExpiry) -----------------------------------------------

#[tokio::test]
async fn undelivered_work_of_a_gone_agent_expires_lost_unstarted() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    seen(&ctx, agent, false, 3600).await;
    let expired = task(&ctx, agent, action).await;
    backdate(&ctx, expired, 10).await;
    let recent = task(&ctx, agent, action).await;

    reconcile::expire_disconnected_tasks(&ctx, LIMIT)
        .await
        .unwrap();

    assert_eq!(state(&ctx, expired).await, ("LOST".into(), true));
    let (message, value) = event(&ctx, expired, "LOST").await;
    assert_eq!(
        message.as_deref(),
        Some("The agent never came back to pick this task up.")
    );
    assert_eq!(value.unwrap()["started"], json!(false));
    assert_eq!(state(&ctx, recent).await, ("QUEUED".into(), false));
}

// -- workflow resume (test_workflow_resume.py) -------------------------------------------------

/// A running workflow: it started, recorded the clock at step 2, and made a call at step 3.
async fn workflow(ctx: &Context, code_hash: &str) -> (i64, i64) {
    let (agent, action) = agent(ctx).await;
    let implementation = implementation(ctx, agent, action, "WORKFLOW").await;
    let task = assignable(ctx, agent, action, implementation).await;
    sqlx::query("UPDATE facade_task SET code_hash = $2 WHERE id = $1")
        .bind(task)
        .bind(code_hash)
        .execute(&ctx.db)
        .await
        .unwrap();
    start(ctx, task).await;
    sqlx::query(
        "INSERT INTO facade_taskevent (task_id, kind, created_at, step, effect, key, value)
         VALUES ($1, 'EFFECT', now(), 2, 'NOW', 'NOW:1', '1790000000.5')",
    )
    .bind(task)
    .execute(&ctx.db)
    .await
    .unwrap();
    let child = self::task(ctx, agent, action).await;
    sqlx::query(
        "UPDATE facade_task SET parent_id = $2, parent_step = 3, call_key = '7:3f2b6a9c:1' WHERE id = $1",
    )
    .bind(child)
    .bind(task)
    .execute(&ctx.db)
    .await
    .unwrap();
    (agent, task)
}

/// The agent's process dies and a new one takes the agent over.
async fn take_over(ctx: &Context, agent: i64) {
    leases::on_agent_connected(&ctx.db, &ctx.settings, agent, "c1", Some("S1"), false)
        .await
        .unwrap();
    leases::release_lease(&ctx.db, agent, "c1").await.unwrap();
    let claim = leases::on_agent_connected(&ctx.db, &ctx.settings, agent, "c2", Some("S2"), false)
        .await
        .unwrap();
    reconcile::fail_and_cascade_inflight(ctx, &claim.orphaned)
        .await
        .unwrap();
}

#[tokio::test]
async fn a_workflow_whose_agent_dies_is_sent_again_with_its_journal() {
    let _serial = serial().await;
    let Some(ctx) = context(Settings {
        grace: Duration::from_secs(30),
        ..settings()
    })
    .await
    else {
        return;
    };
    let (agent, task) = workflow(&ctx, "h1").await;

    take_over(&ctx, agent).await;

    let (kind, done, picked_up, resumes, attempts): (String, bool, bool, i32, i16) = sqlx::query_as(
        "SELECT latest_event_kind, is_done, picked_up_at IS NOT NULL, resumes, dispatch_attempts
           FROM facade_task WHERE id = $1",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!(
        (kind.as_str(), done, picked_up, resumes, attempts),
        ("QUEUED", false, false, 1, 1)
    );
    assert_eq!(
        event(&ctx, task, "QUEUED").await.0.as_deref(),
        Some("Its agent died: the workflow is sent again, to resume from what it recorded.")
    );
    let frames = queued(&ctx, agent).await;
    assert_eq!(frames.len(), 1);
    assert_eq!(frames[0]["task"], task.to_string());
    assert_eq!(
        frames[0]["resume"],
        json!({"last_step": 3, "effects": [{"key": "NOW:1", "effect": "NOW", "value": 1790000000.5}]}),
        "the call's step counts too"
    );

    // ...and the new process dies too, before it picks the workflow up: it did start once.
    leases::release_lease(&ctx.db, agent, "c2").await.unwrap();
    backdate(&ctx, task, 10).await;
    reconcile::expire_disconnected_tasks(&ctx, LIMIT)
        .await
        .unwrap();
    let (_, value) = event(&ctx, task, "LOST").await;
    assert_eq!(value.unwrap()["started"], json!(true));
}

#[tokio::test]
async fn a_workflow_is_not_resumed_onto_changed_code_nor_too_often() {
    let _serial = serial().await;
    let Some(ctx) = context(Settings {
        grace: Duration::from_secs(30),
        ..settings()
    })
    .await
    else {
        return;
    };
    let (agent, changed) = workflow(&ctx, "h0").await;
    take_over(&ctx, agent).await;
    assert_eq!(state(&ctx, changed).await, ("LOST".into(), true));
    assert!(event(&ctx, changed, "LOST")
        .await
        .0
        .unwrap()
        .contains("code changed"));
    assert!(queued(&ctx, agent).await.is_empty());

    let (agent, tired) = workflow(&ctx, "h1").await;
    sqlx::query("UPDATE facade_task SET resumes = $2 WHERE id = $1")
        .bind(tired)
        .bind(reconcile::MAX_RESUMES)
        .execute(&ctx.db)
        .await
        .unwrap();
    take_over(&ctx, agent).await;
    assert_eq!(
        event(&ctx, tired, "LOST").await.0.as_deref(),
        Some("Resumed 3 times, and its agent died each time.")
    );
    assert!(queued(&ctx, agent).await.is_empty());
}

// -- control escalation (test_auto_interrupt.py) -----------------------------------------------

/// A running task with an unconfirmed `instruct` whose deadline passed `seconds_ago`.
async fn instructed(ctx: &Context, instruct: &str, seconds_ago: i64) -> (i64, i64) {
    let (agent, action) = agent(ctx).await;
    let task = task(ctx, agent, action).await;
    start(ctx, task).await;
    sqlx::query(
        "UPDATE facade_task SET latest_instruct_kind = $2,
                interrupt_at = now() - make_interval(secs => $3) WHERE id = $1",
    )
    .bind(task)
    .bind(instruct)
    .bind(seconds_ago as f64)
    .execute(&ctx.db)
    .await
    .unwrap();
    (agent, task)
}

#[tokio::test]
async fn an_unconfirmed_cancel_escalates_then_the_interrupt_is_finalized() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, task) = instructed(&ctx, "CANCEL", 1).await;
    let (_, not_due) = instructed(&ctx, "CANCEL", -60).await;

    reconcile::escalate_due_controls(&ctx, LIMIT).await.unwrap();

    assert_eq!(kinds(&ctx, task).await, vec!["STARTED", "INTERRUPTING"]);
    let (instruct, rearmed): (String, bool) = sqlx::query_as(
        "SELECT latest_instruct_kind, interrupt_at > now() FROM facade_task WHERE id = $1",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!(
        (instruct.as_str(), rearmed),
        ("INTERRUPT", true),
        "the deadline is armed again"
    );
    let instructs: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM facade_taskinstruct WHERE task_id = $1 AND kind = 'INTERRUPT'",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!(instructs, 1);
    assert_eq!(
        queued(&ctx, agent).await,
        vec![json!({"type": "INTERRUPT", "task": task.to_string()})]
    );
    assert_eq!(kinds(&ctx, not_due).await, vec!["STARTED"], "not due yet");

    // The interrupt is never confirmed either: the server stops waiting.
    sqlx::query("UPDATE facade_task SET interrupt_at = now() - interval '1 second' WHERE id = $1")
        .bind(task)
        .execute(&ctx.db)
        .await
        .unwrap();
    reconcile::escalate_due_controls(&ctx, LIMIT).await.unwrap();
    assert_eq!(state(&ctx, task).await, ("INTERRUPTED".into(), true));
    assert_eq!(
        event(&ctx, task, "INTERRUPTED").await.0.as_deref(),
        Some("Interrupt was never confirmed by the agent — finalized by the server.")
    );
    let cleared: bool =
        sqlx::query_scalar("SELECT interrupt_at IS NULL FROM facade_task WHERE id = $1")
            .bind(task)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert!(cleared);
}

#[tokio::test]
async fn concurrent_escalations_interrupt_once_and_terminal_work_never() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (_, task) = instructed(&ctx, "CANCEL", 1).await;
    let (_, done) = instructed(&ctx, "CANCEL", 1).await;
    sqlx::query(
        "UPDATE facade_task SET is_done = true, latest_event_kind = 'CANCELLED' WHERE id = $1",
    )
    .bind(done)
    .execute(&ctx.db)
    .await
    .unwrap();

    let sweeps = (0..4).map(|_| {
        let ctx = ctx.clone();
        tokio::spawn(async move { reconcile::escalate_due_controls(&ctx, LIMIT).await })
    });
    for sweep in sweeps {
        sweep.await.unwrap().unwrap();
    }

    let interrupting = kinds(&ctx, task)
        .await
        .iter()
        .filter(|k| *k == "INTERRUPTING")
        .count();
    assert_eq!(interrupting, 1);
    assert_eq!(kinds(&ctx, done).await, vec!["STARTED"]);

    reconcile::escalate_to_interrupt(&ctx, done).await.unwrap();
    assert_eq!(
        kinds(&ctx, done).await,
        vec!["STARTED"],
        "a no-op once terminal"
    );
}

// -- delayed tasks (test_delayed_tasks.py) -----------------------------------------------------

/// A delayed task, never handed over, due in `seconds` (negative: already due).
async fn delayed(
    ctx: &Context,
    agent: i64,
    action: i64,
    implementation: Option<i64>,
    seconds: i64,
) -> i64 {
    let task = match implementation {
        Some(implementation) => assignable(ctx, agent, action, implementation).await,
        None => task(ctx, agent, action).await,
    };
    sqlx::query(
        "UPDATE facade_task SET not_before = now() + make_interval(secs => $2), dispatch_attempts = 0,
                dispatched_at = NULL, created_at = now() - interval '1 hour' WHERE id = $1",
    )
    .bind(task)
    .bind(seconds as f64)
    .execute(&ctx.db)
    .await
    .unwrap();
    task
}

#[tokio::test]
async fn a_delayed_task_is_held_back_until_due_then_dispatched_once() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    seen(&ctx, agent, true, 1).await;
    let implementation = implementation(&ctx, agent, action, "PLAIN").await;
    let task = delayed(&ctx, agent, action, Some(implementation), 60).await;

    // Waiting, not undelivered: every other sweep steps over it.
    reconcile::dispatch_due_tasks(&ctx, LIMIT).await.unwrap();
    reconcile::reconcile_unpicked_tasks(&ctx, LIMIT)
        .await
        .unwrap();
    seen(&ctx, agent, false, 3600).await;
    reconcile::expire_disconnected_tasks(&ctx, LIMIT)
        .await
        .unwrap();
    assert_eq!(state(&ctx, task).await, ("QUEUED".into(), false));
    assert!(queued(&ctx, agent).await.is_empty());

    sqlx::query("UPDATE facade_task SET not_before = now() - interval '1 second' WHERE id = $1")
        .bind(task)
        .execute(&ctx.db)
        .await
        .unwrap();
    let sweeps = (0..4).map(|_| {
        let ctx = ctx.clone();
        tokio::spawn(async move { reconcile::dispatch_due_tasks(&ctx, LIMIT).await })
    });
    for sweep in sweeps {
        sweep.await.unwrap().unwrap();
    }

    let (attempts, dispatched): (i16, bool) = sqlx::query_as(
        "SELECT dispatch_attempts, dispatched_at IS NOT NULL FROM facade_task WHERE id = $1",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!((attempts, dispatched), (1, true));
    let frames = queued(&ctx, agent).await;
    assert_eq!(frames.len(), 1, "two backends racing dispatch it once");
    assert_eq!(frames[0]["task"], task.to_string());
}

#[tokio::test]
async fn a_due_task_whose_assign_cannot_be_built_is_critical() {
    let _serial = serial().await;
    let Some(ctx) = context(settings()).await else {
        return;
    };
    let (agent, action) = agent(&ctx).await;
    let task = delayed(&ctx, agent, action, None, -1).await;

    reconcile::dispatch_due_tasks(&ctx, LIMIT).await.unwrap();

    assert_eq!(state(&ctx, task).await, ("CRITICAL".into(), true));
    assert_eq!(
        event(&ctx, task, "CRITICAL").await.0.as_deref(),
        Some("Due, but the Assign could not be built (no caller identity, or the provenance policy refused).")
    );
}

// -- the reaper (test_pickup_watchdog.py TestReaperPass) ---------------------------------------

#[tokio::test]
async fn one_reaper_pass_heals_a_crashed_backends_leftovers() {
    let _serial = serial().await;
    let Some(ctx) = context(Settings {
        grace: Duration::from_millis(50),
        disconnected_expiry: Duration::from_secs(3600),
        ..settings()
    })
    .await
    else {
        return;
    };
    // One agent is stuck connected with a long-expired lease...
    let (stuck, action) = agent(&ctx).await;
    let stuck_task = task(&ctx, stuck, action).await;
    start(&ctx, stuck_task).await;
    seen(&ctx, stuck, true, 3600).await;
    // ...another disconnected cleanly, but the process holding its grace window is gone.
    let (graced, action) = self::agent(&ctx).await;
    let graced_task = task(&ctx, graced, action).await;
    start(&ctx, graced_task).await;
    seen(&ctx, graced, false, 300).await;

    assert!(reaper::run_sweeps(&ctx).await, "a correct clock sweeps");

    assert_eq!(state(&ctx, stuck_task).await, ("LOST".into(), true));
    assert_eq!(state(&ctx, graced_task).await, ("LOST".into(), true));
    let connected: bool = sqlx::query_scalar("SELECT connected FROM facade_agent WHERE id = $1")
        .bind(stuck)
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    assert!(!connected);

    reaper::run_sweeps(&ctx).await; // idempotent: a second backend's pass changes nothing
    let lost = kinds(&ctx, graced_task)
        .await
        .iter()
        .filter(|k| *k == "LOST")
        .count();
    assert_eq!(lost, 1);
}

#[tokio::test]
async fn the_tick_token_lets_one_reaper_sweep_per_tick() {
    let _serial = serial().await;
    let Some(ctx) = context(Settings {
        redis_key_prefix: format!("sweeps-test-{}", uuid::Uuid::new_v4().simple()),
        sweep_interval: Duration::from_millis(250),
        ..settings()
    })
    .await
    else {
        return;
    };
    assert!(reaper::take_tick_token(&ctx).await);
    assert!(
        !reaper::take_tick_token(&ctx).await,
        "held for most of the interval"
    );
    tokio::time::sleep(Duration::from_millis(250)).await;
    assert!(
        reaper::take_tick_token(&ctx).await,
        "and free again after it"
    );

    let skew = clock::check_skew(&ctx.db).await.expect("measurable");
    assert!(skew.abs() < clock::max_skew_seconds(&ctx.settings));
}
