//! Held work against a database the Python server migrated: a child an agent asks for under an
//! ASK run waits for a person, a decision releases or denies it, AUTO runs straight through,
//! and a task's own hold gets its answer back. Needs `TAKT_TEST_DATABASE_URL` and
//! `TAKT_TEST_REDIS_URL` (`eval "$(scripts/test-db.sh)"`).

use std::sync::Arc;

use authentikate::base_models::StaticToken;
use chrono::{DateTime, Utc};
use facade::backend::{self, AssignInput, AssignOrigin, BackendError, Control};
use facade::caller_context::CallerContext;
use facade::consumers::agent_protocol::child_mirrors;
use facade::consumers::connections::Connections;
use facade::holds;
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

/// One person in one organization: the apps they run (agents) and the clients they decide from
/// share their user, each with a client of its own.
struct Person {
    sub: String,
    org: String,
}

impl Person {
    fn new() -> Self {
        let unique = uuid::Uuid::new_v4().simple().to_string();
        Self {
            sub: format!("s-{unique}"),
            org: format!("o-{unique}"),
        }
    }

    /// Someone else in the same organization.
    fn colleague(&self) -> Self {
        Self {
            sub: format!("s-{}", uuid::Uuid::new_v4().simple()),
            org: self.org.clone(),
        }
    }

    async fn identity(&self, ctx: &Context) -> authentikate::expand::ExpandedTokenContext {
        let client = uuid::Uuid::new_v4().simple().to_string();
        let spec: StaticToken = serde_json::from_value(json!({
            "sub": self.sub, "iss": "hold-tests", "org": self.org,
            "client_id": format!("c-{client}"), "client_app": format!("a-{client}"), "client_release": "1",
        }))
        .unwrap();
        authentikate::expand::expand_token_context(&ctx.db, &spec.to_token(Utc::now(), "raw"))
            .await
            .unwrap()
    }

    /// A live agent of an app of theirs.
    async fn agent(&self, ctx: &Context) -> i64 {
        let identity = self.identity(ctx).await;
        let agent = facade::registration::ensure_agent(
            &ctx.db,
            identity.client,
            identity.user,
            identity.organization,
        )
        .await
        .unwrap();
        // A test agent is granted everything: the run-time gates are tested on their own.
        sqlx::query("UPDATE facade_agent SET connected = true, last_seen = now(), granted_scopes = '[\"rekuest_all\"]' WHERE id = $1")
            .bind(agent)
            .execute(&ctx.db)
            .await
            .unwrap();
        agent
    }

    /// Them in a UI: their user, a client that is no agent's.
    async fn at_a_desk(&self, ctx: &Context) -> CallerContext {
        let identity = self.identity(ctx).await;
        CallerContext::load(
            &ctx.db,
            identity.user,
            identity.client,
            Some(identity.organization),
            vec![],
        )
        .await
        .unwrap()
    }
}

/// An action of `agent` with one `INT` port `x`, and its implementation: `(action, implementation)`.
async fn action(ctx: &Context, agent: i64) -> (i64, i64) {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let ports =
        json!([{"key": "x", "kind": "INT", "nullable": false, "effects": [], "validators": []}]);
    let action: i64 = sqlx::query_scalar(
        "INSERT INTO facade_action (defined_at, key, version, pure, idempotent, allow_probe, stateful,
                                    kind, port_groups, name, description, scope, is_dev, hash, args, returns,
                                    arg_count, return_count, app_id, organization_id)
         SELECT now(), 'echo', '1', false, false, false, false, 'FUNCTION', '[]', 'Echo', '', 'GLOBAL', false, $2,
                $3, '[]', 1, 0, a.app_id, a.organization_id FROM facade_agent a WHERE a.id = $1
         RETURNING id",
    )
    .bind(agent)
    .bind(format!("hash-{unique}"))
    .bind(&ports)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    let implementation: i64 = sqlx::query_scalar(
        "INSERT INTO facade_implementation (interface, higher_order_config, params, created_at,
                                            updated_at, tracks, diagnostics, needs_token, provenance_audience,
                                            effects, execution, action_id, agent_id,
                                            dynamic_asks, dynamic_calls, dynamic_informs)
         SELECT 'echo', '{}', '{}', now(), now(), '[]', '[]', true, '[\"mikro\"]', 'UNKNOWN', 'PLAIN',
                $2, a.id, true, true, true FROM facade_agent a WHERE a.id = $1
         RETURNING id",
    )
    .bind(agent)
    .bind(action)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    (action, implementation)
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

fn echo(implementation: i64, x: i64) -> AssignInput {
    AssignInput {
        implementation: Some(implementation.to_string()),
        args: json!({"x": x}).as_object().unwrap().clone(),
        ..AssignInput::default()
    }
}

#[derive(Debug, sqlx::FromRow)]
struct Row {
    latest_event_kind: String,
    is_done: bool,
    dispatched_at: Option<DateTime<Utc>>,
    dispatch_attempts: i16,
    policy: String,
}

