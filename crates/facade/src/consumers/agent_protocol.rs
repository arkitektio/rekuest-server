//! The agent conversation (`facade/consumers/agent_protocol.py`).
//!
//! Before `REGISTER`, [`serve`] parses and gates: the first frame must be a `REGISTER` whose token
//! authenticates, whose agent is not blocked and whose lease it wins. After it, a registered
//! session runs four things side by side:
//!
//! * the **read loop**, which answers heartbeats itself and hands every other frame, in order, to
//! * the **worker**, which routes them (persistence, replies);
//! * the **heartbeat**, which pings, waits for the answer and renews the lease; and
//! * the **drain**, which delivers the agent's queue, fenced by the lease on every frame; and
//! * the **mirror**, which forwards the events of work this agent assigned (its caller group,
//!   `task_caller_{caller}`, joined before `INIT` and re-joined hourly) as `…_EVENT` frames.
//!
//! Unlike the Python server, a heartbeat answer never waits behind the reports the agent sent
//! before it: liveness means "the agent answers", not "our backlog is short".

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::ws::{CloseFrame, Message, WebSocket};
use futures::{SinkExt, StreamExt};
use serde_json::Value;
use tokio::sync::{mpsc, oneshot};

use crate::caller_events::{build_execution_event, EventLike};
use crate::channels;
use crate::codes;
use crate::consumers::agent_queue::AgentQueue;
use crate::consumers::connections::Control;
use crate::context::Context;
use crate::message_router;
use crate::messages::{AgentFrame, FromAgent, Inquiry, ToAgent, ToAgentFrame};
use crate::persist::{caller_ops, leases};
use crate::persist::{positions, reports};
use crate::probes;
use crate::registration;

/// What the writer task sends: a frame, or the close that ends the connection.
enum Outbound {
    Text(String),
    Close(u16),
}

/// The single path to the socket: frames from the loops never interleave.
#[derive(Clone)]
pub struct Sender {
    out: mpsc::UnboundedSender<Outbound>,
}

impl Sender {
    pub fn send(&self, message: ToAgent) {
        let frame = ToAgentFrame {
            id: Some(uuid::Uuid::new_v4().to_string()),
            message,
        };
        match serde_json::to_string(&frame) {
            Ok(text) => {
                let _ = self.out.send(Outbound::Text(text));
            }
            Err(e) => tracing::error!("could not serialize a frame: {e}"),
        }
    }

    /// Send an already serialized frame (the queue holds them serialized).
    pub fn send_text(&self, text: String) -> bool {
        self.out.send(Outbound::Text(text)).is_ok()
    }

    pub fn close(&self, code: u16) {
        let _ = self.out.send(Outbound::Close(code));
    }
}

/// Why a registration did not produce a session: the close code (and a protocol error first).
struct Refused {
    code: u16,
    error: Option<String>,
}

impl Refused {
    fn close(code: u16) -> Self {
        Self { code, error: None }
    }
    fn explained(code: u16, error: impl Into<String>) -> Self {
        Self {
            code,
            error: Some(error.into()),
        }
    }
}

/// The identity a `REGISTER` won.
pub struct Registered {
    pub agent: i64,
    pub caller: i64,
    pub epoch: i64,
    pub session_id: Option<String>,
    /// This connection's channel in the caller group, and what arrives on it.
    pub mirror: Mirror,
}

/// The caller group's channel of one connection (`register_caller`).
pub struct Mirror {
    pub channel: String,
    pub inbox: mpsc::UnboundedReceiver<Value>,
}

/// Channel-layer group membership expires (`group_expiry`, a day); sockets live longer. Joining
/// is idempotent, so it is repeated well inside the window (`GROUP_REFRESH_SECONDS`).
pub const GROUP_REFRESH: Duration = Duration::from_secs(3600);

/// The group carrying the events of work a caller originated (`_caller_group`).
pub fn caller_group(caller: i64) -> String {
    format!("task_caller_{caller}")
}

/// Join the caller group on a fresh channel of this process (`register_caller`).
async fn join_caller_group(ctx: &Context, caller: i64) -> Result<Mirror, kante::KanteError> {
    let (channel, inbox) = ctx.channel_layer.subscribe("specific").await?;
    if let Err(e) = ctx
        .channel_layer
        .group_add(&caller_group(caller), &channel)
        .await
    {
        ctx.channel_layer.unsubscribe(&channel);
        return Err(e);
    }
    Ok(Mirror { channel, inbox })
}

