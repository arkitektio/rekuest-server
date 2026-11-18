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
use std::time::{Duration, Instant};

use axum::extract::ws::{CloseFrame, Message, WebSocket};
use futures::{SinkExt, StreamExt};
use serde_json::Value;
use tokio::sync::{mpsc, oneshot};

use crate::caller_events::{build_execution_event, mirror_of_channel_message, EventLike};
use crate::codes;
use crate::consumers::agent_queue::{self, Lane};
use crate::consumers::connections::Control;
use crate::context::Context;
use crate::message_router;
use crate::messages::{
    AgentFrame, Diagnostic, FromAgent, ToAgent, ToAgentFrame, FEATURE_PROTOCOL_2,
};
use crate::persist::positions;
use crate::persist::{caller_ops, leases, reconcile};
use crate::probes;
use crate::registration;
use rekuest_core::inputs::ImplementAgentInputModel;

/// What the writer task sends: a frame, or the close that ends the connection.
enum Outbound {
    Text(String),
    /// A frame whose sender wants to know whether it was written to the socket.
    Confirmed(String, oneshot::Sender<bool>),
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
            pos: None,
            message,
        };
        match serde_json::to_string(&frame) {
            Ok(text) => {
                let _ = self.out.send(Outbound::Text(text));
            }
            Err(e) => tracing::error!("could not serialize a frame: {e}"),
        }
    }

    /// Send an already serialized frame (the queue holds them serialized) and wait until it is
    /// written to the socket. `false` when it was not: the connection is gone.
    ///
    /// Handing a frame to the writer is not delivering it: what the writer still holds when a
    /// write fails is dropped with it. Whoever must not lose the frame (the drain, before it
    /// forgets the frame in redis) waits for the write.
    pub async fn send_text(&self, text: String) -> bool {
        let (written, confirmed) = oneshot::channel();
        if self.out.send(Outbound::Confirmed(text, written)).is_err() {
            return false;
        }
        confirmed.await.unwrap_or(false)
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
    pub session_id: Option<String>,
    /// The highest command position the agent's process has handled.
    pub commands_seen: u64,
    /// The tasks the agent had under way when its last connection ended, for an agent that
    /// did not say what it holds: asked about, now that it is back.
    pub inquiries: Vec<i64>,
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
                Outbound::Confirmed(text, written) => {
                    let sent = sink.send(Message::Text(text.into())).await.is_ok();
                    let _ = written.send(sent);
                    if !sent {
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
        features,
        nonce,
        commands_seen,
        sessions,
        tasks,
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
        &features,
        Handshake {
            nonce,
            commands_seen,
            sessions,
            // Probes are not tasks the server keeps: only what it could have under way.
            tasks: tasks.map(|tasks| tasks.iter().filter_map(|task| task.parse().ok()).collect()),
        },
        &declaration,
    )
    .await
}

/// What a `REGISTER` says about where the agent stands.
struct Handshake {
    nonce: Option<String>,
    commands_seen: u64,
    sessions: Vec<String>,
    /// Every task the process holds, in whatever state (waiting, running, ended with its
    /// report still to come). None from an agent that does not say: it is asked instead.
    tasks: Option<Vec<i64>>,
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
#[allow(clippy::too_many_arguments)]
async fn register(
    ctx: &Context,
    sender: &Sender,
    connection_id: &str,
    token: &str,
    force: bool,
    session_id: Option<String>,
    features: &[String],
    handshake: Handshake,
    declaration: &crate::messages::RegisterDeclaration,
) -> Result<Registered, Refused> {
    let refuse = |e: &dyn std::fmt::Display| {
        tracing::warn!("registration refused: {e}");
        Refused::close(codes::FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE)
    };
    // The server's own trouble (its database, its redis, the issuer's keys out of reach) is not
    // a refusal: the agent is told to come back, never that it was turned away.
    let fail = |e: &dyn std::fmt::Display| {
        tracing::error!("registration failed on the server's side: {e}");
        Refused::close(codes::AGENT_TRANSPORT_FAILED_CODE)
    };
    let denied = |e: &authentikate::AuthentikateError| match e.is_permission_denied() {
        true => refuse(e),
        false => fail(e),
    };
    let decoded = authentikate::authenticate_token(&ctx.verifier, token)
        .await
        .map_err(|e| denied(&e))?;
    // Before anything is written: an agent that names no spec is older than this server reads.
    if declaration
        .spec_version
        .as_deref()
        .is_none_or(|version| version.trim().is_empty())
    {
        return Err(Refused::explained(
            codes::AGENT_REGISTRATION_REJECTED_CODE,
            "Registration refused: this agent names no spec_version, so it is older than this \
             server accepts. Update the arkitekt it runs."
                .to_owned(),
        ));
    }
    // Nor one on another protocol: what it could lose, this server no longer makes up for.
    if !features.iter().any(|feature| feature == FEATURE_PROTOCOL_2) {
        return Err(Refused::explained(
            codes::AGENT_REGISTRATION_REJECTED_CODE,
            "Registration refused: this server speaks agent protocol 2 (commands are numbered and \
             acknowledged, a gap in the journal is refused), and this agent does not say it does \
             (`protocol_2` in its features). Update the arkitekt it runs."
                .to_owned(),
        ));
    }
    let identity = authentikate::expand::expand_token_context(&ctx.db, &decoded)
        .await
        .map_err(|e| denied(&e))?;
    let agent = registration::ensure_agent(
        &ctx.db,
        identity.client,
        identity.user,
        identity.organization,
    )
    .await
    .map_err(|e| fail(&e))?;

    let (blocked, hash): (bool, String) =
        sqlx::query_as("SELECT blocked, hash FROM facade_agent WHERE id = $1")
            .bind(agent)
            .fetch_one(&ctx.db)
            .await
            .map_err(|e| fail(&e))?;
    if blocked {
        return Err(Refused::close(codes::AGENT_IS_BLOCKED_CODE));
    }

    // The supervisor rides every REGISTER, outside the declaration: a hash-equal reconnect
    // skips the reconciliation below, and must still say who supervises it now.
    registration::set_supervisor(&ctx.db, agent, declaration.supervisor.as_deref())
        .await
        .map_err(|e| fail(&e))?;
    // So does its hardware: it may have come back on another machine.
    registration::set_hardware(&ctx.db, agent, declaration.hardware.as_ref())
        .await
        .map_err(|e| fail(&e))?;
    // So do its features: what it says it implements now, whatever it said before.
    registration::set_features(&ctx.db, agent, features)
        .await
        .map_err(|e| fail(&e))?;
    // And the spec it runs: an upgrade may have changed nothing it declares.
    registration::set_spec_version(&ctx.db, agent, declaration.spec_version.as_deref())
        .await
        .map_err(|e| fail(&e))?;
    // And what its token grants: a process may come back with a narrower one.
    registration::set_granted_scopes(&ctx.db, agent, &decoded.scopes())
        .await
        .map_err(|e| fail(&e))?;

    // Registering is implementing: a declaration whose hash the agent does not hold yet is
    // reconciled in one transaction before the lease is claimed, so a refused one strands no
    // lease. Its non-fatal findings ride back on INIT.
    let mut hash = hash;
    let mut diagnostics = vec![];
    if declaration.declares() && declaration.hash.as_deref() != Some(hash.as_str()) {
        let implemented = implement(ctx, agent, declaration).await?;
        hash = implemented.hash;
        diagnostics = implemented
            .diagnostics
            .into_iter()
            .map(|d| Diagnostic {
                level: Some(d.level.to_owned()),
                code: d.code.to_owned(),
                message: d.message,
                path: d.path,
            })
            .collect();
        implemented.on_commit.publish(ctx).await;
    }

    let caller = caller_ops::get_or_create_caller_id(&ctx.db, agent)
        .await
        .map_err(|e| fail(&e))?;

    let claim = leases::on_agent_connected(
        &ctx.db,
        &ctx.settings,
        agent,
        connection_id,
        session_id.as_deref(),
        force,
    )
    .await
    .map_err(|e| fail(&e))?;
    if !claim.claimed {
        return Err(Refused::explained(
            codes::AGENT_ALREADY_CONNECTED_CODE,
            "Another connection is already registered for this agent. Reconnect with force to take over.",
        ));
    }
    if claim.displaced_incumbent {
        ctx.connections.kick_others(agent, connection_id);
    }
    // A fresh process took over. What its predecessor was owed and only it could use (answers,
    // the events of what it assigned, what steered the tasks it had) is not for this one.
    if leases::fresh_process(claim.prior_session.as_deref(), session_id.as_deref()) {
        match agent_queue::trim_for_new_process(&ctx.db, agent).await {
            Ok(0) => {}
            Ok(gone) => tracing::info!(
                agent,
                gone,
                "a new process took over: dropped the commands only its predecessor could use"
            ),
            Err(e) => tracing::error!(agent, "dropping a previous process's commands failed: {e}"),
        }
    }
    // Its predecessor's in-flight work is orphaned. It ends LOST (a workflow is sent again,
    // among the commands the drain is about to deliver) before `INIT`.
    if let Err(e) = reconcile::fail_and_cascade_inflight(ctx, &claim.orphaned).await {
        tracing::error!(
            agent,
            "failing the orphaned work of a previous process failed: {e}"
        );
    }

    // The same process came back and says what it holds. What it had under way and does not
    // name, it lost: nobody else can have it, so that is how it ended, here and at once.
    // One that does not say is asked task by task, once it is listening (`ask_about`).
    let mut inquiries = claim.inquiries;
    if let Some(held) = &handshake.tasks {
        for task in inquiries.drain(..).filter(|task| !held.contains(task)) {
            let as_claimed = move |row: &crate::persist::transitions::TaskRow| {
                row.agent_id == agent && row.picked_up_at.is_some()
            };
            if let Err(e) = crate::persist::transitions::finalize_lost(
                ctx,
                task,
                "Its agent came back without it.",
                true,
                Some(&as_claimed),
                false,
            )
            .await
            {
                tracing::error!(agent, task, "ending a task its agent lost failed: {e}");
            }
        }
    }

    // Before INIT: an agent assigns dependent work, and the first event of it must find us.
    let mirror = match join_caller_group(ctx, caller).await {
        Ok(mirror) => mirror,
        Err(e) => {
            if let Err(e) = leases::release_lease(&ctx.db, agent, connection_id).await {
                tracing::error!(agent, "releasing the lease failed: {e}");
            }
            return Err(fail(&e));
        }
    };

    // `connected` changed: the agents feed shows it without waiting for anything else to.
    crate::signals::agent_saved(ctx, agent, false).await;

    let positions = match positions::projected_positions(&ctx.db, agent, &handshake.sessions).await
    {
        Ok(positions) => positions,
        Err(e) => {
            if let Err(e) = leases::release_lease(&ctx.db, agent, connection_id).await {
                tracing::error!(agent, "releasing the lease failed: {e}");
            }
            return Err(fail(&e));
        }
    };
    sender.send(ToAgent::Init {
        nonce: handshake.nonce,
        positions,
        agent: agent.to_string(),
        hash: Some(hash).filter(|hash| !hash.is_empty()),
        diagnostics,
    });
    Ok(Registered {
        agent,
        caller,
        session_id,
        commands_seen: handshake.commands_seen,
        inquiries,
        mirror,
    })
}

/// Reconcile a `REGISTER`'s declaration (`on_agent_implement`). A declaration the models refuse
/// is a frame that does not match the schema (3003, as Python's parse of `Register` refuses it);
/// a refused registration is a `ProtocolError` and 4006.
async fn implement(
    ctx: &Context,
    agent: i64,
    declaration: &crate::messages::RegisterDeclaration,
) -> Result<registration::Implemented, Refused> {
    let schema = |e: &dyn std::fmt::Display| {
        Refused::explained(
            codes::FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE,
            e.to_string(),
        )
    };
    let mut payload: ImplementAgentInputModel = serde_json::to_value(declaration)
        .and_then(serde_json::from_value)
        .map_err(|e| schema(&e))?;
    payload.validate().map_err(|e| schema(&e.0))?;

    let rejected = |e: &dyn std::fmt::Display| {
        tracing::warn!(agent, "registration refused: {e}");
        Refused::explained(
            codes::AGENT_REGISTRATION_REJECTED_CODE,
            format!("Registration refused: {e}"),
        )
    };
    // Not the declaration's fault: the agent comes back with it.
    let failed = |e: &sqlx::Error| {
        tracing::error!(agent, "registration failed on the server's side: {e}");
        Refused::close(codes::AGENT_TRANSPORT_FAILED_CODE)
    };
    let mut tx = ctx.db.begin().await.map_err(|e| failed(&e))?;
    let implemented = registration::implement_agent(&mut tx, agent, &payload)
        .await
        .map_err(|e| rejected(&e))?;
    tx.commit().await.map_err(|e| failed(&e))?;
    Ok(implemented)
}

/// A connection's turn: held by the worker while it handles a frame and sends the reply, and
/// by the mirror and the drain while they forward one event.
type Turn = Arc<tokio::sync::Mutex<()>>;

/// The pending heartbeat: resolved by the read loop when the agent is heard from.
type HeartbeatWaiter = Arc<Mutex<Option<oneshot::Sender<()>>>>;

/// When the agent last sent a frame, of any kind.
type Heard = Arc<Mutex<Instant>>;

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
    let heard: Heard = Arc::new(Mutex::new(Instant::now()));
    // One turn per connection, as a Channels consumer handles one message at a time: what a
    // request publishes (the QUEUED of the child it created, the CANCELLING of the task it
    // cancelled) reaches this caller only after the request's own response, whether it comes
    // through the agent's queue (a child's events) or the caller group (everything else).
    let turn = Turn::default();
    let drain = tokio::spawn(drain(
        ctx.clone(),
        agent,
        connection_id.to_owned(),
        sender.clone(),
        turn.clone(),
        registered.commands_seen,
    ));
    let heartbeat = tokio::spawn(heartbeat(
        ctx.clone(),
        agent,
        connection_id.to_owned(),
        sender.clone(),
        waiter.clone(),
        heard.clone(),
        drain.abort_handle(),
    ));
    let mirror_channel = registered.mirror.channel.clone();
    let mirror = tokio::spawn(mirror(
        ctx.clone(),
        registered.caller,
        registered.mirror,
        sender.clone(),
        turn.clone(),
    ));
    let (work_tx, work_rx) = mpsc::unbounded_channel::<AgentFrame>();
    let worker = tokio::spawn(work(
        ctx.clone(),
        agent,
        registered.caller,
        registered.session_id.clone(),
        connection_id.to_owned(),
        sender.clone(),
        work_rx,
        turn,
    ));
    // The same process came back: is it still on what it had? A task it no longer knows ends
    // LOST (`inquiry::inquire`); the rest goes on.
    let asking = tokio::spawn(ask_about(ctx.clone(), agent, registered.inquiries.clone()));

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
                // Whatever it is, the agent wrote it: that is what a heartbeat asks.
                *heard.lock().expect("heard lock") = Instant::now();
                if let Some(answered) = waiter.lock().expect("heartbeat lock").take() {
                    let _ = answered.send(());
                }
                match parse(&text).map(|frame| (matches!(frame.message, FromAgent::HeartbeatAnswer {}), frame)) {
                    Ok((true, _)) => {}
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
    asking.abort();
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

/// How many frames the worker takes at once when they are already waiting: a run of light
/// frames among them is one transaction (`message_router::route_light`).
pub const BURST: usize = 128;

/// What the worker of one connection keeps between frames.
struct Worker {
    ctx: Context,
    agent: i64,
    caller: i64,
    session_id: Option<String>,
    connection_id: String,
    sender: Sender,
    turn: Turn,
    /// Per session, the frames handled since its last JOURNAL_ACK.
    unacked: HashMap<String, u32>,
    /// Per session, the position last asked for again: one JOURNAL_NACK per gap, not one per
    /// frame that arrives behind it.
    nacked: HashMap<String, u64>,
    deadline: Option<tokio::time::Instant>,
}

/// Route the agent's frames in order, send their replies, and acknowledge the numbered ones
/// (`dispatch`, `on_numbered_handled`). A frame the router fails on closes the connection, as
/// the Python consumer does.
#[allow(clippy::too_many_arguments)]
async fn work(
    ctx: Context,
    agent: i64,
    caller: i64,
    session_id: Option<String>,
    connection_id: String,
    sender: Sender,
    mut frames: mpsc::UnboundedReceiver<AgentFrame>,
    turn: Turn,
) {
    let mut worker = Worker {
        ctx,
        agent,
        caller,
        session_id,
        connection_id,
        sender,
        turn,
        unacked: HashMap::new(),
        nacked: HashMap::new(),
        deadline: None,
    };
    loop {
        let next = match worker.deadline {
            Some(at) => tokio::select! {
                frame = frames.recv() => frame,
                _ = tokio::time::sleep_until(at) => {
                    worker.flush().await;
                    continue;
                }
            },
            None => frames.recv().await,
        };
        let Some(first) = next else {
            worker.flush().await;
            return;
        };
        // Whatever is already waiting is taken with it.
        let mut burst = vec![first];
        while burst.len() < BURST {
            match frames.try_recv() {
                Ok(frame) => burst.push(frame),
                Err(_) => break,
            }
        }
        let mut at = 0;
        while at < burst.len() {
            let light = burst[at..]
                .iter()
                .take_while(|frame| message_router::is_light(frame))
                .count();
            let went_on = if light > 0 {
                let run: Vec<&AgentFrame> = burst[at..at + light].iter().collect();
                at += light;
                worker.light(&run).await
            } else {
                at += 1;
                worker.one(&burst[at - 1]).await
            };
            if !went_on {
                return;
            }
        }
    }
}

impl Worker {
    /// Close for a frame the router failed on. What the agent sent is at fault only when it was
    /// refused. A database or redis that failed says nothing about the frame: the agent comes
    /// back and sends it again, where the schema code would end it for good.
    fn failed(&self, e: &message_router::RouteError) {
        tracing::error!(agent = self.agent, "error handling agent message: {e}");
        self.sender.close(match e {
            message_router::RouteError::Refused(_) => {
                codes::FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE
            }
            _ => codes::AGENT_TRANSPORT_FAILED_CODE,
        });
    }

    /// A frame arrived ahead of its turn: ask for the position expected, once per gap.
    fn nack(&mut self, journal_session: String, expected: u64) {
        if self.nacked.insert(journal_session.clone(), expected) != Some(expected) {
            self.sender.send(ToAgent::JournalNack {
                journal_session,
                expected,
            });
        }
    }

    /// A numbered frame is handled (projected now, or before): it counts towards the next ack.
    async fn counted(&mut self, frame: &AgentFrame) {
        if !positions::is_numbered(frame) {
            return;
        }
        let Some(journal_session) = frame.journal_session.clone() else {
            return;
        };
        self.nacked.remove(&journal_session);
        let count = self.unacked.entry(journal_session).or_default();
        *count += 1;
        if frame.message.is_terminal() || *count >= JOURNAL_ACK_EVERY {
            self.flush().await;
        } else if self.deadline.is_none() {
            self.deadline = Some(tokio::time::Instant::now() + JOURNAL_ACK_DELAY);
        }
    }

    async fn flush(&mut self) {
        self.deadline = None;
        flush_journal_acks(&self.ctx, self.agent, &self.sender, &mut self.unacked).await;
    }

    /// A run of light frames: one transaction. Whether the connection goes on.
    async fn light(&mut self, run: &[&AgentFrame]) -> bool {
        let routed = match message_router::route_light(&self.ctx, self.agent, run, false).await {
            Ok(routed) => routed,
            Err(e) => {
                self.failed(&e);
                return false;
            }
        };
        for (frame, routed) in run.iter().zip(routed) {
            match routed {
                message_router::Routed::Gap {
                    journal_session,
                    expected,
                } => self.nack(journal_session, expected),
                _ => self.counted(frame).await,
            }
        }
        true
    }

    /// One frame of any other kind. Whether the connection goes on.
    async fn one(&mut self, frame: &AgentFrame) -> bool {
        let (ctx, agent) = (self.ctx.clone(), self.agent);
        match &frame.message {
            FromAgent::CommandAck { through } => {
                // An acknowledgement still waiting here when another connection took the
                // agent over is not acted on: what it covers was given out anew since.
                if !leases::holds_lease(&ctx.db, agent, &self.connection_id)
                    .await
                    .unwrap_or(false)
                {
                    return true;
                }
                // Kept a little longer when this fails: the next ack covers them too.
                if let Err(e) = agent_queue::ack(&ctx.db, agent, *through).await {
                    tracing::error!(agent, "forgetting acknowledged commands failed: {e}")
                }
                return true;
            }
            FromAgent::InquiryAnswer {
                status,
                request: Some(request),
                step,
                reported_at,
                progress,
                message,
                ..
            } => {
                let answer = crate::inquiry::Answer {
                    status: *status,
                    step: *step,
                    reported_at: *reported_at,
                    progress: *progress,
                    message: message.clone(),
                };
                if let Err(e) = crate::inquiry::answered(&ctx, request, &answer).await {
                    tracing::error!(agent, "keeping an inquiry's answer failed: {e}");
                }
                return true;
            }
            FromAgent::InquiryAnswer { .. } => return true, // answers no question
            FromAgent::InquireRequest { task } => {
                // Asking waits for the task's agent: beside the frames, never in their way.
                tokio::spawn(answer_inquiry(
                    ctx.clone(),
                    agent,
                    self.caller,
                    frame.id.clone(),
                    task.clone(),
                    self.sender.clone(),
                ));
                return true;
            }
            FromAgent::JournalSkip {
                session,
                first,
                last,
            } => {
                let skipped = match ctx.db.acquire().await {
                    Ok(mut conn) => {
                        positions::skip_positions(
                            &mut conn,
                            agent,
                            session,
                            *first as i64,
                            *last as i64,
                        )
                        .await
                    }
                    Err(e) => Err(e),
                };
                match skipped {
                    Ok(true) => tracing::error!(
                        agent,
                        journal_session = %session,
                        "the agent lost its reports {first}..={last}: they are gone for good"
                    ),
                    Ok(false) => tracing::warn!(
                        agent,
                        journal_session = %session,
                        "a JOURNAL_SKIP of {first}..={last} does not start at the next position: ignored"
                    ),
                    Err(e) => tracing::error!(agent, "recording a JOURNAL_SKIP failed: {e}"),
                }
                self.nacked.remove(session);
                return true;
            }
            _ => {}
        }
        let turn = self.turn.clone();
        let _turn = turn.lock().await;
        match message_router::route_socket(&ctx, agent, frame, self.session_id.as_deref()).await {
            Ok(Some(ToAgent::JournalNack {
                journal_session,
                expected,
            })) => {
                self.nack(journal_session, expected);
                return true;
            }
            Ok(Some(reply)) => {
                // The answer to a request that carries no position: it has no command to be.
                let replay = match &reply {
                    ToAgent::AssignResponse {
                        created: false,
                        task: Some(task),
                        ..
                    } => Some(task.clone()),
                    _ => None,
                };
                self.sender.send(reply);
                if let Some(task) = replay {
                    replay_child_events(&ctx, &task, &self.sender).await;
                }
            }
            Ok(None) => {}
            Err(e) => {
                self.failed(&e);
                return false;
            }
        }
        drop(_turn);
        self.counted(frame).await;
        true
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
                through: pos as u64,
            }),
            Err(e) => tracing::error!(agent, "sending a JOURNAL_ACK failed: {e}"),
        }
    }
}