async fn row(ctx: &Context, task: i64) -> Row {
    sqlx::query_as(
        "SELECT latest_event_kind, is_done, dispatched_at, dispatch_attempts, policy
           FROM facade_task WHERE id = $1",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

async fn events(ctx: &Context, task: i64) -> Vec<(String, Option<String>, Option<Value>)> {
    sqlx::query_as(
        "SELECT kind, message, value FROM facade_taskevent WHERE task_id = $1 ORDER BY id",
    )
    .bind(task)
    .fetch_all(&ctx.db)
    .await
    .unwrap()
    .into_iter()
    .map(
        |(kind, message, value): (String, Option<String>, Option<sqlx::types::Json<Value>>)| {
            (kind, message, value.map(|v| v.0))
        },
    )
    .collect()
}

fn kinds(events: &[(String, Option<String>, Option<Value>)]) -> Vec<&str> {
    events.iter().map(|(kind, ..)| kind.as_str()).collect()
}

/// A root the person starts on `implementation` from their desk, with `policy`.
async fn root(ctx: &Context, desk: &CallerContext, implementation: i64, policy: &str) -> i64 {
    backend::assign_with_status(
        ctx,
        desk,
        &AssignInput {
            policy: Some(policy.into()),
            ..echo(implementation, 1)
        },
        AssignOrigin::default(),
    )
    .await
    .unwrap()
    .task
}

/// The child `agent` asks for under `parent`, over its socket, with a prompt.
async fn ask(ctx: &Context, agent: i64, parent: i64, implementation: i64) -> i64 {
    let reply = route(
        ctx,
        agent,
        &frame(json!({
            "type": "ASSIGN_REQUEST", "parent": parent.to_string(),
            "implementation": implementation.to_string(), "args": {"x": 2},
            "prompt": {"title": "Delegate the echo", "description": "It echoes better than I do."},
        })),
        None,
    )
    .await
    .unwrap();
    let Some(ToAgent::AssignResponse {
        task: Some(task),
        created: true,
        ..
    }) = reply
    else {
        panic!("{reply:?}")
    };
    task.parse().unwrap()
}

#[tokio::test]
async fn an_asked_child_waits_until_its_person_approves_it() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let (asker, runner) = (person.agent(&ctx).await, person.agent(&ctx).await);
    let (_, workflow) = action(&ctx, asker).await;
    let (_, echoing) = action(&ctx, runner).await;
    let desk = person.at_a_desk(&ctx).await;

    let parent = root(&ctx, &desk, workflow, "ASK").await;
    let child = ask(&ctx, asker, parent, echoing).await;

    let held = row(&ctx, child).await;
    assert_eq!(
        (
            held.latest_event_kind.as_str(),
            held.dispatched_at,
            held.dispatch_attempts,
            held.policy.as_str()
        ),
        ("HELD", None, 0, "ASK")
    );
    assert!(queued(&ctx, runner).await.is_empty(), "nothing is sent");
    let child_events = events(&ctx, child).await;
    assert_eq!(kinds(&child_events), ["HELD"]);
    let (_, message, value) = &child_events[0];
    assert_eq!(message.as_deref(), Some("Delegate the echo"));
    let value = value.as_ref().unwrap();
    assert_eq!(value["kind"], "assign");
    assert_eq!(
        value["prompt"]["description"],
        "It echoes better than I do."
    );
    assert_eq!(value["action"]["name"], "Echo");
    assert_eq!(value["agent"]["id"], runner.to_string());
    assert_eq!(value["args"], json!({"x": 2}));
    assert_eq!(value["caller_task"], parent.to_string());
    let parent_events = events(&ctx, parent).await;
    assert_eq!(
        kinds(&parent_events),
        ["HELD"],
        "the parent's run is told it waits"
    );
    assert_eq!(
        parent_events[0].2.as_ref().unwrap()["held"],
        child.to_string()
    );
    assert_eq!(
        row(&ctx, parent).await.latest_event_kind,
        "QUEUED",
        "the parent itself runs on"
    );

    // Its caller is told the child is held, with the prompt; the parent's HELD is not mirrored.
    let mirrors = child_mirrors(&ctx, &child.to_string()).await.unwrap();
    assert!(
        matches!(&mirrors[..], [ToAgent::HeldEvent { prompt: Some(prompt), .. }] if prompt.title == "Delegate the echo"),
        "{mirrors:?}"
    );
    assert!(child_mirrors(&ctx, &parent.to_string())
        .await
        .unwrap()
        .is_empty());

    // A held child is decided, not resumed or paused.
    for control in [Control::Resume { step: false }, Control::Pause] {
        assert!(matches!(
            backend::request_control(&ctx, &child.to_string(), control, None).await,
            Err(BackendError::Refused(_))
        ));
    }
    // The agent that asked cannot approve it, and neither can another user.
    let from_the_asker = CallerContext::from_agent(&ctx.db, asker, vec![])
        .await
        .unwrap();
    assert!(matches!(
        holds::decide(&ctx, &from_the_asker, &child.to_string(), true, None, None).await,
        Err(BackendError::Forbidden(_))
    ));
    let colleague = person.colleague().at_a_desk(&ctx).await;
    assert!(matches!(
        holds::decide(&ctx, &colleague, &child.to_string(), true, None, None).await,
        Err(BackendError::Forbidden(_))
    ));

    holds::decide(
        &ctx,
        &desk,
        &child.to_string(),
        true,
        Some("go ahead".into()),
        None,
    )
    .await
    .unwrap();
    let approved = row(&ctx, child).await;
    assert_eq!(
        (
            approved.latest_event_kind.as_str(),
            approved.dispatch_attempts
        ),
        ("QUEUED", 1)
    );
    assert!(approved.dispatched_at.is_some());
    let sent = queued(&ctx, runner).await;
    assert_eq!(sent.len(), 1);
    assert_eq!(
        (&sent[0]["type"], &sent[0]["task"], &sent[0]["message"]),
        (
            &json!("ASSIGN"),
            &json!(child.to_string()),
            &json!("go ahead")
        )
    );
    let child_events = events(&ctx, child).await;
    assert_eq!(kinds(&child_events), ["HELD", "DECIDED", "QUEUED"]);
    let decided = child_events[1].2.as_ref().unwrap();
    assert_eq!(
        (&decided["approved"], &decided["answer"], &decided["auto"]),
        (&json!(true), &json!("go ahead"), &json!(false))
    );
    assert_eq!(decided["user"], person.sub);
    assert_eq!(kinds(&events(&ctx, parent).await), ["HELD", "DECIDED"]);

    assert!(matches!(
        holds::decide(&ctx, &desk, &child.to_string(), true, None, None).await,
        Err(BackendError::Refused(_))
    ));
}

#[tokio::test]
async fn a_denied_child_never_runs_and_its_caller_is_told() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let (asker, runner) = (person.agent(&ctx).await, person.agent(&ctx).await);
    let (_, workflow) = action(&ctx, asker).await;
    let (_, echoing) = action(&ctx, runner).await;
    let desk = person.at_a_desk(&ctx).await;
    let parent = root(&ctx, &desk, workflow, "ASK").await;
    let child = ask(&ctx, asker, parent, echoing).await;

    holds::decide(
        &ctx,
        &desk,
        &child.to_string(),
        false,
        Some("not now".into()),
        None,
    )
    .await
    .unwrap();
    let denied = row(&ctx, child).await;
    assert_eq!(
        (denied.latest_event_kind.as_str(), denied.is_done),
        ("DENIED", true)
    );
    assert!(queued(&ctx, runner).await.is_empty());
    assert_eq!(
        kinds(&events(&ctx, child).await),
        ["HELD", "DECIDED", "DENIED"]
    );
    let mirrors = child_mirrors(&ctx, &child.to_string()).await.unwrap();
    assert!(
        matches!(mirrors.last(), Some(ToAgent::DeniedEvent { answer: Some(answer), .. }) if answer == "not now"),
        "{mirrors:?}"
    );
}