/// Serve one agent socket from its first frame to its close.
pub async fn serve(ctx: Context, socket: WebSocket) {
    let (mut sink, mut stream) = socket.split();
    let (out_tx, mut out_rx) = mpsc::unbounded_channel::<Outbound>();
    let sender = Sender { out: out_tx };

    let writer = tokio::spawn(async move {
        while let Some(outbound) = out_rx.recv().await {
            match outbound {
                Outbound::Text(text) => {
                    if sink.send(Message::Text(text.into())).await.is_err() {
                        break;
                    }
                }
                Outbound::Close(code) => {
                    let _ = sink
                        .send(Message::Close(Some(CloseFrame {
                            code,
                            reason: "".into(),
                        })))
                        .await;
                    break;
                }
            }
        }
    });

    let connection_id = uuid::Uuid::new_v4().to_string();
    let registered = match first_frame(&ctx, &mut stream, &sender, &connection_id).await {
        Ok(registered) => registered,
        Err(refused) => {
            if let Some(error) = refused.error {
                sender.send(ToAgent::ProtocolError { error });
            }
            sender.close(refused.code);
            let _ = writer.await;
            return;
        }
    };

    run_session(&ctx, registered, &connection_id, &mut stream, &sender).await;
    drop(sender);
    let _ = writer.await;
}

/// The first frame: parse it, and register if it is a `REGISTER`.
async fn first_frame(
    ctx: &Context,
    stream: &mut futures::stream::SplitStream<WebSocket>,
    sender: &Sender,
    connection_id: &str,
) -> Result<Registered, Refused> {
    let frame = loop {
        match stream.next().await {
            Some(Ok(Message::Text(text))) => break text.to_string(),
            Some(Ok(Message::Binary(_))) => {
                return Err(Refused::close(
                    codes::FROM_AGENT_MESSAGE_IS_NOT_VALID_JSON_CODE,
                ))
            }
            Some(Ok(Message::Ping(_) | Message::Pong(_))) => continue,
            _ => {
                return Err(Refused::close(
                    codes::FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE,
                ))
            }
        }
    };
    let frame = parse(&frame)?;
    let FromAgent::Register {
        token,
        force,
        session_id,
        declaration,
    } = frame.message
    else {
        return Err(Refused::close(
            codes::FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE,
        ));
    };
    register(
        ctx,
        sender,
        connection_id,
        &token,
        force,
        session_id,
        &declaration,
    )
    .await
}

/// Parse a frame, refusing what is not JSON (3002) or not a frame (3003, with the reason).
fn parse(text: &str) -> Result<AgentFrame, Refused> {
    let value: Value = serde_json::from_str(text)
        .map_err(|_| Refused::close(codes::FROM_AGENT_MESSAGE_IS_NOT_VALID_JSON_CODE))?;
    serde_json::from_value::<AgentFrame>(value).map_err(|e| {
        Refused::explained(
            codes::FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE,
            e.to_string(),
        )
    })
}

