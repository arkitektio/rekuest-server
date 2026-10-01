//! Schedules, triggers and retention: the runs they create, move and prune.
//!
//! The scenarios are the rekuest server's own schedule and trigger suites, from before these
//! moved to agentd, plus retention. Needs `AGENTD_TEST_DATABASE_URL` and
//! `AGENTD_TEST_REDIS_URL`; skipped without them.

use std::sync::Arc;
use std::time::Duration;

use authentikate::base_models::StaticToken;
use chrono::{DateTime, Utc};
use facade::backend::{self, BackendError, Control};
use facade::consumers::connections::Connections;
use facade::settings::Settings;
use facade::{retention, schedules, triggers, Context};
use serde_json::{json, Value};

const IDENTIFIER: &str = "@mikro/arraydataset";
const CHANNELS: &str = "@mikro/n_channels";

/// The sweeps look at every schedule and signal in the database: one test at a time.
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

async fn context(settings: Settings) -> Option<Context> {
    let db_url = std::env::var("AGENTD_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("AGENTD_TEST_REDIS_URL").ok()?;
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
            redis_key_prefix: format!("scheduling-tests-{}", uuid::Uuid::new_v4().simple()),
            ..settings
        }),
        verifier: Arc::new(authentikate::Verifier::new(auth)),
        connections: Connections::default(),
        channel_layer: kante::ChannelLayer::new(redis_client, kante::ChannelLayerConfig::default())
            .await
            .unwrap(),
    })
}

fn unique(prefix: &str) -> String {
    format!("{prefix}-{}", uuid::Uuid::new_v4().simple())
}

/// A fresh identity in `org`: (client, user, organization).
async fn identity(ctx: &Context, org: &str) -> (i64, i64, i64) {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "scheduling-tests", "org": org,
        "client_id": format!("c-{unique}"), "client_app": format!("a-{unique}"), "client_release": "1",
    }))
    .unwrap();
    let identity = authentikate::expand::expand_token_context(
        &ctx.db,
        &spec.to_token(chrono::Utc::now(), "raw"),
    )
    .await
    .unwrap();
    (identity.client, identity.user, identity.organization)
}