#[tokio::test]
async fn a_held_child_cancelled_before_anyone_decided_is_settled() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let (asker, runner) = (person.agent(&ctx).await, person.agent(&ctx).await);
    let (_, workflow) = action(&ctx, asker).await;
    let (_, echoing) = action(&ctx, runner).await;
    let desk = person.at_a_desk(&ctx).await;
    let parent = root(&ctx, &desk, workflow, "ASK").await;
    let child = ask(&ctx, asker, parent, echoing).await;

    backend::request_control(&ctx, &child.to_string(), Control::Cancel, None)
        .await
        .unwrap();
    let cancelled = row(&ctx, child).await;
    assert_eq!(
        (cancelled.latest_event_kind.as_str(), cancelled.is_done),
        ("CANCELLED", true)
    );
    assert!(queued(&ctx, runner).await.is_empty(), "nothing is sent");
}

#[tokio::test]
async fn an_auto_run_hands_every_child_on_and_a_child_cannot_change_that() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let (asker, runner) = (person.agent(&ctx).await, person.agent(&ctx).await);
    let (_, workflow) = action(&ctx, asker).await;
    let (_, echoing) = action(&ctx, runner).await;
    let desk = person.at_a_desk(&ctx).await;

    let parent = root(&ctx, &desk, workflow, "AUTO").await;
    let child = ask(&ctx, asker, parent, echoing).await;
    let running = row(&ctx, child).await;
    assert_eq!(
        (running.latest_event_kind.as_str(), running.policy.as_str()),
        ("QUEUED", "AUTO")
    );
    assert_eq!(queued(&ctx, runner).await.len(), 1);
    // ... and so are its children's children.
    let grandchild = ask(&ctx, runner, child, workflow).await;
    assert_eq!(row(&ctx, grandchild).await.latest_event_kind, "QUEUED");

    // Under ASK, a child that names AUTO is still held: the parent's policy is the child's.
    let asking = root(&ctx, &desk, workflow, "ASK").await;
    let insisting = backend::assign_with_status(
        &ctx,
        &CallerContext::from_agent(&ctx.db, asker, vec![])
            .await
            .unwrap(),
        &AssignInput {
            parent: Some(asking.to_string()),
            policy: Some("AUTO".into()),
            ..echo(echoing, 3)
        },
        AssignOrigin::default(),
    )
    .await
    .unwrap();
    let insisting = row(&ctx, insisting.task).await;
    assert_eq!(
        (
            insisting.latest_event_kind.as_str(),
            insisting.policy.as_str()
        ),
        ("HELD", "ASK")
    );

    assert!(matches!(
        backend::assign_with_status(
            &ctx,
            &desk,
            &AssignInput {
                policy: Some("SOMETIMES".into()),
                ..echo(workflow, 1)
            },
            AssignOrigin::default()
        )
        .await,
        Err(BackendError::Refused(_))
    ));
    // A root that says nothing asks.
    let unsaid =
        backend::assign_with_status(&ctx, &desk, &echo(workflow, 1), AssignOrigin::default())
            .await
            .unwrap();
    assert_eq!(row(&ctx, unsaid.task).await.policy, "ASK");
}