/// Authenticate, gate and claim, in the Python order (`on_register`): token → blocked →
/// declaration → caller → lease → displacement → `INIT`.
async fn register(
    ctx: &Context,
    sender: &Sender,
    connection_id: &str,
    token: &str,
    force: bool,
    session_id: Option<String>,
    declaration: &crate::messages::RegisterDeclaration,
) -> Result<Registered, Refused> {
    let refuse = |e: &dyn std::fmt::Display| {
        tracing::warn!("registration refused: {e}");
        Refused::close(codes::FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE)
    };
    let decoded = authentikate::authenticate_token(&ctx.verifier, token)
        .await
        .map_err(|e| refuse(&e))?;
    let identity = authentikate::expand::expand_token_context(&ctx.db, &decoded)
        .await
        .map_err(|e| refuse(&e))?;
    let agent = registration::ensure_agent(
        &ctx.db,
        identity.client,
        identity.user,
        identity.organization,
    )
    .await
    .map_err(|e| refuse(&e))?;

    let (blocked, hash): (bool, String) =
        sqlx::query_as("SELECT blocked, hash FROM facade_agent WHERE id = $1")
            .bind(agent)
            .fetch_one(&ctx.db)
            .await
            .map_err(|e| refuse(&e))?;
    if blocked {
        return Err(Refused::close(codes::AGENT_IS_BLOCKED_CODE));
    }

    if declaration.declares() && declaration.hash.as_deref() != Some(hash.as_str()) {
        return Err(Refused::explained(
            codes::AGENT_REGISTRATION_REJECTED_CODE,
            "Registration refused: this server does not reconcile declarations yet",
        ));
    }

    let caller = caller_ops::get_or_create_caller_id(&ctx.db, agent)
        .await
        .map_err(|e| refuse(&e))?;

    let claim = leases::on_agent_connected(
        &ctx.db,
        &ctx.settings,
        agent,
        connection_id,
        session_id.as_deref(),
        force,
    )
    .await
    .map_err(|e| refuse(&e))?;
    let Some(epoch) = claim.epoch.filter(|_| claim.claimed) else {
        return Err(Refused::explained(
            codes::AGENT_ALREADY_CONNECTED_CODE,
            "Another connection is already registered for this agent. Reconnect with force to take over.",
        ));
    };
    if claim.displaced_incumbent {
        ctx.connections.kick_others(agent, connection_id);
    }

    // Before INIT: an agent assigns dependent work, and the first event of it must find us.
    let mirror = match join_caller_group(ctx, caller).await {
        Ok(mirror) => mirror,
        Err(e) => {
            if let Err(e) = leases::release_lease(&ctx.db, agent, connection_id).await {
                tracing::error!(agent, "releasing the lease failed: {e}");
            }
            return Err(refuse(&e));
        }
    };

    sender.send(ToAgent::Init {
        agent: agent.to_string(),
        inquiries: claim
            .inquiries
            .iter()
            .map(|task| Inquiry {
                task: task.to_string(),
            })
            .collect(),
        hash: Some(hash).filter(|hash| !hash.is_empty()),
        diagnostics: vec![],
    });
    Ok(Registered {
        agent,
        caller,
        epoch,
        session_id,
        mirror,
    })
}

/// The pending heartbeat: resolved by the read loop when the answer arrives.
type HeartbeatWaiter = Arc<Mutex<Option<oneshot::Sender<()>>>>;

async fn run_session(
    ctx: &Context,
    registered: Registered,
    connection_id: &str,
    stream: &mut futures::stream::SplitStream<WebSocket>,
    sender: &Sender,
) {
    let agent = registered.agent;
    let (control_tx, mut control_rx) = mpsc::unbounded_channel();
    ctx.connections.join(agent, connection_id, control_tx);

    let waiter: HeartbeatWaiter = Arc::default();
    let drain = tokio::spawn(drain(ctx.clone(), agent, registered.epoch, sender.clone()));
    let heartbeat = tokio::spawn(heartbeat(
        ctx.clone(),
        agent,
        registered.epoch,
        sender.clone(),
        waiter.clone(),
        drain.abort_handle(),
    ));
    let mirror_channel = registered.mirror.channel.clone();
    let mirror = tokio::spawn(mirror(
        ctx.clone(),
        registered.caller,
        registered.mirror,
        sender.clone(),
    ));
    let (work_tx, work_rx) = mpsc::unbounded_channel::<AgentFrame>();
    let worker = tokio::spawn(work(
        ctx.clone(),
        agent,
        registered.session_id.clone(),
        sender.clone(),
        work_rx,
    ));

    loop {
        tokio::select! {
            frame = stream.next() => {
                let text = match frame {
                    Some(Ok(Message::Text(text))) => text.to_string(),
                    Some(Ok(Message::Ping(_) | Message::Pong(_))) => continue,
                    Some(Ok(Message::Binary(_))) => {
                        sender.close(codes::FROM_AGENT_MESSAGE_IS_NOT_VALID_JSON_CODE);
                        break;
                    }
                    _ => break,
                };
                match parse(&text).map(|frame| (matches!(frame.message, FromAgent::HeartbeatAnswer {}), frame)) {
                    Ok((true, _)) => {
                        match waiter.lock().expect("heartbeat lock").take() {
                            Some(answered) => { let _ = answered.send(()); }
                            None => tracing::warn!(agent, "received a heartbeat answer nobody waited for"),
                        }
                    }
                    Ok((false, frame)) if matches!(frame.message, FromAgent::Register { .. }) => {
                        sender.close(codes::FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE);
                        break;
                    }
                    Ok((false, frame)) => { let _ = work_tx.send(frame); }
                    Err(refused) => {
                        if let Some(error) = refused.error {
                            sender.send(ToAgent::ProtocolError { error });
                        }
                        sender.close(refused.code);
                        break;
                    }
                }
            }
            Some(Control::Displace) = control_rx.recv() => {
                drain.abort();
                sender.close(codes::AGENT_REPLACED_CODE);
                break;
            }
        }
    }

    // Stop executing first, then tear down: the drain must not deliver another frame.
    drain.abort();
    heartbeat.abort();
    mirror.abort();
    drop(work_tx);
    let _ = worker.await;
    leave_caller_group(ctx, registered.caller, &mirror_channel).await;
    ctx.connections.leave(agent, connection_id);
    if let Err(e) = on_agent_disconnected(ctx, agent, connection_id).await {
        tracing::error!(agent, "releasing the lease failed: {e}");
    }
}