/// A caller of `org` with its own client and user.
async fn caller(ctx: &Context, org: &str) -> i64 {
    let (client, user, organization) = identity(ctx, org).await;
    sqlx::query_scalar(
        "INSERT INTO facade_caller (client_id, user_id, organization_id) VALUES ($1, $2, $3) RETURNING id",
    )
    .bind(client)
    .bind(user)
    .bind(organization)
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

/// What a schedule or trigger runs: a HookAgent of `org` (always a valid assign target,
/// connected or not) implementing `thumbnail`, whose `image` port takes a multi-channel dataset.
struct Target {
    agent: i64,
    action: i64,
    implementation: i64,
    organization: i64,
}

async fn target(ctx: &Context, org: &str) -> Target {
    let (client, user, organization) = identity(ctx, org).await;
    let agent = facade::registration::ensure_agent(&ctx.db, client, user, organization)
        .await
        .unwrap();
    let payload: rekuest_core::inputs::ImplementAgentInputModel = serde_json::from_value(json!({
        "hash": unique("h"),
        "implementations": [{
            "interface": "thumbnail",
            "needs_token": false,
            "definition": {"key": unique("thumbnail"), "name": "Thumbnail", "kind": "FUNCTION", "args": [
                {"key": "image", "kind": "STRUCTURE", "identifier": IDENTIFIER, "nullable": false,
                 "requires": [{"key": CHANNELS, "operator": "GTE", "value": 2}]},
                {"key": "size", "kind": "INT", "nullable": true},
            ], "returns": []},
        }],
    }))
    .unwrap();
    let mut tx = ctx.db.begin().await.unwrap();
    facade::registration::implement_agent(&mut tx, agent, &payload)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    sqlx::query("UPDATE facade_agent SET kind = 'WEBHOOK', hook_url = 'http://127.0.0.1:9/hook', hook_url_secret = 'x' WHERE id = $1")
        .bind(agent)
        .execute(&ctx.db)
        .await
        .unwrap();
    let (implementation, action): (i64, i64) =
        sqlx::query_as("SELECT id, action_id FROM facade_implementation WHERE agent_id = $1")
            .bind(agent)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    Target {
        agent,
        action,
        implementation,
        organization,
    }
}

// -- schedules ---------------------------------------------------------------------------------

/// A schedule pinned to a fresh HookAgent, every `interval` seconds: (its id, its target).
async fn schedule(ctx: &Context, interval: i32, enabled: bool) -> (i64, Target) {
    let org = unique("sch");
    let target = target(ctx, &org).await;
    let id = sqlx::query_scalar(
        "INSERT INTO facade_schedule (name, caller_id, action_id, agent_id, interface, args, interval_seconds, enabled)
         VALUES ('every so often', $1, $2, $3, 'thumbnail', $4, $5, $6) RETURNING id",
    )
    .bind(caller(ctx, &org).await)
    .bind(target.action)
    .bind(target.agent)
    .bind(json!({"image": {"__identifier": IDENTIFIER, "object": "7"}}))
    .bind(interval)
    .bind(enabled)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    (id, target)
}

#[derive(Debug, sqlx::FromRow)]
struct Run {
    id: i64,
    reference: String,
    not_before: Option<DateTime<Utc>>,
    dispatch_attempts: i16,
    latest_event_kind: String,
}

async fn open_runs(ctx: &Context, schedule: i64) -> Vec<Run> {
    sqlx::query_as(
        "SELECT id, reference, not_before, dispatch_attempts, latest_event_kind FROM facade_task
          WHERE schedule_id = $1 AND NOT is_done ORDER BY id",
    )
    .bind(schedule)
    .fetch_all(&ctx.db)
    .await
    .unwrap()
}

async fn the_open_run(ctx: &Context, schedule: i64) -> Run {
    let mut runs = open_runs(ctx, schedule).await;
    assert_eq!(runs.len(), 1, "exactly one open run: {runs:?}");
    runs.remove(0)
}

/// End a run as the agent's report would.
async fn finish(ctx: &Context, task: i64, kind: &str, message: &str) {
    sqlx::query("UPDATE facade_task SET is_done = true, latest_event_kind = $2, finished_at = now() WHERE id = $1")
        .bind(task)
        .bind(kind)
        .execute(&ctx.db)
        .await
        .unwrap();
    sqlx::query("INSERT INTO facade_taskevent (task_id, kind, message) VALUES ($1, $2, $3)")
        .bind(task)
        .bind(kind)
        .bind(message)
        .execute(&ctx.db)
        .await
        .unwrap();
}

async fn bookkeeping(ctx: &Context, schedule: i64) -> (i32, Option<String>, Option<DateTime<Utc>>) {
    sqlx::query_as(
        "SELECT consecutive_failures, last_error, refill_after FROM facade_schedule WHERE id = $1",
    )
    .bind(schedule)
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

#[tokio::test]
async fn a_schedule_always_has_exactly_one_waiting_run() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let (schedule, _) = schedule(&ctx, 60, true).await;

    assert!(schedules::refill_one(&ctx, schedule).await.unwrap());
    // It has its open run: nothing more to plan, by the schedule's own refill or the sweep.
    assert!(!schedules::refill_one(&ctx, schedule).await.unwrap());
    schedules::refill_schedules(&ctx, 100_000).await.unwrap();

    let run = the_open_run(&ctx, schedule).await;
    let slot = run
        .not_before
        .expect("a planned run is delayed to its slot");
    assert!(slot > Utc::now());
    assert_eq!(run.dispatch_attempts, 0);
    assert!(run.reference.starts_with(&format!("schedule:{schedule}:")));
    assert!(run.reference.ends_with("+00:00"), "{}", run.reference);

    // The next run follows a finished one.
    finish(&ctx, run.id, "COMPLETED", "").await;
    schedules::refill_schedules(&ctx, 100_000).await.unwrap();
    let following = the_open_run(&ctx, schedule).await;
    assert_ne!(following.id, run.id);
    assert!(following.not_before.unwrap() >= slot);
}

#[tokio::test]
async fn failures_are_counted_once_per_run_and_reset_by_success() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let (schedule, _) = schedule(&ctx, 60, true).await;
    schedules::refill_one(&ctx, schedule).await.unwrap();

    finish(
        &ctx,
        the_open_run(&ctx, schedule).await.id,
        "CRITICAL",
        "bank said no",
    )
    .await;
    schedules::refill_one(&ctx, schedule).await.unwrap();
    schedules::refill_one(&ctx, schedule).await.unwrap();
    let (failures, error, _) = bookkeeping(&ctx, schedule).await;
    assert_eq!(failures, 1, "a second refill does not count the run again");
    assert!(error.unwrap().ends_with("ended CRITICAL: bank said no"));

    finish(&ctx, the_open_run(&ctx, schedule).await.id, "COMPLETED", "").await;
    schedules::refill_one(&ctx, schedule).await.unwrap();
    assert_eq!(bookkeeping(&ctx, schedule).await, (0, None, None));
}