#[tokio::test]
async fn a_task_that_holds_itself_gets_the_answer_back() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let holder = person.agent(&ctx).await;
    let (_, workflow) = action(&ctx, holder).await;
    let desk = person.at_a_desk(&ctx).await;
    let hold = |task: i64| {
        frame(json!({
            "type": "HELD", "task": task.to_string(), "message": "Overwrite the blok?",
            "prompt": {"title": "Overwrite the blok", "description": "It replaces what is there."},
        }))
    };
    let decides = |frames: Vec<Value>| -> Vec<Value> {
        // Without the id the transport stamps on every frame.
        frames
            .into_iter()
            .filter(|f| f["type"] == "DECIDE")
            .map(|mut f| {
                f.as_object_mut().unwrap().remove("id");
                f
            })
            .collect()
    };

    // Under ASK it waits for a person, and a denial does not end it.
    let asking = root(&ctx, &desk, workflow, "ASK").await;
    route(&ctx, holder, &hold(asking), None).await.unwrap();
    assert_eq!(row(&ctx, asking).await.latest_event_kind, "HELD");
    assert!(decides(queued(&ctx, holder).await).is_empty());
    let from_the_holder = CallerContext::from_agent(&ctx.db, holder, vec![])
        .await
        .unwrap();
    assert!(matches!(
        holds::decide(
            &ctx,
            &from_the_holder,
            &asking.to_string(),
            true,
            None,
            None
        )
        .await,
        Err(BackendError::Forbidden(_))
    ));
    holds::decide(
        &ctx,
        &desk,
        &asking.to_string(),
        false,
        Some("keep it".into()),
        None,
    )
    .await
    .unwrap();
    let after = row(&ctx, asking).await;
    assert_eq!(
        (after.latest_event_kind.as_str(), after.is_done),
        ("DECIDED", false)
    );
    assert_eq!(
        decides(queued(&ctx, holder).await),
        [
            json!({"type": "DECIDE", "task": asking.to_string(), "approved": false, "answer": "keep it", "auto": false})
        ]
    );
    let held = events(&ctx, asking).await;
    assert_eq!(kinds(&held), ["HELD", "DECIDED"]);
    assert_eq!(held[0].2.as_ref().unwrap()["kind"], "self");

    // Under AUTO the server approves it at once.
    let auto = root(&ctx, &desk, workflow, "AUTO").await;
    route(&ctx, holder, &hold(auto), None).await.unwrap();
    let auto_events = events(&ctx, auto).await;
    assert_eq!(kinds(&auto_events), ["HELD", "DECIDED"]);
    assert_eq!(auto_events[1].2.as_ref().unwrap()["auto"], true);
    assert!(decides(queued(&ctx, holder).await).contains(
        &json!({"type": "DECIDE", "task": auto.to_string(), "approved": true, "auto": true})
    ));
}

/// The mirrors that reach `agent`'s caller group within a few seconds of `act`.
async fn mirrored_to(
    ctx: &Context,
    agent: i64,
    act: impl std::future::Future<Output = ()>,
) -> Vec<ToAgent> {
    let caller = facade::persist::caller_ops::get_or_create_caller_id(&ctx.db, agent)
        .await
        .unwrap();
    let (channel, mut inbox) = ctx.channel_layer.subscribe("specific").await.unwrap();
    ctx.channel_layer
        .group_add(&format!("task_caller_{caller}"), &channel)
        .await
        .unwrap();
    act.await;
    let mut mirrors = vec![];
    while let Ok(Some(message)) =
        tokio::time::timeout(std::time::Duration::from_millis(1500), inbox.recv()).await
    {
        if let Some(mirror) = facade::caller_events::mirror_of_channel_message(&message) {
            mirrors.push(mirror);
        }
    }
    ctx.channel_layer.unsubscribe(&channel);
    mirrors
}

fn cancelled(mirrors: &[ToAgent], task: i64) -> bool {
    mirrors.iter().any(
        |mirror| matches!(mirror, ToAgent::CancelledEvent(event) if event.task == task.to_string()),
    )
}

#[tokio::test]
async fn cancelling_the_root_ends_its_held_child_at_once_and_tells_its_caller() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let (asker, runner) = (person.agent(&ctx).await, person.agent(&ctx).await);
    let (_, workflow) = action(&ctx, asker).await;
    let (_, echoing) = action(&ctx, runner).await;
    let desk = person.at_a_desk(&ctx).await;
    let parent = root(&ctx, &desk, workflow, "ASK").await;
    let child = ask(&ctx, asker, parent, echoing).await;

    // The root's cancel is a request its agent confirms later; the held child has no agent to
    // confirm anything, so it ends with the request.
    let mirrors = mirrored_to(&ctx, asker, async {
        backend::request_control(&ctx, &parent.to_string(), Control::Cancel, None)
            .await
            .unwrap();
    })
    .await;
    let ended = row(&ctx, child).await;
    assert_eq!(
        (ended.latest_event_kind.as_str(), ended.is_done),
        ("CANCELLED", true)
    );
    assert_eq!(
        row(&ctx, parent).await.latest_event_kind,
        "QUEUED",
        "the root waits for its agent"
    );
    assert!(cancelled(&mirrors, child), "{mirrors:?}");
    assert!(queued(&ctx, runner).await.is_empty(), "nothing is sent");
    let sent = queued(&ctx, asker).await;
    assert_eq!(
        sent.last().unwrap()["type"],
        "CANCEL",
        "the root's agent is told"
    );
}

#[tokio::test]
async fn a_held_child_never_outlives_its_parent() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let (asker, runner) = (person.agent(&ctx).await, person.agent(&ctx).await);
    let (_, workflow) = action(&ctx, asker).await;
    let (_, echoing) = action(&ctx, runner).await;
    let desk = person.at_a_desk(&ctx).await;
    let parent = root(&ctx, &desk, workflow, "ASK").await;
    let child = ask(&ctx, asker, parent, echoing).await;

    // The parent ends on its own (here it fails) while the child still waits.
    let mirrors = mirrored_to(&ctx, asker, async {
        for report in [
            json!({"type": "STARTED", "task": parent.to_string()}),
            json!({"type": "FAILED", "task": parent.to_string(), "error": "gave up"}),
        ] {
            route(&ctx, asker, &frame(report), None).await.unwrap();
        }
    })
    .await;
    let ended = row(&ctx, child).await;
    assert_eq!(
        (ended.latest_event_kind.as_str(), ended.is_done),
        ("CANCELLED", true)
    );
    assert!(cancelled(&mirrors, child), "{mirrors:?}");
    assert!(matches!(
        holds::decide(&ctx, &desk, &child.to_string(), true, None, None).await,
        Err(BackendError::Refused(_))
    ));
}

