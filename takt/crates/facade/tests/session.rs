//! Caller sessions over a real socket (docs/session-protocol.md): following a task, a resume
//! that replays exactly what was missed, the gap fill, scoping, calls and their memory, and
//! the dead man switch. Needs `TAKT_TEST_DATABASE_URL` and `TAKT_TEST_REDIS_URL`
//! (`eval "$(scripts/test-db.sh)"`); skipped without them.

use std::sync::Arc;
use std::time::Duration;

use authentikate::base_models::StaticToken;
use axum::extract::{State, WebSocketUpgrade};
use axum::response::Response;
use axum::routing::get;
use facade::caller_context::CallerContext;
use facade::consumers::connections::Connections;
use facade::message_router::route;
use facade::messages::AgentFrame;
use facade::persist::transitions::{self, NewEvent};
use facade::settings::Settings;
use facade::Context;
use futures::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio_tungstenite::tungstenite::Message;

type Socket =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// Who the tests are: one organization; a reader-and-caller, a reader only, and a stranger.
struct Tokens {
    unique: String,
}

impl Tokens {
    fn new() -> Self {
        Self {
            unique: uuid::Uuid::new_v4().simple().to_string(),
        }
    }
    fn caller(&self) -> String {
        format!("caller-{}", self.unique)
    }
    fn reader(&self) -> String {
        format!("reader-{}", self.unique)
    }
    fn stranger(&self) -> String {
        format!("stranger-{}", self.unique)
    }
    fn agent(&self) -> String {
        format!("agent-{}", self.unique)
    }
    fn spec(&self, who: &str, org: &str, scope: &str) -> Value {
        json!({
            "sub": format!("{who}-{}", self.unique), "iss": "session-tests",
            "org": format!("{org}-{}", self.unique), "client_id": format!("c-{who}-{}", self.unique),
            "client_app": format!("a-{who}-{}", self.unique), "client_release": "1",
            "scope": scope, "roles": ["rekuest_editor"],
        })
    }
    fn settings(&self) -> Value {
        json!({
            "audience": "rekuest",
            "static_tokens": {
                self.caller(): self.spec("caller", "o", "rekuest_read rekuest_root_call"),
                self.reader(): self.spec("reader", "o", "rekuest_read"),
                self.stranger(): self.spec("stranger", "elsewhere", "rekuest_read rekuest_root_call"),
                self.agent(): self.spec("agent", "o", "rekuest_agent"),
            }
        })
    }
}

async fn context(tokens: &Tokens) -> Option<Context> {
    let db_url = std::env::var("TAKT_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("TAKT_TEST_REDIS_URL").ok()?;
    let db = sqlx::PgPool::connect(&db_url).await.expect("the test database answers");
    let redis_client = redis::Client::open(redis_url).unwrap();
    let redis = redis::aio::ConnectionManager::new(redis_client.clone())
        .await
        .unwrap();
    let auth = authentikate::AuthentikateSettings::prepare(&tokens.settings(), true).unwrap();
    let channel_layer = kante::ChannelLayer::new(
        redis_client.clone(),
        kante::ChannelLayerConfig {
            prefix: "rekuest".into(),
            ..kante::ChannelLayerConfig::default()
        },
    )
    .await
    .unwrap();
    let mut settings = Settings::default();
    settings.session.watch_check = Duration::from_millis(300);
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

/// takt's `/session`, on a port of its own.
async fn serve(ctx: &Context) -> String {
    async fn socket(State(ctx): State<Context>, upgrade: WebSocketUpgrade) -> Response {
        upgrade.on_upgrade(move |socket| facade::session::serve(ctx, socket))
    }
    let router = axum::Router::new()
        .route("/session", get(socket))
        .with_state(ctx.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, router).await });
    format!("ws://{address}/session")
}

async fn connect(url: &str) -> Socket {
    tokio_tungstenite::connect_async(url).await.unwrap().0
}

async fn send(socket: &mut Socket, frame: Value) {
    socket
        .send(Message::Text(frame.to_string().into()))
        .await
        .unwrap();
}