#[tokio::test]
async fn cancelling_the_waiting_run_skips_that_slot() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let (schedule, _) = schedule(&ctx, 60, true).await;
    schedules::refill_one(&ctx, schedule).await.unwrap();
    let skipped = the_open_run(&ctx, schedule).await;

    backend::request_control(&ctx, &skipped.id.to_string(), Control::Cancel, None)
        .await
        .unwrap();

    assert!(schedules::refill_one(&ctx, schedule).await.unwrap());
    let following = the_open_run(&ctx, schedule).await;
    assert!(
        following.not_before > skipped.not_before,
        "the reference of the skipped slot is taken"
    );
}

#[tokio::test]
async fn a_disabled_schedule_plans_nothing_and_a_broken_target_backs_off() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let (off, _) = schedule(&ctx, 60, false).await;
    assert!(!schedules::refill_one(&ctx, off).await.unwrap());
    schedules::refill_schedules(&ctx, 100_000).await.unwrap();
    assert!(open_runs(&ctx, off).await.is_empty());

    let (broken, target) = schedule(&ctx, 60, true).await;
    sqlx::query("UPDATE facade_schedule SET interface = 'gone' WHERE id = $1")
        .bind(broken)
        .execute(&ctx.db)
        .await
        .unwrap();
    assert!(!schedules::refill_one(&ctx, broken).await.unwrap());
    let (_, error, refill_after) = bookkeeping(&ctx, broken).await;
    assert!(error
        .unwrap()
        .starts_with("Could not create the next run: "));
    assert!(refill_after.unwrap() > Utc::now());
    // Backed off: not retried every tick, even once the target is fine again.
    sqlx::query("UPDATE facade_schedule SET interface = 'thumbnail' WHERE id = $1")
        .bind(broken)
        .execute(&ctx.db)
        .await
        .unwrap();
    assert!(!schedules::refill_one(&ctx, broken).await.unwrap());
    schedules::refill_schedules(&ctx, 100_000).await.unwrap();
    assert!(open_runs(&ctx, broken).await.is_empty());
    // A change to it is a fresh start: planned at once.
    assert!(schedules::plan(&ctx, broken, true, None).await.unwrap());
    assert_eq!(bookkeeping(&ctx, broken).await.2, None);
    let _ = target;
}

