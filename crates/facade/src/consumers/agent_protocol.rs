//! The agent conversation (`facade/consumers/agent_protocol.py`).
//!
//! Before `REGISTER`, [`serve`] parses and gates: the first frame must be a `REGISTER` whose token
//! authenticates, whose agent is not blocked and whose lease it wins. After it, a registered
//! session runs four things side by side:
//!
//! * the **read loop**, which answers heartbeats itself and hands every other frame, in order, to
//! * the **worker**, which routes them (persistence, replies);
//! * the **heartbeat**, which pings, waits for the answer and renews the lease; and
//! * the **drain**, which delivers the agent's queue, fenced by the lease on every frame.
//!
//! Unlike the Python server, a heartbeat answer never waits behind the reports the agent sent
//! before it: liveness means "the agent answers", not "our backlog is short".

use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::ws::{CloseFrame, Message, WebSocket};
use futures::{SinkExt, StreamExt};
use serde_json::Value;
use tokio::sync::{mpsc, oneshot};

use crate::codes;
use crate::consumers::agent_queue::AgentQueue;
use crate::consumers::connections::Control;
use crate::context::Context;
use crate::message_router;
use crate::messages::{AgentFrame, AgentMessage, FromAgent, Inquiry, ToAgent, ToAgentFrame};
use crate::persist::{caller_ops, leases};
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
    let message = parse(&frame)?;
    let FromAgent::Register {
        token,
        force,
        session_id,
        declaration,
    } = message
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
fn parse(text: &str) -> Result<AgentMessage, Refused> {
    let value: Value = serde_json::from_str(text)
        .map_err(|_| Refused::close(codes::FROM_AGENT_MESSAGE_IS_NOT_VALID_JSON_CODE))?;
    serde_json::from_value::<AgentFrame>(value)
        .map(|envelope| envelope.message)
        .map_err(|e| {
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
    let (work_tx, mut work_rx) = mpsc::unbounded_channel::<AgentMessage>();
    let worker = {
        let ctx = ctx.clone();
        let sender = sender.clone();
        tokio::spawn(async move {
            while let Some(message) = work_rx.recv().await {
                message_router::route(&ctx, agent, message, &sender).await;
            }
        })
    };

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
                match parse(&text) {
                    Ok(FromAgent::HeartbeatAnswer {}) => {
                        match waiter.lock().expect("heartbeat lock").take() {
                            Some(answered) => { let _ = answered.send(()); }
                            None => tracing::warn!(agent, "received a heartbeat answer nobody waited for"),
                        }
                    }
                    Ok(FromAgent::Register { .. }) => {
                        sender.close(codes::FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE);
                        break;
                    }
                    Ok(message) => { let _ = work_tx.send(message); }
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
    drop(work_tx);
    let _ = worker.await;
    ctx.connections.leave(agent, connection_id);
    if let Err(e) = on_agent_disconnected(ctx, agent, connection_id).await {
        tracing::error!(agent, "releasing the lease failed: {e}");
    }
}

/// The lease is released if it is still ours (`on_agent_disconnected`). What was in flight is
/// the reconcile sweep's, after the grace window.
async fn on_agent_disconnected(
    ctx: &Context,
    agent: i64,
    connection_id: &str,
) -> Result<(), sqlx::Error> {
    leases::release_lease(&ctx.db, agent, connection_id).await?;
    Ok(())
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

/// Whether a queued frame is an `ASSIGN` for a task the server already closed.
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
    let Some(task) = raw.get("task").and_then(|t| {
        t.as_str()
            .map(str::to_owned)
            .or_else(|| t.as_i64().map(|i| i.to_string()))
    }) else {
        return false;
    };
    let Ok(task) = task.parse::<i64>() else {
        return false;
    };
    matches!(
        sqlx::query_scalar::<_, bool>("SELECT is_done FROM facade_task WHERE id = $1")
            .bind(task)
            .fetch_optional(&ctx.db)
            .await,
        Ok(Some(true))
    )
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