/// The next frame that is not a PONG.
async fn recv(socket: &mut Socket) -> Value {
    loop {
        let message = tokio::time::timeout(Duration::from_secs(5), socket.next())
            .await
            .expect("a frame within 5 s")
            .expect("the socket is open")
            .unwrap();
        if let Message::Text(text) = message {
            let frame: Value = serde_json::from_str(&text).unwrap();
            if frame["type"] != "PONG" {
                return frame;
            }
        }
    }
}

async fn hello(socket: &mut Socket, token: &str, session: Option<&str>, last_pos: u64) -> Value {
    send(
        socket,
        json!({"type": "HELLO", "token": token, "session": session, "last_pos": last_pos,
               "client_info": {"app": "tests"}}),
    )
    .await;
    let welcome = recv(socket).await;
    assert_eq!(welcome["type"], "WELCOME", "{welcome}");
    welcome
}

/// An agent of the tests' organization, and an action.
async fn agent(ctx: &Context, tokens: &Tokens) -> (i64, i64) {
    let spec: StaticToken = serde_json::from_value(tokens.spec("agent", "o", "rekuest_agent")).unwrap();
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
    .bind(format!("act-{}", uuid::Uuid::new_v4().simple()))
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    (agent, action)
}

/// The caller a token's session acts as.
async fn caller_of(ctx: &Context, tokens: &Tokens, who: &str) -> i64 {
    let spec: StaticToken =
        serde_json::from_value(tokens.spec(who, "o", "rekuest_read rekuest_root_call")).unwrap();
    let token = spec.to_token(chrono::Utc::now(), "raw");
    let identity = authentikate::expand::expand_token_context(&ctx.db, &token)
        .await
        .unwrap();
    let principal = CallerContext::load(
        &ctx.db,
        identity.user,
        identity.client,
        Some(identity.organization),
        vec![],
    )
    .await
    .unwrap();
    facade::backend::get_caller_for_context(&ctx.db, &principal)
        .await
        .unwrap()
}

async fn task(ctx: &Context, agent: i64, action: i64, caller: Option<i64>) -> i64 {
    sqlx::query_scalar(
        "INSERT INTO facade_task (acted_on, ephemeral, reference, resumes, capture, is_higher_order_child,
                                  latest_event_kind, latest_instruct_kind, is_done, created_at,
                                  updated_at, revision, step, dispatch_attempts, trigger_depth, action_id, agent_id,
                                  dispatched_at, caller_id)
         VALUES ('{}', false, $3, 0, false, false, 'QUEUED', 'ASSIGN', false, now(), now(), 0, false, 1, 0,
                 $2, $1, now(), $4)
         RETURNING id",
    )
    .bind(agent)
    .bind(action)
    .bind(uuid::Uuid::new_v4().to_string())
    .bind(caller)
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

/// An agent reports on its task, as over its own socket.
async fn report(ctx: &Context, agent: i64, body: Value) {
    let mut body = body;
    body.as_object_mut()
        .unwrap()
        .entry("id")
        .or_insert_with(|| json!(uuid::Uuid::new_v4().to_string()));
    let frame: AgentFrame = serde_json::from_value(body).unwrap();
    route(ctx, agent, &frame, None).await.unwrap();
}

fn seqs(frames: &[Value]) -> Vec<i64> {
    frames
        .iter()
        .map(|frame| frame["item"]["task_seq"].as_i64().unwrap())
        .collect()
}