#[tokio::test]
async fn two_replicas_refilling_plan_one_run() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let (schedule, _) = schedule(&ctx, 60, true).await;

    let (one, other) = tokio::join!(
        schedules::refill_one(&ctx, schedule),
        schedules::refill_one(&ctx, schedule)
    );

    assert_eq!(usize::from(one.unwrap()) + usize::from(other.unwrap()), 1);
    the_open_run(&ctx, schedule).await;
}

#[tokio::test]
async fn run_now_moves_the_waiting_run_and_refuses_while_it_executes() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let (schedule, _) = schedule(&ctx, 3600, true).await;
    schedules::refill_one(&ctx, schedule).await.unwrap();
    let waiting = the_open_run(&ctx, schedule).await;
    let revision = |task: i64| {
        let db = ctx.db.clone();
        async move {
            sqlx::query_scalar::<_, i64>("SELECT revision FROM facade_task WHERE id = $1")
                .bind(task)
                .fetch_one(&db)
                .await
                .unwrap()
        }
    };
    let before = revision(waiting.id).await;

    assert_eq!(
        schedules::trigger(&ctx, schedule).await.unwrap(),
        waiting.id
    );
    let moved = the_open_run(&ctx, schedule).await;
    assert!(moved.not_before.unwrap() <= Utc::now());
    assert_eq!(
        moved.reference, waiting.reference,
        "it keeps its slot's reference"
    );
    assert_eq!(
        revision(waiting.id).await,
        before + 1,
        "the feeds order the change"
    );

    // Dispatched: now it is executing, and a second run would overlap it.
    sqlx::query("UPDATE facade_task SET dispatch_attempts = 1 WHERE id = $1")
        .bind(waiting.id)
        .execute(&ctx.db)
        .await
        .unwrap();
    assert!(matches!(
        schedules::trigger(&ctx, schedule).await,
        Err(BackendError::Refused(message)) if message == "A run of this schedule is already executing"
    ));
    // Nor is an executing run cancelled by a change to the schedule.
    schedules::cancel_waiting_run(&ctx, schedule, None)
        .await
        .unwrap();
    assert_eq!(
        the_open_run(&ctx, schedule).await.latest_event_kind,
        "QUEUED"
    );
}

#[tokio::test]
async fn run_now_without_an_open_run_creates_a_one_off() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let (schedule, _) = schedule(&ctx, 60, false).await;

    let task = schedules::trigger(&ctx, schedule).await.unwrap();

    let (owner, reference): (Option<i64>, String) =
        sqlx::query_as("SELECT schedule_id, reference FROM facade_task WHERE id = $1")
            .bind(task)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(owner, Some(schedule));
    assert!(reference.starts_with(&format!("schedule:{schedule}:manual:")));
}

// -- triggers ----------------------------------------------------------------------------------

/// A trigger running the target's action on a new dataset, owned by its own caller.
async fn trigger(ctx: &Context, org: &str, target: &Target, conditions: Value) -> i64 {
    let constraints: Vec<rekuest_core::inputs::DescriptorConstraint> =
        serde_json::from_value(conditions.clone()).unwrap();
    let compiled =
        facade::descriptors::compile_descriptors_to_jsonpath(Some(&constraints)).unwrap();
    sqlx::query_scalar(
        "INSERT INTO facade_trigger (name, caller_id, kind, identifier, conditions, compiled_jsonpath, action_id,
                                     agent_id, interface, port, args)
         VALUES ('thumbnail new images', $1, 'CREATED', $2, $3, $4, $5, $6, 'thumbnail', 'image', $7) RETURNING id",
    )
    .bind(caller(ctx, org).await)
    .bind(IDENTIFIER)
    .bind(conditions)
    .bind(compiled)
    .bind(target.action)
    .bind(target.agent)
    .bind(json!({"size": 128}))
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