/// The standard catalog of `agent`'s organization, drawing `Box`.
async fn standard_catalog(ctx: &Context, agent: i64) {
    sqlx::query(
        "INSERT INTO facade_module (name, components, operations, widget_defaults, organization_id)
         SELECT 'orkestrator', '[{\"name\": \"Box\"}]', '[]', '[]', organization_id FROM facade_agent WHERE id = $1
         ON CONFLICT (organization_id, name) DO UPDATE SET components = excluded.components",
    )
    .bind(agent)
    .execute(&ctx.db)
    .await
    .unwrap();
}

#[derive(Debug, sqlx::FromRow)]
struct Shown {
    scope: String,
    task_role: Option<String>,
    owner_task_id: Option<i64>,
    args: sqlx::types::Json<Value>,
    blok_owner: Option<i64>,
    returns: sqlx::types::Json<Value>,
}

async fn shown(ctx: &Context, materialized: i64) -> Option<Shown> {
    sqlx::query_as(
        "SELECT m.scope, m.task_role, m.owner_task_id, m.args, b.owner_task_id AS blok_owner, b.returns
           FROM facade_materializedblok m JOIN facade_blok b ON b.id = m.blok_id WHERE m.id = $1",
    )
    .bind(materialized)
    .fetch_optional(&ctx.db)
    .await
    .unwrap()
}

async fn bound(ctx: &Context, materialized: i64) -> Vec<(String, i64)> {
    sqlx::query_as(
        "SELECT key, agent_id FROM facade_blokagentmapping WHERE materialized_blok_id = $1 ORDER BY key",
    )
    .bind(materialized)
    .fetch_all(&ctx.db)
    .await
    .unwrap()
}

fn of_type(frames: Vec<Value>, kind: &str) -> Vec<Value> {
    frames
        .into_iter()
        .filter(|f| f["type"] == kind)
        .map(|mut f| {
            f.as_object_mut().unwrap().remove("id");
            f
        })
        .collect()
}

#[tokio::test]
async fn a_task_asks_through_a_blok_it_writes_and_gets_the_values_back() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let holder = person.agent(&ctx).await;
    standard_catalog(&ctx, holder).await;
    let (_, workflow) = action(&ctx, holder).await;
    let desk = person.at_a_desk(&ctx).await;
    let images = json!([
        {"__identifier": "@mikro/image", "object": "7"},
        {"__identifier": "@mikro/image", "object": "8"},
    ]);
    let ask = |task: i64| {
        frame(json!({
            "type": "HELD", "task": task.to_string(), "message": "Which one?",
            "blok": {
                "source": "<Html><Box/></Html>", "name": "Pick an image", "args": {"images": images},
                "returns": [{"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image",
                             "nullable": false, "effects": []}],
            },
        }))
    };

    let asking = root(&ctx, &desk, workflow, "ASK").await;
    route(&ctx, holder, &ask(asking), None).await.unwrap();
    let refused = of_type(queued(&ctx, holder).await, "BLOK_REFUSED");
    assert!(refused.is_empty(), "{refused:?}");
    assert_eq!(row(&ctx, asking).await.latest_event_kind, "HELD");
    let held = events(&ctx, asking).await;
    let materialized: i64 = held[0].2.as_ref().unwrap()["blok"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    let blok = shown(&ctx, materialized).await.unwrap();
    assert_eq!(
        (
            blok.scope.as_str(),
            blok.task_role.as_deref(),
            blok.owner_task_id,
            blok.blok_owner
        ),
        ("TASK", Some("ASK"), Some(asking), Some(asking))
    );
    assert_eq!(blok.args.0, json!({"images": images}));
    assert_eq!(blok.returns.0[0]["key"], "image");
    assert_eq!(
        bound(&ctx, materialized).await,
        [("self".to_string(), holder)]
    );

    // The answer is checked against the blok's returns: nothing answered misses the required image.
    assert!(matches!(
        holds::decide(&ctx, &desk, &asking.to_string(), true, None, None).await,
        Err(BackendError::Refused(_))
    ));
    // The answer is checked against the blok's returns.
    let not_an_image = json!({"image": 3}).as_object().unwrap().clone();
    assert!(matches!(
        holds::decide(
            &ctx,
            &desk,
            &asking.to_string(),
            true,
            None,
            Some(not_an_image)
        )
        .await,
        Err(BackendError::Refused(_))
    ));
    let picked = json!({"image": {"__identifier": "@mikro/image", "object": "8"}});
    holds::decide(
        &ctx,
        &desk,
        &asking.to_string(),
        true,
        None,
        Some(picked.as_object().unwrap().clone()),
    )
    .await
    .unwrap();
    assert_eq!(
        of_type(queued(&ctx, holder).await, "DECIDE"),
        [
            json!({"type": "DECIDE", "task": asking.to_string(), "approved": true, "auto": false, "values": picked})
        ]
    );
    let decided = events(&ctx, asking).await;
    assert_eq!(kinds(&decided), ["HELD", "DECIDED"]);
    assert_eq!(decided[1].2.as_ref().unwrap()["values"], picked);

    // Auto-mode cannot answer for a person: an ask waits under AUTO too.
    let auto = root(&ctx, &desk, workflow, "AUTO").await;
    route(&ctx, holder, &ask(auto), None).await.unwrap();
    assert_eq!(kinds(&events(&ctx, auto).await), ["HELD"]);

    // A plain hold carries no values.
    let plain = root(&ctx, &desk, workflow, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(json!({"type": "HELD", "task": plain.to_string()})),
        None,
    )
    .await
    .unwrap();
    assert!(matches!(
        holds::decide(
            &ctx,
            &desk,
            &plain.to_string(),
            true,
            None,
            Some(picked.as_object().unwrap().clone())
        )
        .await,
        Err(BackendError::Refused(_))
    ));
}