/// Ask a returning agent about what it had under way, one task after the other.
async fn ask_about(ctx: Context, agent: i64, tasks: Vec<i64>) {
    for task in tasks {
        match crate::inquiry::inquire(&ctx, &task.to_string()).await {
            Ok(inquiry) => tracing::debug!(agent, task, ?inquiry, "asked about a task"),
            Err(e) => tracing::error!(agent, task, "asking about a task failed: {e}"),
        }
    }
}

/// Answer an agent asking about a task it assigned (`INQUIRE_REQUEST`).
async fn answer_inquiry(
    ctx: Context,
    agent: i64,
    caller: i64,
    request: String,
    task: String,
    sender: Sender,
) {
    let refused = |error: String| ToAgent::InquireResponse {
        request: request.clone(),
        task: task.clone(),
        status: None,
        asked: false,
        detail: None,
        step: None,
        reported_at: None,
        progress: None,
        message: None,
        error: Some(error),
    };
    let response = match crate::inquiry::caller_of(&ctx, &task).await {
        Ok(Some(owner)) if owner == caller => match crate::inquiry::inquire(&ctx, &task).await {
            Ok(inquiry) => {
                // It ended, and whoever assigned it still asks: the end did not reach them.
                // What the task recorded is kept for them again; they drop what they have.
                if inquiry.status == Some(crate::messages::TaskStatus::Ended) {
                    if let Ok(id) = task.parse::<i64>() {
                        match agent_queue::push_events_of(&ctx.db, agent, id).await {
                            Ok(0) => {}
                            Ok(_) => agent_queue::wake(&ctx, agent).await,
                            Err(e) => tracing::error!(
                                agent,
                                task,
                                "keeping an ended task's events for its caller failed: {e}"
                            ),
                        }
                    }
                }
                ToAgent::InquireResponse {
                    request: request.clone(),
                    task: inquiry.task,
                    status: inquiry.status,
                    asked: inquiry.asked,
                    detail: inquiry.detail,
                    step: inquiry.step,
                    reported_at: inquiry.reported_at,
                    progress: inquiry.progress,
                    message: inquiry.message,
                    error: None,
                }
            }
            Err(e) => refused(e.to_string()),
        },
        Ok(_) => refused("Not a task this agent assigned.".into()),
        Err(e) => refused(e.to_string()),
    };
    sender.send(response);
}