/// A task in the target's organization, as the one a service was called in.
async fn causing_task(ctx: &Context, org: &str, target: &Target, depth: i16) -> i64 {
    sqlx::query_scalar(
        "INSERT INTO facade_task (reference, latest_event_kind, latest_instruct_kind, action_id, agent_id,
                                  implementation_id, caller_id, trigger_depth)
         VALUES ($1, 'STARTED', 'ASSIGN', $2, $3, $4, $5, $6) RETURNING id",
    )
    .bind(unique("cause"))
    .bind(target.action)
    .bind(target.agent)
    .bind(target.implementation)
    .bind(caller(ctx, org).await)
    .bind(depth)
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

/// A signal as the intake stores it: verified, with its cause when a provenance token held.
async fn signal(ctx: &Context, organization: i64, channels: i64, cause: Option<i64>) -> i64 {
    sqlx::query_scalar(
        "INSERT INTO facade_signal (service, signal_id, kind, identifier, object, organization_id, descriptors,
                                    causing_task_id)
         VALUES ('mikro', $1, 'CREATED', $2, '42', $3, $4, $5) RETURNING id",
    )
    .bind(unique("sig"))
    .bind(IDENTIFIER)
    .bind(organization)
    .bind(json!({CHANNELS: channels}))
    .bind(cause)
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

async fn processed(ctx: &Context, signal: i64) -> bool {
    sqlx::query_scalar("SELECT processed_at IS NOT NULL FROM facade_signal WHERE id = $1")
        .bind(signal)
        .fetch_one(&ctx.db)
        .await
        .unwrap()
}

async fn trigger_state(ctx: &Context, trigger: i64) -> (i32, Option<String>) {
    sqlx::query_as("SELECT consecutive_failures, last_error FROM facade_trigger WHERE id = $1")
        .bind(trigger)
        .fetch_one(&ctx.db)
        .await
        .unwrap()
}

#[derive(Debug, sqlx::FromRow)]
struct Fired {
    parent_id: Option<i64>,
    root_id: Option<i64>,
    signal_id: Option<i64>,
    trigger_depth: i16,
    args: sqlx::types::Json<Value>,
    reference: String,
    caller_id: Option<i64>,
}

async fn fired(ctx: &Context, trigger: i64) -> Vec<Fired> {
    sqlx::query_as(
        "SELECT parent_id, root_id, signal_id, trigger_depth, args, reference, caller_id FROM facade_task
          WHERE trigger_id = $1 ORDER BY id",
    )
    .bind(trigger)
    .fetch_all(&ctx.db)
    .await
    .unwrap()
}

#[tokio::test]
async fn a_matching_signal_runs_the_action_as_a_child_of_its_cause() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let org = unique("fire-child");
    let target = target(&ctx, &org).await;
    let trigger = trigger(&ctx, &org, &target, json!([])).await;
    let cause = causing_task(&ctx, &org, &target, 1).await;
    let signal = signal(&ctx, target.organization, 3, Some(cause)).await;

    assert_eq!(triggers::fire_one(&ctx, signal).await.unwrap(), 1);

    let runs = fired(&ctx, trigger).await;
    let [run] = runs.as_slice() else {
        panic!("one run: {runs:?}")
    };
    assert_eq!((run.parent_id, run.root_id), (Some(cause), Some(cause)));
    assert_eq!((run.signal_id, run.trigger_depth), (Some(signal), 2));
    assert_eq!(
        run.args.0,
        json!({"size": 128, "image": {"__identifier": IDENTIFIER, "object": "42"}})
    );
    assert_eq!(run.reference, format!("trigger:{trigger}:{signal}"));
    assert!(processed(&ctx, signal).await);
    // Processed: never fired twice, by its own firing or the sweep.
    assert_eq!(triggers::fire_one(&ctx, signal).await.unwrap(), 0);
    triggers::fire_triggers(&ctx, 100_000).await.unwrap();
    assert_eq!(fired(&ctx, trigger).await.len(), 1);
}