/// JOURNAL_ACK debounce: acknowledge after this many newly persisted entries, or this long after
/// the first unacknowledged one, whichever comes first. A terminal report is acked at once.
pub const JOURNAL_ACK_EVERY: u32 = 50;
pub const JOURNAL_ACK_DELAY: Duration = Duration::from_millis(200);

/// Route the agent's frames in order, send their replies, and acknowledge the numbered ones
/// (`dispatch`, `on_numbered_handled`). A frame the router fails on closes the connection, as
/// the Python consumer does.
async fn work(
    ctx: Context,
    agent: i64,
    session_id: Option<String>,
    sender: Sender,
    mut frames: mpsc::UnboundedReceiver<AgentFrame>,
) {
    let mut unacked: HashMap<String, u32> = HashMap::new();
    let mut deadline: Option<tokio::time::Instant> = None;
    loop {
        let next = match deadline {
            Some(at) => tokio::select! {
                frame = frames.recv() => frame,
                _ = tokio::time::sleep_until(at) => {
                    deadline = None;
                    flush_journal_acks(&ctx, agent, &sender, &mut unacked).await;
                    continue;
                }
            },
            None => frames.recv().await,
        };
        let Some(frame) = next else {
            flush_journal_acks(&ctx, agent, &sender, &mut unacked).await;
            return;
        };
        match message_router::route(&ctx, agent, &frame, session_id.as_deref()).await {
            Ok(Some(reply)) => {
                let replay = match &reply {
                    ToAgent::AssignResponse {
                        created: false,
                        task: Some(task),
                        ..
                    } => Some(task.clone()),
                    _ => None,
                };
                sender.send(reply);
                if let Some(task) = replay {
                    replay_child_events(&ctx, &task, &sender).await;
                }
            }
            Ok(None) => {}
            Err(e) => {
                tracing::error!(agent, "error handling agent message: {e}");
                sender.close(codes::FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE);
                return;
            }
        }
        if !positions::is_numbered(&frame) {
            continue;
        }
        let Some(journal_session) = frame.journal_session.clone() else {
            continue;
        };
        let count = unacked.entry(journal_session).or_default();
        *count += 1;
        if frame.message.is_terminal() || *count >= JOURNAL_ACK_EVERY {
            deadline = None;
            flush_journal_acks(&ctx, agent, &sender, &mut unacked).await;
        } else if deadline.is_none() {
            deadline = Some(tokio::time::Instant::now() + JOURNAL_ACK_DELAY);
        }
    }
}

/// One cumulative JOURNAL_ACK per session with handled frames: its watermark, read from the
/// session row, since another connection may have moved it further (`flush_journal_acks`).
async fn flush_journal_acks(
    ctx: &Context,
    agent: i64,
    sender: &Sender,
    unacked: &mut HashMap<String, u32>,
) {
    for (journal_session, count) in unacked.iter_mut() {
        if *count == 0 {
            continue;
        }
        *count = 0;
        match positions::projected_position(&ctx.db, agent, journal_session).await {
            Ok(0) => {}
            Ok(pos) => sender.send(ToAgent::JournalAck {
                journal_session: journal_session.clone(),
                pos: pos as u64,
            }),
            Err(e) => tracing::error!(agent, "sending a JOURNAL_ACK failed: {e}"),
        }
    }
}