#[tokio::test]
async fn a_blok_that_cannot_be_made_refuses_the_hold_and_says_why() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let holder = person.agent(&ctx).await;
    standard_catalog(&ctx, holder).await;
    let (_, workflow) = action(&ctx, holder).await;
    let desk = person.at_a_desk(&ctx).await;

    for (blok, why) in [
        (json!({"source": "<Box"}), "does not hold"),
        (json!({"key": "nothing"}), "declares no ask"),
        (
            json!({"source": "<Html><Box/></Html>", "agent_mappings": {"other": "1"}}),
            "no dependency",
        ),
    ] {
        let task = root(&ctx, &desk, workflow, "ASK").await;
        route(
            &ctx,
            holder,
            &frame(json!({"type": "HELD", "task": task.to_string(), "blok": blok})),
            None,
        )
        .await
        .unwrap();
        assert_ne!(row(&ctx, task).await.latest_event_kind, "HELD", "{blok}");
        let refusals = of_type(queued(&ctx, holder).await, "BLOK_REFUSED");
        let refusal = refusals
            .iter()
            .find(|f| f["task"].as_str() == Some(task.to_string().as_str()))
            .unwrap_or_else(|| panic!("no refusal for {blok}"));
        let reason = refusal["reason"].as_str().unwrap();
        assert!(reason.contains(why), "{blok}: {reason}");
        let left: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM facade_materializedblok WHERE owner_task_id = $1",
        )
        .bind(task)
        .fetch_one(&ctx.db)
        .await
        .unwrap();
        assert_eq!(left, 0, "{blok}");
    }
}

/// What the UI catalog lacks is the UI's to draw (as an inline error): the task still holds,
/// and the blok carries it as a finding.
#[tokio::test]
async fn a_blok_the_catalog_does_not_know_still_holds_and_says_so() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let holder = person.agent(&ctx).await;
    standard_catalog(&ctx, holder).await;
    let (_, workflow) = action(&ctx, holder).await;
    let desk = person.at_a_desk(&ctx).await;

    let task = root(&ctx, &desk, workflow, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(json!({"type": "HELD", "task": task.to_string(), "blok": {"source": "<Html><Box><Knob/></Box></Html>"}})),
        None,
    )
    .await
    .unwrap();
    assert_eq!(row(&ctx, task).await.latest_event_kind, "HELD");
    let findings: Value = sqlx::query_scalar(
        "SELECT b.diagnostics FROM facade_materializedblok m JOIN facade_blok b ON b.id = m.blok_id
          WHERE m.owner_task_id = $1",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!(
        findings[0]["code"], "catalog.unknown_component",
        "{findings}"
    );
    assert!(findings[0]["message"].as_str().unwrap().contains("<Knob>"));
}

/// A `source` blok sent with a task keeps every v2 field it declares beside its tree, as a
/// declared blok does: its vocab, catalogs, module, locals, demo, queries and subscriptions.
#[tokio::test]
async fn a_source_blok_keeps_what_it_declares_beside_its_tree() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let holder = person.agent(&ctx).await;
    standard_catalog(&ctx, holder).await;
    let (_, workflow) = action(&ctx, holder).await;
    let desk = person.at_a_desk(&ctx).await;
    let local = json!({"key": "note", "kind": "STRING", "default": "", "persist": true});
    let query = json!({"key": "positions", "service": "mikro", "document": "query Positions { positions { id } }",
                       "variables": [], "poll": 5.0, "more": "positions"});
    let subscription = json!({"key": "live", "service": "mikro", "document": "subscription Live { live { id } }",
                              "variables": [], "keep": 3, "history": "positions"});

    let task = root(&ctx, &desk, workflow, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(json!({
            "type": "INFORM", "task": task.to_string(),
            "blok": {
                "source": "<Html><Box/></Html>", "vocab": 2, "catalogs": ["ui@1"], "module": "orkestrator",
                "locals": [local], "demo": {"args": {"done": 5}},
                "queries": [query], "subscriptions": [subscription],
            },
        })),
        None,
    )
    .await
    .unwrap();
    let refused = of_type(queued(&ctx, holder).await, "BLOK_REFUSED");
    assert!(refused.is_empty(), "{refused:?}");
    assert_stored(&ctx, task, &query).await;

    // As arkitekt-runtime dumps it: every unset field null, each local a full port.
    let task = root(&ctx, &desk, workflow, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(json!({
            "type": "INFORM", "task": task.to_string(),
            "blok": {"key": null, "id": null, "source": "<Html><Box/></Html>", "name": null, "args": null,
                     "returns": [{"key": "image", "label": null, "kind": "STRUCTURE", "description": null,
                                  "identifier": "@mikro/image", "nullable": false, "effects": null, "choices": null,
                                  "reference_unit": null, "proposed_units": null, "dimension": null,
                                  "children": null, "widget": null, "provides": null}],
                     "dependencies": null, "agent_mappings": null, "vocab": 2, "catalogs": ["ui@1"],
                     "locals": [{"key": "note", "label": null, "description": null, "identifier": null,
                                 "nullable": false, "effects": null, "choices": null, "reference_unit": null,
                                 "proposed_units": null, "dimension": null, "children": null, "validators": null,
                                 "default": "", "widget": null, "requires": null, "kind": "STRING", "persist": true}],
                     "demo": {"deps": null, "args": {"done": 5}, "task": null, "queries": null, "subscriptions": null},
                     "queries": [{"key": "positions", "service": "mikro", "document": "query Positions { positions { id } }",
                                  "variables": [], "poll": 5.0, "more": "positions"}],
                     "subscriptions": [{"key": "live", "service": "mikro", "document": "subscription Live { live { id } }",
                                        "variables": [], "keep": 3, "history": "positions"}],
                     "module": "orkestrator"},
        })),
        None,
    )
    .await
    .unwrap();
    let refused = of_type(queued(&ctx, holder).await, "BLOK_REFUSED");
    assert!(refused.is_empty(), "{refused:?}");
    assert_stored(&ctx, task, &query).await;
}