#[tokio::test]
async fn without_a_cause_the_run_is_a_root_of_the_trigger_owner() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let org = unique("fire-root");
    let target = target(&ctx, &org).await;
    let trigger = trigger(&ctx, &org, &target, json!([])).await;
    let signal = signal(&ctx, target.organization, 3, None).await;

    // Through the sweep, as the reaper fires it.
    triggers::fire_triggers(&ctx, 100_000).await.unwrap();

    let runs = fired(&ctx, trigger).await;
    let [run] = runs.as_slice() else {
        panic!("one run: {runs:?}")
    };
    let owner: i64 = sqlx::query_scalar("SELECT caller_id FROM facade_trigger WHERE id = $1")
        .bind(trigger)
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    assert_eq!(
        (run.parent_id, run.caller_id, run.trigger_depth),
        (None, Some(owner), 1)
    );
    assert!(processed(&ctx, signal).await);
}

#[tokio::test]
async fn the_ports_requires_and_the_triggers_conditions_filter() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let org = unique("fire-filter");
    let target = target(&ctx, &org).await;
    let trigger = trigger(
        &ctx,
        &org,
        &target,
        json!([{"key": CHANNELS, "operator": "LTE", "value": 3}]),
    )
    .await;
    // The port requires at least two channels; the trigger takes at most three.
    let too_few = signal(&ctx, target.organization, 1, None).await;
    let too_many = signal(&ctx, target.organization, 5, None).await;
    let fits = signal(&ctx, target.organization, 3, None).await;

    assert_eq!(triggers::fire_one(&ctx, too_few).await.unwrap(), 0);
    assert_eq!(triggers::fire_one(&ctx, too_many).await.unwrap(), 0);
    assert_eq!(triggers::fire_one(&ctx, fits).await.unwrap(), 1);

    assert!(processed(&ctx, too_few).await && processed(&ctx, too_many).await);
    assert_eq!(
        trigger_state(&ctx, trigger).await,
        (0, None),
        "filtered out, not failed"
    );
    let runs = fired(&ctx, trigger).await;
    assert_eq!(runs.len(), 1);
    assert_eq!(runs[0].signal_id, Some(fits));
}

#[tokio::test]
async fn triggers_only_see_their_own_organization() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let org = unique("fire-tenant-a");
    let target_a = target(&ctx, &org).await;
    let trigger = trigger(&ctx, &org, &target_a, json!([])).await;
    let elsewhere = target(&ctx, &unique("fire-tenant-b")).await;
    let signal = signal(&ctx, elsewhere.organization, 3, None).await;

    assert_eq!(triggers::fire_one(&ctx, signal).await.unwrap(), 0);
    assert!(fired(&ctx, trigger).await.is_empty());
}

#[tokio::test]
async fn the_loop_guard_stops_deep_chains_and_a_broken_target_is_recorded() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings {
        trigger_max_depth: 3,
        ..Settings::default()
    })
    .await
    else {
        return;
    };
    let org = unique("fire-loop");
    let target = target(&ctx, &org).await;
    let trigger = trigger(&ctx, &org, &target, json!([])).await;
    let deep = causing_task(&ctx, &org, &target, 3).await;
    let looping = signal(&ctx, target.organization, 3, Some(deep)).await;

    assert_eq!(triggers::fire_one(&ctx, looping).await.unwrap(), 0);
    let (failures, error) = trigger_state(&ctx, trigger).await;
    assert_eq!(failures, 1);
    assert!(error.unwrap().contains("4 triggers deep (limit 3)"));

    // A target that is gone: recorded, the signal still processed, never retried.
    sqlx::query("UPDATE facade_trigger SET interface = 'gone' WHERE id = $1")
        .bind(trigger)
        .execute(&ctx.db)
        .await
        .unwrap();
    let broken = signal(&ctx, target.organization, 3, None).await;
    assert_eq!(triggers::fire_one(&ctx, broken).await.unwrap(), 0);
    let (failures, error) = trigger_state(&ctx, trigger).await;
    assert_eq!(failures, 2);
    assert!(error
        .unwrap()
        .starts_with(&format!("Could not run for signal {broken}: ")));
    assert!(processed(&ctx, broken).await);

    // And a firing that works clears both.
    sqlx::query("UPDATE facade_trigger SET interface = 'thumbnail' WHERE id = $1")
        .bind(trigger)
        .execute(&ctx.db)
        .await
        .unwrap();
    let fine = signal(&ctx, target.organization, 3, None).await;
    assert_eq!(triggers::fire_one(&ctx, fine).await.unwrap(), 1);
    assert_eq!(trigger_state(&ctx, trigger).await, (0, None));
}