/// The lease is released if it is still ours (`on_agent_disconnected`); then its probes fail at
/// once (hover-grade work has no grace window). Tasks in flight are the reconcile sweep's, after
/// the grace window.
async fn on_agent_disconnected(
    ctx: &Context,
    agent: i64,
    connection_id: &str,
) -> Result<(), sqlx::Error> {
    if !leases::release_lease(&ctx.db, agent, connection_id).await? {
        return Ok(());
    }
    if let Err(e) = probes::persist::fail_all_for_agent(ctx, agent).await {
        tracing::error!(
            agent,
            "failing the probes of a disconnected agent failed: {e}"
        );
    }
    Ok(())
}

/// Send the events a child already has to the caller that asked for it again
/// (`replay_child_events`): a workflow resuming finds the child it made before
/// (`created = false`), whose events went to the previous process. They follow the response as
/// the same mirrors, with the same `seq`; the caller drops what it already has.
async fn replay_child_events(ctx: &Context, task: &str, sender: &Sender) {
    let Ok(task_id) = task.parse::<i64>() else {
        return;
    };
    let events: Vec<EventRow> = match sqlx::query_as(
        "SELECT id, kind, message, progress, returns, level, value FROM facade_taskevent
          WHERE task_id = $1 ORDER BY id",
    )
    .bind(task_id)
    .fetch_all(&ctx.db)
    .await
    {
        Ok(events) => events,
        Err(e) => {
            tracing::error!(task, "replaying a child's events failed: {e}");
            return;
        }
    };
    for row in events {
        let event = EventLike {
            id: row.id as u64,
            task: task.to_owned(),
            kind: row.kind,
            message: row.message,
            progress: row.progress.map(i64::from),
            returns: row.returns,
            level: row.level,
            value: row.value,
        };
        if let Some(mirror) = build_execution_event(&event) {
            sender.send(mirror);
        }
    }
}

#[derive(sqlx::FromRow)]
struct EventRow {
    id: i64,
    kind: String,
    message: Option<String>,
    progress: Option<i32>,
    returns: Option<Value>,
    level: Option<String>,
    value: Option<Value>,
}

/// Forward what arrives in the caller group (`channel_TaskEventCreatedEvent`,
/// `channel_probe_event_broadcast`), and re-join it hourly. Only a task event's `event` branch is
/// forwarded: a `create` is covered by the `ASSIGN_RESPONSE`. Best effort, like every mirror.
async fn mirror(ctx: Context, caller: i64, mirror: Mirror, sender: Sender) {
    let Mirror { channel, mut inbox } = mirror;
    let mut refresh =
        tokio::time::interval_at(tokio::time::Instant::now() + GROUP_REFRESH, GROUP_REFRESH);
    loop {
        tokio::select! {
            message = inbox.recv() => {
                let Some(message) = message else { return };
                let event = if let Some(payload) = kante::channel::payload_of(&message, channels::TASK_EVENT) {
                    payload.get("event").filter(|e| !e.is_null()).and_then(EventLike::from_payload)
                } else if let Some(payload) = kante::channel::payload_of(&message, channels::PROBE_EVENT) {
                    EventLike::from_probe_payload(payload)
                } else {
                    None
                };
                if let Some(mirror) = event.as_ref().and_then(build_execution_event) {
                    sender.send(mirror);
                }
            }
            _ = refresh.tick() => {
                if let Err(e) = ctx.channel_layer.group_add(&caller_group(caller), &channel).await {
                    tracing::error!(caller, "refreshing the caller group failed: {e}");
                }
            }
        }
    }
}

/// Leave the caller group on disconnect.
async fn leave_caller_group(ctx: &Context, caller: i64, channel: &str) {
    if let Err(e) = ctx
        .channel_layer
        .group_discard(&caller_group(caller), channel)
        .await
    {
        tracing::warn!(caller, "leaving the caller group failed: {e}");
    }
    ctx.channel_layer.unsubscribe(channel);
}

