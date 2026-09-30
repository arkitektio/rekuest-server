//! Where a registered agent's frames go (`facade/message_router.py`).
//!
//! [`route`] performs the side effects and returns the reply (`EVENT_ACK`, `SHELVED`, …) rather
//! than sending it, so each transport delivers it its own way.
//!
//! A **numbered** frame (`pos` + `journal_session`) is handled once per session position: a
//! resend at or below the watermark is skipped; anything else is claimed, projected and
//! confirmed. It is never answered with an `EVENT_ACK`: the cumulative `JOURNAL_ACK` covers it.
//! A refusal (unknown state, another agent's task, a duplicate revision) is logged and counts as
//! handled, or a frame refused on every delivery would hold the watermark back forever. Anything
//! else releases the claim and fails, so the resend projects it again.
//!
//! The agent's requests (`ASSIGN_REQUEST`, `STATE_REVISION_REQUEST`, `PROBE_REQUEST` and the
//! `CANCEL`/`INTERRUPT`/`PAUSE`/`RESUME` requests) are answered, never fatal: a refusal of any
//! kind is the reply's `error`. Frames whose task is a probe (`p-…`) go to the redis-held probe
//! handlers (`_route_probe_message`), never to the database.

use crate::backend::{BackendError, Control};
use crate::context::Context;
use crate::guards;
use crate::messages::{is_probe_task, AgentFrame, FromAgent, ToAgent};
use crate::persist::positions::{self, is_numbered, Position};
use crate::persist::{caller_ops, reports, state, PersistError};
use crate::probes;
use crate::registration;

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

/// The durable-report acknowledgement, so the agent can stop retaining it (`_ack`).
fn ack(frame: &AgentFrame) -> ToAgent {
    ToAgent::EventAck {
        event: Some(frame.id.clone()),
        task: frame.message.task().map(str::to_owned),
        seq: frame.seq,
    }
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
    let (Some(pos), Some(journal_session)) = (frame.pos, frame.journal_session.as_deref()) else {
        return project(ctx, agent, frame, session_id).await;
    };
    if !is_numbered(frame) {
        return project(ctx, agent, frame, session_id).await;
    }
    let pos = pos as i64;
    if positions::claim_position(&ctx.db, agent, journal_session, pos).await? == Position::Duplicate
    {
        tracing::debug!(journal_session, pos, "resend skipped");
        return Ok(None);
    }
    match project(ctx, agent, frame, session_id).await {
        Ok(reply) => {
            positions::confirm_position(&ctx.db, agent, journal_session, pos).await?;
            Ok(reply.filter(|reply| !matches!(reply, ToAgent::EventAck { .. })))
        }
        Err(e) => {
            positions::release_position(&ctx.db, agent, journal_session, pos).await?;
            Err(e)
        }
    }
}

/// The task a frame names as Python reads it (`getattr(message, "task")`): a `STATE_PATCH`'s
/// changing task is its `task_id`, which does not count.
fn named_task<D>(message: &FromAgent<D>) -> Option<&str> {
    match message {
        FromAgent::StatePatch { .. } => None,
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
        FromAgent::StatePatch {
            task_id: Some(task_id),
            ..
        } if is_probe_task(task_id) => {
            // `Patch.task` is a real foreign key: keep the patch, drop the probe's link.
            let mut unlinked = frame.clone();
            if let FromAgent::StatePatch { task_id, .. } = &mut unlinked.message {
                *task_id = None;
            }
            state::on_state(ctx, agent, &unlinked).await.map(|_| None)
        }
        FromAgent::StatePatch { .. }
        | FromAgent::StateSnapshot { .. }
        | FromAgent::SessionInit { .. }
        | FromAgent::Lock { .. }
        | FromAgent::Unlock { .. } => state::on_state(ctx, agent, frame).await.map(|_| None),
        _ => reports::on_report(ctx, agent, frame)
            .await
            .map(|_| acked(frame)),
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
        FromAgent::Failed { task, error } => terminal(ctx, task, "FAILED", Some(error)).await?,
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
        _ => {
            return Err(RouteError::Refused(format!(
                "{kind}: not a message a probe sends"
            )))
        }
    }
    Ok(Some(ack(frame)))
}

/// Lifecycle confirmations and terminals are acked; the stream events are fire-and-forget.
fn acked(frame: &AgentFrame) -> Option<ToAgent> {
    matches!(
        frame.message,
        FromAgent::Started { .. }
            | FromAgent::Paused { .. }
            | FromAgent::Resumed { .. }
            | FromAgent::Completed { .. }
            | FromAgent::Failed { .. }
            | FromAgent::Critical { .. }
            | FromAgent::Cancelled { .. }
            | FromAgent::Interrupted { .. }
    )
    .then(|| ack(frame))
}

/// Shelving: numbered, the agent minted the reference and never waits for an answer; without
/// numbering, request and reply, a failure answering with `error`.
async fn shelving(
    ctx: &Context,
    agent: i64,
    frame: &AgentFrame,
    session_id: Option<&str>,
) -> Result<Option<ToAgent>, RouteError> {
    let numbered = is_numbered(frame);
    match &frame.message {
        FromAgent::Shelve {
            reference,
            identifier,
            resource_id,
            label,
            description,
            ..
        } => {
            if numbered && session_id.is_some() && frame.journal_session.as_deref() != session_id {
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
                numbered,
            )
            .await
            .map_err(PersistError::from);
            match (shelved, numbered) {
                (Ok(_), true) => Ok(None),
                (Ok(drawer), false) => Ok(Some(ToAgent::Shelved {
                    reference: reference.clone().unwrap_or_default(),
                    drawer: Some(drawer.to_string()),
                    error: None,
                })),
                (Err(PersistError::Database(e)), _) => Err(RouteError::Database(e)),
                (Err(e), true) => {
                    log_refusal("numbered SHELVE", &e);
                    Ok(None)
                }
                (Err(e), false) => {
                    log_refusal("SHELVE", &e);
                    Ok(Some(ToAgent::Shelved {
                        reference: reference.clone().unwrap_or_default(),
                        drawer: None,
                        error: Some(e.to_string()),
                    }))
                }
            }
        }
        FromAgent::Unshelve { reference, drawer } => {
            match registration::unshelve(&ctx.db, agent, drawer, numbered).await {
                Ok(()) if numbered => Ok(None),
                Ok(()) => Ok(Some(ToAgent::Unshelved {
                    reference: reference.clone().unwrap_or_default(),
                    error: None,
                })),
                Err(registration::UnshelveError::Database(e)) => Err(RouteError::Database(e)),
                Err(e) if numbered => {
                    tracing::info!(agent, "numbered UNSHELVE refused: {e}");
                    Ok(None)
                }
                Err(e) => {
                    tracing::warn!(agent, "unshelve failed: {e}");
                    Ok(Some(ToAgent::Unshelved {
                        reference: reference.clone().unwrap_or_default(),
                        error: Some(e.to_string()),
                    }))
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