#[tokio::test]
async fn a_followed_task_resumes_with_exactly_what_was_missed() {
    let tokens = Tokens::new();
    let Some(ctx) = context(&tokens).await else { return };
    let url = serve(&ctx).await;
    let (agent, action) = agent(&ctx, &tokens).await;
    let task = task(&ctx, agent, action, None).await;
    let t = task.to_string();

    let mut socket = connect(&url).await;
    let welcome = hello(&mut socket, &tokens.caller(), None, 0).await;
    assert_eq!(welcome["resumed"], false);
    let session = welcome["session"].as_str().unwrap().to_owned();

    send(&mut socket, json!({"type": "WATCH", "topic": format!("task:{t}")})).await;
    let snapshot = recv(&mut socket).await;
    assert_eq!(
        (snapshot["type"].as_str(), snapshot["reason"].as_str()),
        (Some("RESYNC"), Some("new"))
    );
    assert_eq!(snapshot["snapshot"]["task"]["id"], t);
    assert_eq!(snapshot["snapshot"]["task_seq"], 0);

    report(&ctx, agent, json!({"type": "STARTED", "task": t})).await;
    report(&ctx, agent, json!({"type": "LOG", "task": t, "message": "one", "level": "INFO"})).await;
    let live = vec![recv(&mut socket).await, recv(&mut socket).await];
    assert_eq!(seqs(&live), vec![1, 2]);
    let last = live[1]["pos"].as_u64().unwrap();
    // The window says how far it got, then goes away.
    send(&mut socket, json!({"type": "PING", "pos": last})).await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    socket.close(None).await.unwrap();
    tokio::time::sleep(Duration::from_millis(300)).await;

    // Recorded while nobody watched.
    report(&ctx, agent, json!({"type": "YIELD", "task": t, "returns": {"n": 1}})).await;
    report(&ctx, agent, json!({"type": "COMPLETED", "task": t})).await;

    let mut socket = connect(&url).await;
    let welcome = hello(&mut socket, &tokens.caller(), Some(&session), last).await;
    assert_eq!(welcome["resumed"], true);
    assert_eq!(welcome["session"], session);
    let replayed = vec![recv(&mut socket).await, recv(&mut socket).await];
    assert!(replayed.iter().all(|frame| frame["type"] == "DATA"));
    assert_eq!(seqs(&replayed), vec![3, 4]);
    assert!(replayed[0]["pos"].as_u64().unwrap() > last);
    assert_eq!(replayed[1]["item"]["kind"], "COMPLETED");
}

#[tokio::test]
async fn an_event_whose_broadcast_was_lost_is_sent_before_the_next() {
    let tokens = Tokens::new();
    let Some(ctx) = context(&tokens).await else { return };
    let url = serve(&ctx).await;
    let (agent, action) = agent(&ctx, &tokens).await;
    let task = task(&ctx, agent, action, None).await;
    let t = task.to_string();

    let mut socket = connect(&url).await;
    hello(&mut socket, &tokens.reader(), None, 0).await;
    send(&mut socket, json!({"type": "WATCH", "topic": format!("task:{t}")})).await;
    assert_eq!(recv(&mut socket).await["type"], "RESYNC");

    // Recorded, but its broadcast never went out.
    let silent = NewEvent::default().with_message(Some("silent".into()));
    transitions::insert_event(&ctx.db, task, "LOG", &silent)
        .await
        .unwrap();
    report(&ctx, agent, json!({"type": "LOG", "task": t, "message": "loud", "level": "INFO"})).await;

    let frames = vec![recv(&mut socket).await, recv(&mut socket).await];
    assert_eq!(seqs(&frames), vec![1, 2]);
    assert_eq!(frames[0]["item"]["message"], "silent");
    assert_eq!(frames[1]["item"]["message"], "loud");
}

#[tokio::test]
async fn a_quiet_task_is_checked_for_what_the_feed_lost() {
    let tokens = Tokens::new();
    let Some(ctx) = context(&tokens).await else { return };
    let url = serve(&ctx).await;
    let (agent, action) = agent(&ctx, &tokens).await;
    let task = task(&ctx, agent, action, None).await;

    let mut socket = connect(&url).await;
    hello(&mut socket, &tokens.reader(), None, 0).await;
    send(&mut socket, json!({"type": "WATCH", "topic": format!("task:{task}")})).await;
    assert_eq!(recv(&mut socket).await["type"], "RESYNC");

    // The last event, and its broadcast lost: nothing follows to reveal the gap.
    let last = NewEvent::default().with_message(Some("the end".into()));
    transitions::insert_event(&ctx.db, task, "LOG", &last)
        .await
        .unwrap();
    let found = recv(&mut socket).await;
    assert_eq!(found["item"]["message"], "the end");
}