#[tokio::test]
async fn two_replicas_fire_each_trigger_once() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings::default()).await else {
        return;
    };
    let org = unique("fire-race");
    let target = target(&ctx, &org).await;
    let trigger = trigger(&ctx, &org, &target, json!([])).await;
    let signal = signal(&ctx, target.organization, 3, None).await;

    let (one, other) = tokio::join!(
        triggers::fire_one(&ctx, signal),
        triggers::fire_one(&ctx, signal)
    );

    assert_eq!(one.unwrap() + other.unwrap(), 1);
    assert_eq!(fired(&ctx, trigger).await.len(), 1);
}

// -- retention ---------------------------------------------------------------------------------

/// A finished root task of the target, done `age` ago, with an event and a finished child.
async fn finished_tree(
    ctx: &Context,
    org: &str,
    target: &Target,
    age: Duration,
    ephemeral: bool,
) -> i64 {
    let caller = caller(ctx, org).await;
    let insert = "INSERT INTO facade_task (reference, latest_event_kind, latest_instruct_kind, action_id, agent_id,
                                          implementation_id, caller_id, ephemeral, is_done, finished_at, parent_id, root_id)
                  VALUES ($1, 'COMPLETED', 'ASSIGN', $2, $3, $4, $5, $6, true, now() - make_interval(secs => $7), $8, $8)
                  RETURNING id";
    let root: i64 = sqlx::query_scalar(insert)
        .bind(unique("kept"))
        .bind(target.action)
        .bind(target.agent)
        .bind(target.implementation)
        .bind(caller)
        .bind(ephemeral)
        .bind(age.as_secs_f64())
        .bind(None::<i64>)
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    let _child: i64 = sqlx::query_scalar(insert)
        .bind(unique("kept-child"))
        .bind(target.action)
        .bind(target.agent)
        .bind(target.implementation)
        .bind(caller)
        .bind(ephemeral)
        .bind(age.as_secs_f64())
        .bind(Some(root))
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    sqlx::query("INSERT INTO facade_taskevent (task_id, kind) VALUES ($1, 'COMPLETED')")
        .bind(root)
        .execute(&ctx.db)
        .await
        .unwrap();
    root
}

async fn tree_size(ctx: &Context, root: i64) -> i64 {
    sqlx::query_scalar("SELECT count(*) FROM facade_task WHERE id = $1 OR root_id = $1")
        .bind(root)
        .fetch_one(&ctx.db)
        .await
        .unwrap()
}

