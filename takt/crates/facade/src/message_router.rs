//! Where a registered agent's frames go (`facade/message_router.py`).
//!
//! [`route`] performs the side effects and returns the reply (`ASSIGN_RESPONSE`, …) rather than
//! sending it, so each transport delivers it its own way. A report is never replied to.
//!
//! A **numbered** frame (`pos` + `journal_session`) is handled once per session position: a
//! resend at or below the watermark is skipped; the next position is taken and projected; one
//! ahead of it is not taken, and the reply is a `JOURNAL_NACK` naming the position expected.
//! The cumulative `JOURNAL_ACK` confirms what was projected.
//!
//! **The watermark moves in a transaction with what it counts** (`persist::positions::advance`).
//! Frames come in two kinds:
//!
//! * A **light** frame's whole projection is a few writes: the reports a running task streams
//!   (`LOG`, `PROGRESS`, `YIELD`, `EFFECT`) and the agent's state and locks. It is projected
//!   inside the watermark's transaction, so it is recorded exactly when its position is
//!   counted, and a run of them shares one transaction ([`route_light`]).
//! * Any other frame (the end of a task, a hold, a request) runs through the task machinery,
//!   which commits on its own. The watermark's transaction is held open around it: its row
//!   lock keeps a second backend from projecting the same frame, the answer to a request is
//!   kept for the agent in that same transaction, and a projection that fails rolls the
//!   watermark back, so the resend projects it again. What such a projection writes is
//!   idempotent on the report's own key, for the one case left: this process dying between
//!   the projection's commit and the watermark's.
//!
//! The answer to a numbered request of a socket agent ([`route_socket`]) is a command: it is
//! kept for the agent with the request's position, so a request that was projected is never
//! left without its answer, and a resend of it is skipped.
//! A refusal (unknown state, another agent's task, a duplicate revision) is logged and counts as
//! handled, or a frame refused on every delivery would hold the watermark back forever. Anything
//! else rolls back and fails, so the resend projects it again.
//!
//! The agent's requests (`ASSIGN_REQUEST`, `STATE_REVISION_REQUEST`, `PROBE_REQUEST` and the
//! `CANCEL`/`INTERRUPT`/`PAUSE`/`RESUME` requests) are answered, never fatal: a refusal of any
//! kind is the reply's `error`. Frames whose task is a probe (`p-…`) go to the redis-held probe
//! handlers (`_route_probe_message`), never to the database.

use std::collections::HashMap;
use std::sync::LazyLock;

use sqlx::PgConnection;
use tokio::sync::Semaphore;

use crate::backend::{BackendError, Control};
use crate::consumers::agent_queue;
use crate::context::Context;
use crate::guards;
use crate::messages::{is_probe_task, AgentFrame, FromAgent, ToAgent};
use crate::persist::positions::{self, is_numbered, Position};
use crate::persist::{caller_ops, reports, run_after, state, After, InTxError, PersistError};
use crate::probes;
use crate::registration;

/// How many frames may hold a watermark's transaction open around a projection that commits on
/// connections of its own. Each holds one connection of the pool while its projection takes
/// others: unbounded, a burst of them would hold every connection and wait for one more.
static HELD_OPEN: LazyLock<Semaphore> = LazyLock::new(|| Semaphore::new(8));