#[tokio::test]
async fn a_session_sees_only_its_organization() {
    let tokens = Tokens::new();
    let Some(ctx) = context(&tokens).await else { return };
    let url = serve(&ctx).await;
    let (agent, action) = agent(&ctx, &tokens).await;
    let task = task(&ctx, agent, action, None).await;

    let mut socket = connect(&url).await;
    hello(&mut socket, &tokens.stranger(), None, 0).await;
    send(&mut socket, json!({"type": "WATCH", "topic": format!("task:{task}")})).await;
    let refused = recv(&mut socket).await;
    assert_eq!(refused["type"], "REFUSED");
    assert_eq!(refused["topic"], format!("task:{task}"));
    assert_eq!(refused["error"], format!("No Task {task} in this organization."));

    // A token the server does not know never gets a session.
    let mut socket = connect(&url).await;
    send(&mut socket, json!({"type": "HELLO", "token": "nobody"})).await;
    let closed = tokio::time::timeout(Duration::from_secs(5), socket.next())
        .await
        .unwrap();
    match closed {
        Some(Ok(Message::Close(Some(frame)))) => assert_eq!(u16::from(frame.code), 4401),
        other => panic!("expected a 4401 close, got {other:?}"),
    }
}

#[tokio::test]
async fn calls_need_their_scope_and_are_answered_once() {
    let tokens = Tokens::new();
    let Some(ctx) = context(&tokens).await else { return };
    let url = serve(&ctx).await;
    let (agent, action) = agent(&ctx, &tokens).await;
    let caller = caller_of(&ctx, &tokens, "caller").await;
    let task = task(&ctx, agent, action, Some(caller)).await;

    let mut reader = connect(&url).await;
    hello(&mut reader, &tokens.reader(), None, 0).await;
    send(
        &mut reader,
        json!({"type": "DEADMAN", "request": "r1", "task": task.to_string(), "seconds": 60}),
    )
    .await;
    let refused = recv(&mut reader).await;
    assert_eq!((refused["type"].as_str(), refused["request"].as_str()), (Some("REFUSED"), Some("r1")));

    let mut socket = connect(&url).await;
    let welcome = hello(&mut socket, &tokens.caller(), None, 0).await;
    let session = welcome["session"].as_str().unwrap().to_owned();
    let arm = json!({"type": "DEADMAN", "request": "arm-1", "task": task.to_string(), "seconds": 60});
    send(&mut socket, arm.clone()).await;
    let first = recv(&mut socket).await;
    assert_eq!(first["type"], "ACCEPTED", "{first}");
    let armed: (Option<i32>, Option<String>) = sqlx::query_as(
        "SELECT deadman_seconds, deadman_session_id FROM facade_task WHERE id = $1",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!(armed, (Some(60), Some(session)));

    // Sent again (after a reconnect, say): the same answer, applied once.
    sqlx::query("UPDATE facade_task SET deadman_seconds = NULL WHERE id = $1")
        .bind(task)
        .execute(&ctx.db)
        .await
        .unwrap();
    send(&mut socket, arm).await;
    let again = recv(&mut socket).await;
    assert_eq!(again["request"], first["request"]);
    assert_eq!(again["result"], first["result"]);
    let seconds: Option<i32> =
        sqlx::query_scalar("SELECT deadman_seconds FROM facade_task WHERE id = $1")
            .bind(task)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(seconds, None, "the resend was not applied again");
}

#[tokio::test]
async fn the_dead_man_switch_fires_on_absence_but_not_after_an_outage() {
    let tokens = Tokens::new();
    let Some(ctx) = context(&tokens).await else { return };
    let url = serve(&ctx).await;
    let (agent, action) = agent(&ctx, &tokens).await;
    let caller = caller_of(&ctx, &tokens, "caller").await;
    let watched = task(&ctx, agent, action, Some(caller)).await;
    let other = task(&ctx, agent, action, Some(caller)).await;

    let mut socket = connect(&url).await;
    let session = hello(&mut socket, &tokens.caller(), None, 0).await["session"]
        .as_str()
        .unwrap()
        .to_owned();
    for (i, task) in [watched, other].into_iter().enumerate() {
        send(
            &mut socket,
            json!({"type": "DEADMAN", "request": format!("arm-{i}"), "task": task.to_string(), "seconds": 15}),
        )
        .await;
        assert_eq!(recv(&mut socket).await["type"], "ACCEPTED");
    }
    socket.close(None).await.unwrap();
    tokio::time::sleep(Duration::from_millis(300)).await;

    // takt was serving all along (a fresh beat), and the window has been gone a minute.
    facade::session::deadman::sweep(&ctx, 100).await.unwrap();
    sqlx::query("UPDATE facade_callersession SET last_seen = now() - interval '60 seconds' WHERE id = $1")
        .bind(&session)
        .execute(&ctx.db)
        .await
        .unwrap();
    // Only `watched` is due: `other` was disarmed meanwhile.
    sqlx::query("UPDATE facade_task SET deadman_seconds = NULL, deadman_session_id = NULL WHERE id = $1")
        .bind(other)
        .execute(&ctx.db)
        .await
        .unwrap();
    facade::session::deadman::sweep(&ctx, 100).await.unwrap();
    let kinds: Vec<String> =
        sqlx::query_scalar("SELECT kind FROM facade_taskevent WHERE task_id = $1 ORDER BY id")
            .bind(watched)
            .fetch_all(&ctx.db)
            .await
            .unwrap();
    assert_eq!(kinds, vec!["LOG", "CANCELLING"]);
    let disarmed: Option<i32> =
        sqlx::query_scalar("SELECT deadman_seconds FROM facade_task WHERE id = $1")
            .bind(watched)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(disarmed, None);
    let untouched: i64 =
        sqlx::query_scalar("SELECT count(*) FROM facade_taskevent WHERE task_id = $1")
            .bind(other)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(untouched, 0);

    // Armed again, the window gone a minute, but takt was down for those minutes: nothing fires.
    let rearmed = task(&ctx, agent, action, Some(caller)).await;
    sqlx::query(
        "UPDATE facade_task SET deadman_seconds = 15, deadman_session_id = $2 WHERE id = $1",
    )
    .bind(rearmed)
    .bind(&session)
    .execute(&ctx.db)
    .await
    .unwrap();
    sqlx::query("UPDATE facade_callersession SET last_seen = now() - interval '60 seconds' WHERE id = $1")
        .bind(&session)
        .execute(&ctx.db)
        .await
        .unwrap();
    sqlx::query("UPDATE facade_sweepbeat SET at = now() - interval '10 minutes' WHERE name = 'reaper'")
        .execute(&ctx.db)
        .await
        .unwrap();
    assert_eq!(facade::session::deadman::sweep(&ctx, 100).await.unwrap(), 0);
}

#[tokio::test]
async fn a_window_back_after_its_grace_keeps_its_dead_man_runs() {
    let tokens = Tokens::new();
    let Some(ctx) = context(&tokens).await else { return };
    let url = serve(&ctx).await;
    let (agent, action) = agent(&ctx, &tokens).await;
    let caller = caller_of(&ctx, &tokens, "caller").await;
    let task = task(&ctx, agent, action, Some(caller)).await;

    let mut socket = connect(&url).await;
    let old = hello(&mut socket, &tokens.caller(), None, 0).await["session"]
        .as_str()
        .unwrap()
        .to_owned();
    send(
        &mut socket,
        json!({"type": "DEADMAN", "request": "arm", "task": task.to_string(), "seconds": 300}),
    )
    .await;
    assert_eq!(recv(&mut socket).await["type"], "ACCEPTED");
    socket.close(None).await.unwrap();
    tokio::time::sleep(Duration::from_millis(300)).await;
    // Its grace ran out: the record is gone.
    facade::session::registry::forget(&ctx, &old).await;

    let mut socket = connect(&url).await;
    let welcome = hello(&mut socket, &tokens.caller(), Some(&old), 0).await;
    assert_eq!(welcome["resumed"], false);
    let new = welcome["session"].as_str().unwrap();
    assert_ne!(new, old);
    let holder: Option<String> =
        sqlx::query_scalar("SELECT deadman_session_id FROM facade_task WHERE id = $1")
            .bind(task)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(holder.as_deref(), Some(new));
}

#[test]
fn every_frame_in_the_wire_examples_reads_and_writes_back() {
    let examples: Value =
        serde_json::from_str(include_str!("fixtures/session_wire_examples.json")).unwrap();
    for frame in examples["client"].as_array().unwrap() {
        let parsed: Result<facade::session::frames::ClientFrame, _> =
            serde_json::from_value(frame.clone());
        assert!(parsed.is_ok(), "{frame}: {:?}", parsed.err());
    }
    for frame in examples["server"].as_array().unwrap() {
        let parsed: facade::session::frames::ServerFrame =
            serde_json::from_value(frame.clone()).unwrap_or_else(|e| panic!("{frame}: {e}"));
        assert_eq!(&serde_json::to_value(&parsed).unwrap(), frame);
    }
}