/// The lease is released if it is still ours (`on_agent_disconnected`). Its probes fail at
/// once; its in-flight work waits out the grace window, which is the reconcile sweep's, or with
/// no grace is failed right here.
async fn on_agent_disconnected(
    ctx: &Context,
    agent: i64,
    connection_id: &str,
) -> Result<(), sqlx::Error> {
    if !leases::release_lease(&ctx.db, agent, connection_id).await? {
        return Ok(()); // displaced: the new owner is authoritative
    }
    crate::signals::agent_saved(ctx, agent, false).await;
    if let Err(e) = probes::persist::fail_all_for_agent(ctx, agent).await {
        tracing::error!(
            agent,
            "failing the probes of a disconnected agent failed: {e}"
        );
    }
    if ctx.settings.grace.is_zero() {
        reconcile::reconcile_orphaned_executor_work(ctx, agent).await?;
    }
    Ok(())
}

/// Send the events a child already has to the caller that asked for it again
/// (`replay_child_events`): a workflow resuming finds the child it made before
/// (`created = false`), whose events went to the previous process. They follow the response as
/// the same mirrors, naming the same `event`; the caller drops what it already has.
async fn replay_child_events(ctx: &Context, task: &str, sender: &Sender) {
    match child_mirrors(ctx, task).await {
        Ok(mirrors) => mirrors.into_iter().for_each(|mirror| sender.send(mirror)),
        Err(e) => tracing::error!(task, "replaying a child's events failed: {e}"),
    }
}