/// A frame the router could not handle: the transport closes, as the Python consumer does when
/// the router raises. A numbered frame's refusal never gets here (it counts as handled).
#[derive(Debug, thiserror::Error)]
pub enum RouteError {
    #[error("refused: {0}")]
    Refused(String),
    #[error("routing failed: {0}")]
    Database(#[from] sqlx::Error),
    #[error("routing failed: {0}")]
    Redis(#[from] redis::RedisError),
}

fn log_refusal(what: &str, e: &PersistError) {
    match e {
        PersistError::Refused(reason) => tracing::info!("{what} refused: {reason}"),
        PersistError::Database(e) => tracing::error!("{what} failed: {e}"),
    }
}

/// Route a frame and return the reply. `session_id` is the connection's registered session.
pub async fn route(
    ctx: &Context,
    agent: i64,
    frame: &AgentFrame,
    session_id: Option<&str>,
) -> Result<Option<ToAgent>, RouteError> {
    route_with(ctx, agent, frame, session_id, false).await
}

/// [`route`] for a socket agent: the answer to a numbered request is kept for it as a command
/// (and, for a child found again, the child's events so far behind it) rather than returned.
pub async fn route_socket(
    ctx: &Context,
    agent: i64,
    frame: &AgentFrame,
    session_id: Option<&str>,
) -> Result<Option<ToAgent>, RouteError> {
    route_with(ctx, agent, frame, session_id, true).await
}

/// Whether a numbered frame's whole projection is a few writes of its own: projected inside
/// the watermark's transaction, and with its neighbours ([`route_light`]).
pub fn is_light(frame: &AgentFrame) -> bool {
    is_numbered(frame)
        && (reports::is_stream_report(&frame.message) || state::is_state_report(&frame.message))
}

/// What became of a numbered frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Routed {
    /// Projected (or refused, which counts): its position is counted.
    Handled,
    /// At or below the watermark: a resend.
    Duplicate,
    /// Ahead of its turn: not taken.
    Gap {
        journal_session: String,
        expected: u64,
    },
}

async fn route_with(
    ctx: &Context,
    agent: i64,
    frame: &AgentFrame,
    session_id: Option<&str>,
    socket: bool,
) -> Result<Option<ToAgent>, RouteError> {
    let (Some(pos), Some(journal_session)) = (frame.pos, frame.journal_session.as_deref()) else {
        return project(ctx, agent, frame, session_id).await;
    };
    if !is_numbered(frame) {
        return project(ctx, agent, frame, session_id).await;
    }
    if is_light(frame) {
        // Over HTTP there is no one to ask again: a gap is passed over there.
        let routed = route_light(ctx, agent, &[frame], !socket).await?;
        return Ok(match routed.into_iter().next() {
            Some(Routed::Gap {
                journal_session,
                expected,
            }) if socket => Some(ToAgent::JournalNack {
                journal_session,
                expected,
            }),
            _ => None,
        });
    }
    let pos = pos as i64;
    let _held_open = HELD_OPEN.acquire().await;
    let mut tx = ctx.db.begin().await?;
    let session = positions::session_row(&mut tx, agent, journal_session).await?;
    match take_position(&mut tx, agent, session, journal_session, pos, !socket).await? {
        Position::Project => {}
        Position::Duplicate => {
            tracing::debug!(journal_session, pos, "resend skipped");
            return Ok(None);
        }
        Position::Gap { expected } => {
            tracing::warn!(
                agent,
                journal_session,
                pos,
                expected,
                "a frame arrived ahead of its turn: not taken"
            );
            return Ok(socket.then(|| ToAgent::JournalNack {
                journal_session: journal_session.to_owned(),
                expected: expected as u64,
            }));
        }
    }
    // Dropping `tx` on any error below rolls the watermark back: the resend projects it again.
    let reply = match project(ctx, agent, frame, session_id).await? {
        // Nobody reads the answer to a control request over the socket: the request is kept
        // until acknowledged, and what became of the task arrives as its events.
        Some(ToAgent::ControlResponse {
            accepted, error, ..
        }) if socket => {
            if !accepted {
                tracing::info!(agent, "a control request was refused: {error:?}");
            }
            None
        }
        Some(answer) if socket && answer.is_command() => {
            answer_as_command(&mut tx, agent, answer).await?;
            tx.commit().await?;
            agent_queue::wake(ctx, agent).await;
            return Ok(None);
        }
        other => other,
    };
    tx.commit().await?;
    Ok(reply)
}

/// Move the watermark onto `pos` in `conn`'s transaction. With `pass_gaps` (the HTTP intake,
/// where each report is its own request and what did not arrive will not) the positions before
/// it that never arrived are passed over, with a warning, as they always were there.
async fn take_position(
    conn: &mut PgConnection,
    agent: i64,
    session: i64,
    journal_session: &str,
    pos: i64,
    pass_gaps: bool,
) -> Result<Position, sqlx::Error> {
    match positions::advance(&mut *conn, session, pos).await? {
        Position::Gap { expected } if pass_gaps => {
            tracing::warn!(
                agent,
                journal_session,
                "positions {expected}..{} never arrived; continuing at {pos}",
                pos - 1
            );
            positions::skip_positions(&mut *conn, agent, journal_session, expected, pos - 1)
                .await?;
            positions::advance(&mut *conn, session, pos).await
        }
        position => Ok(position),
    }
}

/// Project a run of light frames ([`is_light`]) in one transaction: each frame's position and
/// what it records are committed together, and a run costs one commit. What became of each, in
/// order. `pass_gaps` as in [`take_position`].
///
/// A frame the database itself refuses (a constraint) takes the whole transaction with it. The
/// run is then projected again frame by frame, and the one frame that is refused is passed
/// over alone: logged, counted as handled, as a refusal always was.
pub async fn route_light(
    ctx: &Context,
    agent: i64,
    frames: &[&AgentFrame],
    pass_gaps: bool,
) -> Result<Vec<Routed>, RouteError> {
    match light_in_one(ctx, agent, frames, pass_gaps).await {
        Err(e) if is_refusal_by_the_database(&e) => {}
        done => return Ok(done?),
    }
    let mut routed = Vec::with_capacity(frames.len());
    for frame in frames {
        match light_in_one(ctx, agent, &[frame], pass_gaps).await {
            Ok(one) => routed.extend(one),
            Err(e) if is_refusal_by_the_database(&e) => {
                tracing::info!("numbered {} refused: {e}", frame_kind(frame));
                routed.extend(pass_over(ctx, agent, frame, pass_gaps).await?);
            }
            Err(e) => return Err(e.into()),
        }
    }
    Ok(routed)
}

/// What `PersistError` calls a refusal when it comes from the database: a constraint said no.
fn is_refusal_by_the_database(e: &sqlx::Error) -> bool {
    matches!(e, sqlx::Error::Database(db)
        if db.is_unique_violation() || db.is_foreign_key_violation() || db.is_check_violation())
}

async fn light_in_one(
    ctx: &Context,
    agent: i64,
    frames: &[&AgentFrame],
    pass_gaps: bool,
) -> Result<Vec<Routed>, sqlx::Error> {
    let mut tx = ctx.db.begin().await?;
    let mut after: Vec<After> = vec![];
    let mut sessions: HashMap<&str, i64> = HashMap::new();
    let mut routed = Vec::with_capacity(frames.len());
    for frame in frames {
        let (Some(pos), Some(journal_session)) = (frame.pos, frame.journal_session.as_deref())
        else {
            continue;
        };
        let session = match sessions.get(journal_session) {
            Some(session) => *session,
            None => {
                let session = positions::session_row(&mut tx, agent, journal_session).await?;
                sessions.insert(journal_session, session);
                session
            }
        };
        let position = take_position(
            &mut tx,
            agent,
            session,
            journal_session,
            pos as i64,
            pass_gaps,
        )
        .await?;
        routed.push(match position {
            Position::Duplicate => Routed::Duplicate,
            Position::Gap { expected } => Routed::Gap {
                journal_session: journal_session.to_owned(),
                expected: expected as u64,
            },
            Position::Project => {
                let projected = if reports::is_stream_report(&frame.message) {
                    reports::on_stream_report(&mut tx, &mut after, agent, frame).await
                } else {
                    state::on_state_in(&mut tx, &mut after, agent, frame).await
                };
                match projected {
                    Ok(()) => {}
                    // Refused before anything was written: the transaction goes on.
                    Err(InTxError::Refused(reason)) => {
                        tracing::info!("numbered {} refused: {reason}", frame_kind(frame))
                    }
                    Err(InTxError::Database(e)) => return Err(e),
                }
                Routed::Handled
            }
        });
    }
    tx.commit().await?;
    run_after(ctx, after).await;
    Ok(routed)
}

/// Count a frame's position without projecting it: the database refused what it would write.
async fn pass_over(
    ctx: &Context,
    agent: i64,
    frame: &AgentFrame,
    pass_gaps: bool,
) -> Result<Vec<Routed>, sqlx::Error> {
    let (Some(pos), Some(journal_session)) = (frame.pos, frame.journal_session.as_deref()) else {
        return Ok(vec![]);
    };
    let mut tx = ctx.db.begin().await?;
    let session = positions::session_row(&mut tx, agent, journal_session).await?;
    let position = take_position(
        &mut tx,
        agent,
        session,
        journal_session,
        pos as i64,
        pass_gaps,
    )
    .await?;
    tx.commit().await?;
    Ok(vec![match position {
        Position::Project => Routed::Handled,
        Position::Duplicate => Routed::Duplicate,
        Position::Gap { expected } => Routed::Gap {
            journal_session: journal_session.to_owned(),
            expected: expected as u64,
        },
    }])
}

/// Keep the answer to a numbered request for its agent, in the transaction that counts the
/// request. A child found again (`created = false`) made its events for an earlier process:
/// they follow the answer, as the same mirrors, and the caller drops the ones it has.
async fn answer_as_command(
    conn: &mut PgConnection,
    agent: i64,
    answer: ToAgent,
) -> Result<(), sqlx::Error> {
    let found_again = match &answer {
        ToAgent::AssignResponse {
            created: false,
            task: Some(task),
            ..
        } => task.parse::<i64>().ok(),
        _ => None,
    };
    let command = agent_queue::NewCommand::of(crate::transport::frame_text(answer));
    agent_queue::push(&mut *conn, agent, &command).await?;
    if let Some(task) = found_again {
        agent_queue::push_events_of(&mut *conn, agent, task).await?;
    }
    Ok(())
}

/// The task a frame names as Python reads it (`getattr(message, "task")`): a `STATE_PATCH`'s
/// changing task is its `task_id`, which does not count.
fn named_task<D>(message: &FromAgent<D>) -> Option<&str> {
    match message {
        FromAgent::StatePatch { .. } | FromAgent::StateUpdate { .. } => None,
        FromAgent::CancelRequest { task, .. }
        | FromAgent::InterruptRequest { task }
        | FromAgent::PauseRequest { task }
        | FromAgent::ResumeRequest { task, .. } => Some(task),
        message => message.task(),
    }
}

/// The projection: dispatch a frame to its handler and return the reply (`_route`).
async fn project(
    ctx: &Context,
    agent: i64,
    frame: &AgentFrame,
    session_id: Option<&str>,
) -> Result<Option<ToAgent>, RouteError> {
    let kind = frame_kind(frame);
    if named_task(&frame.message).is_some_and(is_probe_task) {
        return route_probe(ctx, agent, frame, &kind).await;
    }
    let handled = match &frame.message {
        FromAgent::Shelve { .. } | FromAgent::Unshelve { .. } => {
            return shelving(ctx, agent, frame, session_id).await
        }
        FromAgent::AssignRequest { .. } => {
            return Ok(Some(assign_request(ctx, agent, frame).await))
        }
        FromAgent::StateRevisionRequest {
            parent,
            dependency,
            state,
            since,
            paths,
        } => {
            let answer = guards::state_revision(
                &ctx.db,
                agent,
                parent,
                dependency,
                state,
                since.as_ref(),
                paths,
            )
            .await;
            return Ok(Some(match answer {
                Ok(revision) => ToAgent::StateRevisionResponse {
                    request: frame.id.clone(),
                    revision: Some(revision.revision),
                    changed: revision.changed,
                    detail: revision.detail,
                    error: None,
                },
                Err(e) => {
                    log_request_refusal("StateRevisionRequest", &e);
                    ToAgent::StateRevisionResponse {
                        request: frame.id.clone(),
                        revision: None,
                        changed: None,
                        detail: None,
                        error: Some(e.to_string()),
                    }
                }
            }));
        }
        FromAgent::ProbeRequest {
            reference,
            args,
            action,
            action_hash,
            implementation,
        } => {
            let input = probes::backend::ProbeInput {
                action: action.clone(),
                action_hash: action_hash.clone(),
                implementation: implementation.clone(),
                args: args.clone(),
                reference: reference.clone(),
            };
            return Ok(Some(
                match probes::backend::probe_for_agent(ctx, agent, &input).await {
                    Ok(state) => ToAgent::ProbeResponse {
                        request: frame.id.clone(),
                        probe: state.get("id").cloned(),
                        error: None,
                    },
                    Err(e) => {
                        log_request_refusal("ProbeRequest", &e);
                        ToAgent::ProbeResponse {
                            request: frame.id.clone(),
                            probe: None,
                            error: Some(e.to_string()),
                        }
                    }
                },
            ));
        }
        FromAgent::CancelRequest {
            task,
            auto_interrupt,
        } => {
            let result = caller_ops::on_caller_cancel(ctx, agent, task, *auto_interrupt).await;
            return Ok(Some(control_response(frame, task, result)));
        }
        FromAgent::InterruptRequest { task } => {
            let result = caller_ops::caller_control(ctx, agent, task, Control::Interrupt).await;
            return Ok(Some(control_response(frame, task, result)));
        }
        FromAgent::PauseRequest { task } => {
            let result = caller_ops::caller_control(ctx, agent, task, Control::Pause).await;
            return Ok(Some(control_response(frame, task, result)));
        }
        FromAgent::ResumeRequest { task, step } => {
            let result =
                caller_ops::caller_control(ctx, agent, task, Control::Resume { step: *step }).await;
            return Ok(Some(control_response(frame, task, result)));
        }
        FromAgent::StateUpdate { .. }
        | FromAgent::StatePatch { .. }
        | FromAgent::StateSnapshot { .. }
        | FromAgent::SessionInit { .. }
        | FromAgent::Lock { .. }
        | FromAgent::Unlock { .. } => state::on_state(ctx, agent, frame).await.map(|_| None),
        _ => reports::on_report(ctx, agent, frame).await.map(|_| None),
    };
    match handled {
        Ok(reply) => Ok(reply),
        Err(PersistError::Refused(reason)) if is_numbered(frame) => {
            log_refusal(&format!("numbered {kind}"), &PersistError::Refused(reason));
            Ok(None)
        }
        Err(PersistError::Refused(reason)) => Err(RouteError::Refused(format!("{kind}: {reason}"))),
        Err(PersistError::Database(e)) => Err(RouteError::Database(e)),
    }
}

fn log_request_refusal(what: &str, e: &BackendError) {
    match e {
        BackendError::Refused(reason) | BackendError::Forbidden(reason) => {
            tracing::info!("{what} refused: {reason}")
        }
        BackendError::Database(e) => tracing::error!("{what} failed: {e}"),
    }
}

/// An agent assigning dependent work: a bad request answers with `error`, never tearing the
/// transport down.
async fn assign_request(ctx: &Context, agent: i64, frame: &AgentFrame) -> ToAgent {
    let FromAgent::AssignRequest {
        reference, parent, ..
    } = &frame.message
    else {
        unreachable!("routed as an ASSIGN_REQUEST");
    };
    let refused = |error: String| ToAgent::AssignResponse {
        request: frame.id.clone(),
        reference: reference.clone().unwrap_or_default(),
        task: None,
        created: false,
        error: Some(error),
    };
    if parent.as_deref().is_some_and(is_probe_task) {
        return refused("A probe cannot parent dependent work — assign a task instead.".into());
    }
    // A request is kept until acknowledged, so it can arrive after its reason is gone: sent
    // again by the process that replaced the one whose task asked. Nobody waits for that child.
    if let Some(parent) = parent.as_deref() {
        if !reports::is_task_open(ctx, parent).await {
            return refused(format!(
                "The parent task {parent} has ended: it can no longer assign dependent work."
            ));
        }
    }
    let assigned = match caller_ops::assign_input(&frame.message).expect("an ASSIGN_REQUEST") {
        Ok(input) => caller_ops::on_caller_assign(ctx, agent, &input).await,
        Err(e) => Err(e),
    };
    match assigned {
        // The task's own reference: the request's, or the one the server minted.
        Ok(assigned) => ToAgent::AssignResponse {
            request: frame.id.clone(),
            reference: assigned.reference,
            task: Some(assigned.task.to_string()),
            created: assigned.created,
            error: None,
        },
        Err(e) => {
            log_request_refusal("AssignRequest", &e);
            refused(e.to_string())
        }
    }
}

/// A caller control request's answer (`_control`): accepted, or refused with why.
fn control_response(frame: &AgentFrame, task: &str, result: Result<i64, BackendError>) -> ToAgent {
    match result {
        Ok(task) => ToAgent::ControlResponse {
            request: frame.id.clone(),
            task: Some(task.to_string()),
            accepted: true,
            error: None,
        },
        Err(e) => {
            log_request_refusal("Caller control request", &e);
            ToAgent::ControlResponse {
                request: frame.id.clone(),
                task: Some(task.to_owned()),
                accepted: false,
                error: Some(e.to_string()),
            }
        }
    }
}

/// A frame of a probe (`_route_probe_message`): lifecycle and terminals are acked (the store
/// dedups the resends), the stream events are fire-and-forget. What a probe cannot do is refused
/// without tearing down the transport: control from an agent answers `accepted=false`, a lock
/// is ignored.
async fn route_probe(
    ctx: &Context,
    agent: i64,
    frame: &AgentFrame,
    kind: &str,
) -> Result<Option<ToAgent>, RouteError> {
    use probes::persist::{nonterminal, terminal, ProbeEvent};
    let none = ProbeEvent::default;
    match &frame.message {
        FromAgent::Started { task } => nonterminal(ctx, task, "STARTED", none()).await?,
        FromAgent::Paused { task, .. } => nonterminal(ctx, task, "PAUSED", none()).await?,
        FromAgent::Resumed { task } => nonterminal(ctx, task, "RESUMED", none()).await?,
        FromAgent::Cancelled { task } => terminal(ctx, task, "CANCELLED", None).await?,
        FromAgent::Interrupted { task } => terminal(ctx, task, "INTERRUPTED", None).await?,
        FromAgent::Completed { task } => terminal(ctx, task, "COMPLETED", None).await?,
        FromAgent::Failed { task, error, .. } => terminal(ctx, task, "FAILED", Some(error)).await?,
        FromAgent::Critical { task, error } => terminal(ctx, task, "CRITICAL", Some(error)).await?,
        FromAgent::Yield { task, returns } => {
            let event = ProbeEvent {
                returns: Some(serde_json::Value::Object(returns.clone())),
                ..none()
            };
            nonterminal(ctx, task, "YIELD", event).await?;
            return Ok(None);
        }
        FromAgent::Log {
            task,
            message,
            level,
        } => {
            let event = ProbeEvent {
                message: Some(message.clone()),
                level: serde_json::to_value(level)
                    .ok()
                    .and_then(|l| l.as_str().map(str::to_owned)),
                ..none()
            };
            nonterminal(ctx, task, "LOG", event).await?;
            return Ok(None);
        }
        FromAgent::Progress {
            task,
            progress,
            message,
        } => {
            let event = ProbeEvent {
                progress: progress.map(i64::from),
                message: message.clone(),
                ..none()
            };
            nonterminal(ctx, task, "PROGRESS", event).await?;
            return Ok(None);
        }
        FromAgent::CancelRequest { task, .. }
        | FromAgent::InterruptRequest { task }
        | FromAgent::PauseRequest { task }
        | FromAgent::ResumeRequest { task, .. } => return Ok(Some(ToAgent::ControlResponse {
            request: frame.id.clone(),
            task: Some(task.clone()),
            accepted: false,
            error: Some(
                "Probes are controlled by their caller via GraphQL, not from an agent connection."
                    .into(),
            ),
        })),
        FromAgent::Lock { key, task } => {
            tracing::warn!(
                agent,
                "Lock {key} requested by probe {task} — ignored (probes cannot hold locks)"
            );
            return Ok(None);
        }
        FromAgent::Effect { .. } => return Ok(None),
        // Nothing is kept for a probe, so these have nothing to change. They are not refused:
        // a refusal closes the socket, and an agent whose probed action holds a lock or
        // shelves a value sends them.
        FromAgent::Unlock { .. }
        | FromAgent::Shelve { .. }
        | FromAgent::Held { .. }
        | FromAgent::Inform { .. } => return Ok(None),
        _ => {
            return Err(RouteError::Refused(format!(
                "{kind}: not a message a probe sends"
            )))
        }
    }
    Ok(None)
}

/// Shelving: the agent minted the reference and never waits for an answer, so a refusal is
/// only logged.
async fn shelving(
    ctx: &Context,
    agent: i64,
    frame: &AgentFrame,
    session_id: Option<&str>,
) -> Result<Option<ToAgent>, RouteError> {
    match &frame.message {
        FromAgent::Shelve {
            identifier,
            resource_id,
            label,
            description,
            ..
        } => {
            if is_numbered(frame)
                && session_id.is_some()
                && frame.journal_session.as_deref() != session_id
            {
                // An earlier process's unacked SHELVE: its value died with that process.
                tracing::info!(agent, "numbered SHELVE of an earlier session ignored");
                return Ok(None);
            }
            let shelved = registration::shelve(
                &ctx.db,
                agent,
                identifier,
                resource_id,
                label.as_deref(),
                description.as_deref(),
                true,
            )
            .await
            .map_err(PersistError::from);
            match shelved {
                Ok(_) => Ok(None),
                Err(PersistError::Database(e)) => Err(RouteError::Database(e)),
                Err(e) => {
                    log_refusal("SHELVE", &e);
                    Ok(None)
                }
            }
        }
        FromAgent::Unshelve { drawer, .. } => {
            match registration::unshelve(&ctx.db, agent, drawer).await {
                Ok(()) => Ok(None),
                Err(registration::UnshelveError::Database(e)) => Err(RouteError::Database(e)),
                Err(e) => {
                    tracing::info!(agent, "UNSHELVE refused: {e}");
                    Ok(None)
                }
            }
        }
        _ => Ok(None),
    }
}

fn frame_kind(frame: &AgentFrame) -> String {
    serde_json::to_value(&frame.message)
        .ok()
        .and_then(|v| v.get("type").and_then(|t| t.as_str()).map(str::to_owned))
        .unwrap_or_default()
}