async fn assert_stored(ctx: &Context, task: i64, query: &Value) {
    let (vocab, catalogs, module, locals, demo, queries, subscriptions): (
        i16,
        Value,
        String,
        Value,
        Value,
        Value,
        Value,
    ) = sqlx::query_as(
        "SELECT b.vocab, b.catalogs, mo.name, b.locals, b.demo, b.queries, b.subscriptions
           FROM facade_materializedblok m JOIN facade_blok b ON b.id = m.blok_id
           JOIN facade_module mo ON mo.id = b.module_id
          WHERE m.owner_task_id = $1",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!((vocab, module.as_str()), (2, "orkestrator"));
    assert_eq!(catalogs, json!(["ui@1"]));
    assert_eq!(locals[0]["key"], "note");
    assert_eq!(locals[0]["kind"], "STRING");
    assert_eq!(locals[0]["persist"], true);
    assert_eq!(demo["args"], json!({"done": 5}));
    assert_eq!(queries[0]["more"], "positions");
    assert_eq!(queries[0]["document"], query["document"]);
    assert_eq!(subscriptions[0]["history"], "positions");
    assert_eq!(subscriptions[0]["keep"], 3);
}

#[tokio::test]
async fn an_inform_shows_a_blok_and_goes_with_its_task() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let holder = person.agent(&ctx).await;
    standard_catalog(&ctx, holder).await;
    let (_, workflow) = action(&ctx, holder).await;
    let desk = person.at_a_desk(&ctx).await;

    let task = root(&ctx, &desk, workflow, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(json!({
            "type": "INFORM", "task": task.to_string(), "message": "Halfway there",
            "blok": {"source": "<Html><Box/></Html>", "name": "Progress"},
        })),
        None,
    )
    .await
    .unwrap();
    let shown_events = events(&ctx, task).await;
    let inform = shown_events
        .iter()
        .find(|(kind, ..)| kind == "INFORM")
        .expect("an INFORM event");
    assert_eq!(inform.1.as_deref(), Some("Halfway there"));
    let materialized: i64 = inform.2.as_ref().unwrap()["blok"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    assert_eq!(
        shown(&ctx, materialized)
            .await
            .unwrap()
            .task_role
            .as_deref(),
        Some("INFORM")
    );
    assert_ne!(row(&ctx, task).await.latest_event_kind, "HELD");

    let mut conn = ctx.db.acquire().await.unwrap();
    facade::deletion::delete_tasks(&mut conn, &[task])
        .await
        .unwrap();
    assert!(shown(&ctx, materialized).await.is_none());
    let owned: i64 =
        sqlx::query_scalar("SELECT count(*) FROM facade_blok WHERE owner_task_id = $1")
            .bind(task)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(owned, 0);
}

/// A blok made at run time (`source`, or any blok by `id`) shows only when the task's
/// implementation declares it may and the agent's token grants the scope: an ask needs
/// `dynamic_asks` and `rekuest_dynamic_ask`, an inform `dynamic_informs` and
/// `rekuest_dynamic_inform` (`rekuest_all` grants both).
#[tokio::test]
async fn a_blok_made_at_run_time_needs_the_flag_and_the_scope() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let holder = person.agent(&ctx).await;
    standard_catalog(&ctx, holder).await;
    let (_, workflow) = action(&ctx, holder).await;
    let desk = person.at_a_desk(&ctx).await;
    let blok = json!({"source": "<Html><Box/></Html>"});

    let refused = |task: i64| {
        let ctx = &ctx;
        async move {
            let refusals = of_type(queued(ctx, holder).await, "BLOK_REFUSED");
            refusals
                .iter()
                .find(|f| f["task"].as_str() == Some(task.to_string().as_str()))
                .map(|f| f["reason"].as_str().unwrap().to_owned())
        }
    };

    sqlx::query("UPDATE facade_implementation SET dynamic_asks = false WHERE id = $1")
        .bind(workflow)
        .execute(&ctx.db)
        .await
        .unwrap();
    let task = root(&ctx, &desk, workflow, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(json!({"type": "HELD", "task": task.to_string(), "blok": blok})),
        None,
    )
    .await
    .unwrap();
    assert_ne!(row(&ctx, task).await.latest_event_kind, "HELD");
    assert!(refused(task).await.unwrap().contains("dynamic_asks"));

    sqlx::query("UPDATE facade_implementation SET dynamic_asks = true WHERE id = $1")
        .bind(workflow)
        .execute(&ctx.db)
        .await
        .unwrap();
    sqlx::query("UPDATE facade_agent SET granted_scopes = '[\"rekuest_read\"]' WHERE id = $1")
        .bind(holder)
        .execute(&ctx.db)
        .await
        .unwrap();
    let task = root(&ctx, &desk, workflow, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(json!({"type": "HELD", "task": task.to_string(), "blok": blok})),
        None,
    )
    .await
    .unwrap();
    assert_ne!(row(&ctx, task).await.latest_event_kind, "HELD");
    assert!(refused(task).await.unwrap().contains("rekuest_dynamic_ask"));

    sqlx::query(
        "UPDATE facade_agent SET granted_scopes = '[\"rekuest_dynamic_ask\"]' WHERE id = $1",
    )
    .bind(holder)
    .execute(&ctx.db)
    .await
    .unwrap();
    let task = root(&ctx, &desk, workflow, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(json!({"type": "HELD", "task": task.to_string(), "blok": blok})),
        None,
    )
    .await
    .unwrap();
    assert_eq!(row(&ctx, task).await.latest_event_kind, "HELD");

    // An inform has its own flag, which the ask's does not stand in for.
    sqlx::query("UPDATE facade_implementation SET dynamic_informs = false WHERE id = $1")
        .bind(workflow)
        .execute(&ctx.db)
        .await
        .unwrap();
    let task = root(&ctx, &desk, workflow, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(json!({"type": "INFORM", "task": task.to_string(), "blok": blok})),
        None,
    )
    .await
    .unwrap();
    assert!(refused(task).await.unwrap().contains("dynamic_informs"));
}