/// The mirrors of every event a task has so far, in order.
pub async fn child_mirrors(ctx: &Context, task: &str) -> Result<Vec<ToAgent>, sqlx::Error> {
    let Ok(task_id) = task.parse::<i64>() else {
        return Ok(vec![]);
    };
    let events: Vec<EventRow> = sqlx::query_as(
        "SELECT id, kind, message, progress, returns, level, value FROM facade_taskevent
          WHERE task_id = $1 ORDER BY id",
    )
    .bind(task_id)
    .fetch_all(&ctx.db)
    .await?;
    Ok(events
        .into_iter()
        .filter_map(|row| {
            build_execution_event(&EventLike {
                id: row.id as u64,
                task: task.to_owned(),
                kind: row.kind,
                message: row.message,
                progress: row.progress.map(i64::from),
                returns: row.returns,
                level: row.level,
                value: row.value,
            })
        })
        .collect())
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
async fn mirror(ctx: Context, caller: i64, mirror: Mirror, sender: Sender, turn: Turn) {
    let Mirror { channel, mut inbox } = mirror;
    let mut refresh =
        tokio::time::interval_at(tokio::time::Instant::now() + GROUP_REFRESH, GROUP_REFRESH);
    loop {
        tokio::select! {
            message = inbox.recv() => {
                let Some(message) = message else { return };
                tracing::debug!(caller, %message, "caller group message");
                if let Some(mirror) = mirror_of_channel_message(&message) {
                    let _turn = turn.lock().await;
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

/// Ping, wait to hear from the agent, renew the lease. Any frame is an answer: a heartbeat asks
/// whether the agent's loop reads and writes, and a report says so as well as a
/// `HEARTBEAT_ANSWER` does. Nothing heard: close `HEARTBEAT_NOT_RESPONDED`; a renewal that
/// finds the lease gone: close `AGENT_REPLACED`. Either way, stop executing first.
async fn heartbeat(
    ctx: Context,
    agent: i64,
    connection_id: String,
    sender: Sender,
    waiter: HeartbeatWaiter,
    heard: Heard,
    drain: tokio::task::AbortHandle,
) {
    let interval = ctx.settings.agent_heartbeat_interval;
    let patience = ctx.settings.agent_heartbeat_response_timeout;
    loop {
        tokio::time::sleep(interval).await;
        let (answered_tx, answered_rx) = oneshot::channel();
        *waiter.lock().expect("heartbeat lock") = Some(answered_tx);
        sender.send(ToAgent::Heartbeat {});
        let silent = tokio::time::timeout(patience, answered_rx)
            .await
            .map(|r| r.is_err())
            .unwrap_or(true);
        // A frame that finished arriving shortly before the ping counts too (as long before
        // it as the answer may take after it): an agent in the middle of writing a large one
        // cannot answer until it is through.
        if silent && heard.lock().expect("heard lock").elapsed() > patience * 2 {
            tracing::error!(agent, "timeout on client for heartbeat");
            drain.abort();
            sender.close(codes::HEARTBEAT_NOT_RESPONDED_CODE);
            return;
        }
        match leases::renew_agent_lease(&ctx.db, agent, &connection_id).await {
            Ok(true) => {}
            Ok(false) => {
                tracing::warn!(
                    agent,
                    connection_id,
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

/// Stamp a command's position on its serialized frame.
fn at_position(frame: &str, pos: u64) -> String {
    match serde_json::from_str::<Value>(frame) {
        Ok(Value::Object(mut fields)) => {
            fields.insert("pos".to_owned(), Value::from(pos));
            Value::Object(fields).to_string()
        }
        _ => frame.to_owned(), // not ours to number: sent as it is
    }
}

/// Deliver what the server has for the agent (`listen_for_tasks`): its commands after the last
/// one it has seen, each at its position, and what is urgent ahead of them. Nothing is
/// forgotten by being sent: the agent's `COMMAND_ACK` does that (`work`). A database or redis
/// failure is survived with a backoff; a socket that cannot be written closes so the agent
/// reconnects.
async fn drain(
    ctx: Context,
    agent: i64,
    connection_id: String,
    sender: Sender,
    turn: Turn,
    commands_seen: u64,
) {
    let mut backoff = Duration::from_millis(500);
    // Where this connection stands: it outlives whatever has to be opened anew.
    let mut cursor = Cursor {
        sent: commands_seen,
        resumed: false,
        urgent_after: "0".to_owned(),
        woken_after: "0".to_owned(),
        since: None,
    };
    loop {
        match drain_once(&ctx, agent, &connection_id, &sender, &turn, &mut cursor).await {
            Ok(()) => return,
            Err(e) => {
                tracing::error!(
                    agent,
                    "delivering the agent's commands failed: {e}; retrying"
                );
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(Duration::from_secs(5));
            }
        }
    }
}

/// How far a connection has sent: the last command position, and the last urgent frame.
struct Cursor {
    sent: u64,
    /// Whether numbering was brought up to what the agent has seen (once per connection).
    resumed: bool,
    urgent_after: String,
    woken_after: String,
    /// When this connection began to drain, on redis's clock: an order to leave given before
    /// it was for the connection there was then.
    since: Option<u64>,
}

/// How long a connection without redis waits before it looks for commands again.
const WITHOUT_REDIS: Duration = Duration::from_secs(1);

async fn drain_once(
    ctx: &Context,
    agent: i64,
    connection_id: &str,
    sender: &Sender,
    turn: &Turn,
    cursor: &mut Cursor,
) -> Result<(), anyhow::Error> {
    if !cursor.resumed {
        // Numbering never falls behind what the agent has handled, whatever is remembered here.
        agent_queue::resume(&ctx.db, agent, cursor.sent).await?;
        cursor.resumed = true;
    }
    // Commands are in the database: redis only carries what is urgent, and the wake. Without
    // it the commands still go out, a little later.
    let mut lane: Option<Lane> = None;
    let mut idle = false;
    loop {
        if lane.is_none() {
            match Lane::open(&ctx.redis_client, &ctx.settings, agent).await {
                Ok(mut opened) => {
                    if cursor.since.is_none() {
                        cursor.since = opened.now_millis().await.ok();
                    }
                    lane = Some(opened);
                }
                Err(e) => tracing::error!(agent, "the priority lane is unavailable: {e}"),
            }
        }
        let urgent = match lane.as_mut() {
            Some(open) => {
                match open
                    .read(&cursor.urgent_after, &cursor.woken_after, idle)
                    .await
                {
                    Ok((urgent, woken)) => {
                        if let Some(woken) = woken {
                            cursor.woken_after = woken;
                        }
                        urgent
                    }
                    Err(e) => {
                        tracing::error!(agent, "reading the priority lane failed: {e}");
                        lane = None;
                        vec![]
                    }
                }
            }
            None => {
                if idle {
                    tokio::time::sleep(WITHOUT_REDIS).await;
                }
                vec![]
            }
        };
        // Once per round, not once per frame: a connection that lost the lease between this
        // and a send writes into a process that hands a position on once.
        if !leases::holds_lease(&ctx.db, agent, connection_id).await? {
            tracing::warn!(
                agent,
                connection_id,
                "lost the lease: leaving what is unsent to the new holder and closing"
            );
            sender.close(codes::AGENT_REPLACED_CODE);
            return Ok(());
        }
        idle = urgent.is_empty();
        for agent_queue::Urgent { id, frame } in urgent {
            // At most once: forgotten whether or not the socket took it.
            if let Some(open) = lane.as_mut() {
                if let Err(e) = open.forget(&id).await {
                    tracing::error!(agent, "forgetting an urgent frame failed: {e}");
                }
            }
            let stale = agent_queue::is_connection_order(&frame)
                && cursor
                    .since
                    .zip(agent_queue::pushed_at(&id))
                    .is_some_and(|(since, at)| at < since);
            cursor.urgent_after = id;
            if stale {
                tracing::info!(
                    agent,
                    "not sending an order to leave given before this connection"
                );
                continue;
            }
            if !sender.send_text(frame).await {
                sender.close(codes::AGENT_TRANSPORT_FAILED_CODE);
                return Ok(());
            }
        }
        {
            // Not in the middle of a request: its answer takes its place ahead of the events
            // of the child it made, which were written before it.
            let _turn = turn.lock().await;
            agent_queue::number(&ctx.db, agent).await?;
        }
        let commands = agent_queue::read(&ctx.db, agent, cursor.sent).await?;
        idle = idle && commands.is_empty();
        for command in commands {
            match command.frame {
                // Nothing to send at this position (an assign whose task ended meanwhile).
                None => tracing::debug!(agent, pos = command.pos, "a command is passed over"),
                Some(frame) => {
                    if !sender.send_text(at_position(&frame, command.pos)).await {
                        sender.close(codes::AGENT_TRANSPORT_FAILED_CODE);
                        return Ok(());
                    }
                }
            }
            cursor.sent = command.pos;
        }
    }
}