/// Ping, wait for the answer, renew the lease. No answer: close `HEARTBEAT_NOT_RESPONDED`; a
/// renewal that finds the epoch moved: close `AGENT_REPLACED`. Either way, stop executing first.
async fn heartbeat(
    ctx: Context,
    agent: i64,
    epoch: i64,
    sender: Sender,
    waiter: HeartbeatWaiter,
    drain: tokio::task::AbortHandle,
) {
    loop {
        tokio::time::sleep(ctx.settings.agent_heartbeat_interval).await;
        let (answered_tx, answered_rx) = oneshot::channel();
        *waiter.lock().expect("heartbeat lock") = Some(answered_tx);
        sender.send(ToAgent::Heartbeat {});
        if tokio::time::timeout(ctx.settings.agent_heartbeat_response_timeout, answered_rx)
            .await
            .map(|r| r.is_err())
            .unwrap_or(true)
        {
            tracing::error!(agent, "timeout on client for heartbeat");
            drain.abort();
            sender.close(codes::HEARTBEAT_NOT_RESPONDED_CODE);
            return;
        }
        match leases::renew_agent_lease(&ctx.db, agent, epoch).await {
            Ok(true) => {}
            Ok(false) => {
                tracing::warn!(
                    agent,
                    epoch,
                    "lost the lease: closing the displaced connection"
                );
                drain.abort();
                sender.close(codes::AGENT_REPLACED_CODE);
                return;
            }
            Err(e) => tracing::error!(agent, "renewing the lease failed: {e}"),
        }
    }
}

/// Whether a queued frame is an `ASSIGN` for a task the server already closed (`_is_stale_assign`).
/// Probe Assigns carry no task row and are never fenced; anything unparseable is delivered.
async fn is_stale_assign(ctx: &Context, frame: &str) -> bool {
    if !frame.contains("\"ASSIGN\"") {
        return false;
    }
    let Ok(raw) = serde_json::from_str::<Value>(frame) else {
        return false;
    };
    if raw.get("type").and_then(Value::as_str) != Some("ASSIGN")
        || raw.get("probe").and_then(Value::as_bool) == Some(true)
    {
        return false;
    }
    let task = match raw.get("task") {
        Some(Value::String(task)) => task.clone(),
        Some(Value::Number(task)) => task.to_string(),
        _ => return false,
    };
    !reports::is_task_open(ctx, &task).await
}

/// Deliver the agent's queue (`listen_for_tasks`): recover what a previous holder popped but
/// never acked, then pop, check the lease, drop stale Assigns, send, ack. A queue failure is
/// survived with a backoff; a socket that cannot be written closes so the agent reconnects.
async fn drain(ctx: Context, agent: i64, epoch: i64, sender: Sender) {
    let mut backoff = Duration::from_millis(500);
    loop {
        let mut queue = match AgentQueue::open(&ctx.redis_client, &ctx.settings, agent).await {
            Ok(queue) => queue,
            Err(e) => {
                tracing::error!(agent, "task queue unavailable: {e}; retrying");
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(Duration::from_secs(5));
                continue;
            }
        };
        match drain_once(&ctx, agent, epoch, &sender, &mut queue).await {
            Ok(()) => return,
            Err(e) => {
                tracing::error!(agent, "task queue failed: {e}; retrying");
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(Duration::from_secs(5));
            }
        }
    }
}

async fn drain_once(
    ctx: &Context,
    agent: i64,
    epoch: i64,
    sender: &Sender,
    queue: &mut AgentQueue,
) -> Result<(), anyhow::Error> {
    let recovered = queue.recover().await?;
    if recovered > 0 {
        tracing::warn!(agent, recovered, "recovered undelivered messages");
    }
    loop {
        let Some(frame) = queue.pop().await? else {
            // Idle, so nothing of ours is in flight: whatever is there was stranded by someone else.
            queue.recover().await?;
            continue;
        };
        if !leases::holds_lease(&ctx.db, agent, epoch).await? {
            tracing::warn!(
                agent,
                epoch,
                "lost the lease: returning an undelivered frame and closing"
            );
            queue.requeue(&frame).await?;
            sender.close(codes::AGENT_REPLACED_CODE);
            return Ok(());
        }
        if is_stale_assign(ctx, &frame).await {
            tracing::info!(
                agent,
                "dropping a queued Assign for an already finalized task"
            );
            queue.ack(&frame).await?;
            continue;
        }
        if !sender.send_text(frame.clone()) {
            sender.close(codes::AGENT_TRANSPORT_FAILED_CODE);
            return Ok(());
        }
        queue.ack(&frame).await?;
    }
}