/// An ask by key names one of the asks the task's own implementation declares, never another
/// implementation's.
#[tokio::test]
async fn an_ask_by_key_is_one_its_implementation_declares() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let holder = person.agent(&ctx).await;
    standard_catalog(&ctx, holder).await;
    let (first, workflow) = action(&ctx, holder).await;
    // A second action of the same agent: the first is moved out of the way of its names.
    sqlx::query("UPDATE facade_implementation SET interface = 'pick' WHERE id = $1")
        .bind(workflow)
        .execute(&ctx.db)
        .await
        .unwrap();
    sqlx::query("UPDATE facade_action SET key = 'pick' WHERE id = $1")
        .bind(first)
        .execute(&ctx.db)
        .await
        .unwrap();
    let (_, other) = action(&ctx, holder).await;
    let desk = person.at_a_desk(&ctx).await;

    // Any blok will do as the declared one: one a task made is at hand.
    let made = root(&ctx, &desk, workflow, "ASK").await;
    route(&ctx, holder, &frame(json!({"type": "HELD", "task": made.to_string(), "blok": {"source": "<Html><Box/></Html>"}})), None)
        .await
        .unwrap();
    let blok: i64 =
        sqlx::query_scalar("SELECT blok_id FROM facade_materializedblok WHERE owner_task_id = $1")
            .bind(made)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    sqlx::query(
        "INSERT INTO facade_implementationinteraction (implementation_id, key, role, description, blok_id, created_at, updated_at)
         VALUES ($1, 'asks.pick', 'ASK', 'Pick one.', $2, now(), now())",
    )
    .bind(workflow)
    .bind(blok)
    .execute(&ctx.db)
    .await
    .unwrap();

    let task = root(&ctx, &desk, workflow, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(json!({"type": "HELD", "task": task.to_string(), "blok": {"key": "asks.pick"}})),
        None,
    )
    .await
    .unwrap();
    assert_eq!(row(&ctx, task).await.latest_event_kind, "HELD");

    let elsewhere = root(&ctx, &desk, other, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(
            json!({"type": "HELD", "task": elsewhere.to_string(), "blok": {"key": "asks.pick"}}),
        ),
        None,
    )
    .await
    .unwrap();
    assert_ne!(row(&ctx, elsewhere).await.latest_event_kind, "HELD");
    let refusals = of_type(queued(&ctx, holder).await, "BLOK_REFUSED");
    assert!(refusals.iter().any(
        |f| f["task"].as_str() == Some(elsewhere.to_string().as_str())
            && f["reason"].as_str().unwrap().contains("declares no ask")
    ));
}


/// An inform by key names one of the informs the task's own implementation declares: an ask of
/// the same key is not one, nor is a blok the agent's app declares.
#[tokio::test]
async fn an_inform_by_key_is_one_its_implementation_declares() {
    let Some(ctx) = context().await else { return };
    let person = Person::new();
    let holder = person.agent(&ctx).await;
    standard_catalog(&ctx, holder).await;
    let (_, workflow) = action(&ctx, holder).await;
    let desk = person.at_a_desk(&ctx).await;

    let made = root(&ctx, &desk, workflow, "ASK").await;
    route(&ctx, holder, &frame(json!({"type": "INFORM", "task": made.to_string(), "blok": {"source": "<Html><Box/></Html>"}})), None)
        .await
        .unwrap();
    let blok: i64 =
        sqlx::query_scalar("SELECT blok_id FROM facade_materializedblok WHERE owner_task_id = $1")
            .bind(made)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    sqlx::query(
        "INSERT INTO facade_implementationinteraction (implementation_id, key, role, description, blok_id, created_at, updated_at)
         VALUES ($1, 'ui.progress', 'INFORM', 'Progress.', $2, now(), now()),
                ($1, 'ui.pick', 'ASK', 'Pick one.', $2, now(), now())",
    )
    .bind(workflow)
    .bind(blok)
    .execute(&ctx.db)
    .await
    .unwrap();

    let task = root(&ctx, &desk, workflow, "ASK").await;
    route(
        &ctx,
        holder,
        &frame(json!({"type": "INFORM", "task": task.to_string(), "message": "Progress.", "blok": {"key": "ui.progress"}})),
        None,
    )
    .await
    .unwrap();
    assert!(events(&ctx, task).await.iter().any(|(kind, ..)| kind == "INFORM"));

    for key in ["ui.pick", "progress_panel"] {
        let task = root(&ctx, &desk, workflow, "ASK").await;
        route(
            &ctx,
            holder,
            &frame(json!({"type": "INFORM", "task": task.to_string(), "blok": {"key": key}})),
            None,
        )
        .await
        .unwrap();
        assert!(!events(&ctx, task).await.iter().any(|(kind, ..)| kind == "INFORM"));
        let refusals = of_type(queued(&ctx, holder).await, "BLOK_REFUSED");
        assert!(refusals.iter().any(
            |f| f["task"].as_str() == Some(task.to_string().as_str())
                && f["reason"].as_str().unwrap().contains("declares no inform")
        ));
    }
}
