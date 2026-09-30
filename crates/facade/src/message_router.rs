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
//! Not routed here yet: the agent's requests (`ASSIGN_REQUEST`, the `CANCEL`/`INTERRUPT`/`PAUSE`/
//! `RESUME` requests, `PROBE_REQUEST`, `STATE_REVISION_REQUEST`) are Phase 3, and frames of a
//! probe (`p-` task ids), which Python keeps in redis only, arrive with the probes in Phase 3.
//! Both are logged.

use crate::context::Context;
use crate::messages::{is_probe_task, AgentFrame, FromAgent, ToAgent};
use crate::persist::positions::{self, is_numbered, Position};
use crate::persist::{reports, state, PersistError};
use crate::registration;

/// A frame the router could not handle: the transport closes, as the Python consumer does when
/// the router raises. A numbered frame's refusal never gets here (it counts as handled).
#[derive(Debug, thiserror::Error)]
pub enum RouteError {
    #[error("refused: {0}")]
    Refused(String),
    #[error("routing failed: {0}")]
    Database(#[from] sqlx::Error),
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

/// The projection: dispatch a frame to its handler and return the reply (`_route`).
async fn project(
    ctx: &Context,
    agent: i64,
    frame: &AgentFrame,
    session_id: Option<&str>,
) -> Result<Option<ToAgent>, RouteError> {
    let kind = frame_kind(frame);
    if frame.message.task().is_some_and(is_probe_task) {
        tracing::warn!(agent, kind, "a probe's frame: probes arrive with Phase 3");
        return Ok(None);
    }
    let handled = match &frame.message {
        FromAgent::Shelve { .. } | FromAgent::Unshelve { .. } => {
            return shelving(ctx, agent, frame, session_id).await
        }
        FromAgent::AssignRequest { .. }
        | FromAgent::ProbeRequest { .. }
        | FromAgent::StateRevisionRequest { .. }
        | FromAgent::CancelRequest { .. }
        | FromAgent::InterruptRequest { .. }
        | FromAgent::PauseRequest { .. }
        | FromAgent::ResumeRequest { .. } => {
            tracing::warn!(
                agent,
                kind,
                "an agent request: requests arrive with Phase 3"
            );
            return Ok(None);
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