#[tokio::test]
async fn retention_deletes_whole_trees_past_their_horizon() {
    let _serial = SERIAL.lock().await;
    let hour = Duration::from_secs(3600);
    let Some(ctx) = context(Settings {
        task_retention: 10 * hour,
        ephemeral_task_retention: hour,
        signal_retention: hour,
        ..Settings::default()
    })
    .await
    else {
        return;
    };
    let org = unique("retention");
    let target = target(&ctx, &org).await;
    let old = finished_tree(&ctx, &org, &target, 11 * hour, false).await;
    let recent = finished_tree(&ctx, &org, &target, 2 * hour, false).await;
    let housekeeping = finished_tree(&ctx, &org, &target, 2 * hour, true).await;
    // Old and done, but a descendant still runs: a cancel reaches the mother only.
    let still_running = finished_tree(&ctx, &org, &target, 11 * hour, false).await;
    sqlx::query("UPDATE facade_task SET is_done = false WHERE root_id = $1")
        .bind(still_running)
        .execute(&ctx.db)
        .await
        .unwrap();
    // A processed signal past its horizon, which the recent run names.
    let stale_signal = signal(&ctx, target.organization, 3, None).await;
    sqlx::query("UPDATE facade_signal SET processed_at = now() - interval '2 hours' WHERE id = $1")
        .bind(stale_signal)
        .execute(&ctx.db)
        .await
        .unwrap();
    sqlx::query("UPDATE facade_task SET signal_id = $2 WHERE id = $1")
        .bind(recent)
        .bind(stale_signal)
        .execute(&ctx.db)
        .await
        .unwrap();

    // The old root holds one of its agent's locks: released, not in the way.
    let lock: i64 = sqlx::query_scalar(
        "INSERT INTO facade_lock (agent_id, key, hold_by_id) VALUES ($1, 'shared', $2) RETURNING id",
    )
    .bind(target.agent)
    .bind(old)
    .fetch_one(&ctx.db)
    .await
    .unwrap();

    // Batches: other runs' leftovers may be ahead of ours.
    for _ in 0..200 {
        if retention::sweep(&ctx).await.unwrap() == 0
            && retention::sweep_signals(&ctx).await.unwrap() == 0
        {
            break;
        }
    }

    assert_eq!(
        tree_size(&ctx, old).await,
        0,
        "past the horizon: the whole tree goes"
    );
    assert_eq!(
        tree_size(&ctx, housekeeping).await,
        0,
        "ephemeral trees have the short horizon"
    );
    assert_eq!(tree_size(&ctx, recent).await, 2);
    assert_eq!(tree_size(&ctx, still_running).await, 2);
    let holder: Option<i64> =
        sqlx::query_scalar("SELECT hold_by_id FROM facade_lock WHERE id = $1")
            .bind(lock)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(holder, None);
    let events: i64 =
        sqlx::query_scalar("SELECT count(*) FROM facade_taskevent WHERE task_id = $1")
            .bind(old)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(events, 0);
    let (signals, names): (i64, Option<i64>) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM facade_signal WHERE id = $2), signal_id FROM facade_task WHERE id = $1",
    )
    .bind(recent)
    .bind(stale_signal)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!(
        (signals, names),
        (0, None),
        "the signal goes; its run stays, naming none"
    );
}

/// Task retention is the operator's opt-in; ephemeral runs have their horizon regardless.
#[tokio::test]
async fn only_ephemeral_runs_go_while_task_retention_is_off() {
    let _serial = SERIAL.lock().await;
    let Some(ctx) = context(Settings {
        signal_retention: Duration::ZERO,
        ..Settings::default()
    })
    .await
    else {
        return;
    };
    assert!(
        ctx.settings.task_retention.is_zero(),
        "the default keeps every task"
    );
    assert_eq!(
        ctx.settings.ephemeral_task_retention,
        Duration::from_secs(86400)
    );
    let org = unique("retention-off");
    let target = target(&ctx, &org).await;
    let day = Duration::from_secs(86400);
    let ancient = finished_tree(&ctx, &org, &target, 400 * day, false).await;
    let old_housekeeping = finished_tree(&ctx, &org, &target, 2 * day, true).await;
    let fresh_housekeeping =
        finished_tree(&ctx, &org, &target, Duration::from_secs(60), true).await;

    for _ in 0..200 {
        if retention::sweep(&ctx).await.unwrap() == 0 {
            break;
        }
    }

    assert_eq!(tree_size(&ctx, ancient).await, 2);
    assert_eq!(tree_size(&ctx, old_housekeeping).await, 0);
    assert_eq!(tree_size(&ctx, fresh_housekeeping).await, 2);
}
