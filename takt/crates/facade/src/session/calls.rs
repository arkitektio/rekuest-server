//! What a caller session asks takt to do (docs/session-protocol.md §3, §5.5, §7, §8).
//!
//! Each call goes through the same backend functions as the internal API the GraphQL
//! mutations forward to (`assign_with_status`, `request_control`, `decide_remembering`,
//! `inquire`, the probe backend): one implementation of validation, ownership and provenance.
//! The session's caller is the human its token names, so it may start roots.
//!
//! Every call carries a `request` id. Its answer is kept for `request_memory` under the caller
//! and that id: a request sent again (after a reconnect) is answered with the first answer,
//! never applied twice.

use redis::AsyncCommands;
use serde_json::{json, Value};

use crate::backend::{self, AssignOrigin, BackendError, Control};
use crate::context::Context;
use crate::probes::backend::{self as probe_backend, ProbeControl};
use crate::redis_keys;
use crate::session::frames::{Answer, ClientFrame};
use crate::session::topics::{self, Who};

fn memory_key(ctx: &Context, who: &Who, request: &str) -> String {
    redis_keys::key(&ctx.settings, &[&"session_answer", &who.caller, &request])
}

/// The answer kept for `request`, if it was answered before.
pub async fn remembered(ctx: &Context, who: &Who, request: &str) -> Option<Answer> {
    let mut redis = ctx.redis.clone();
    let kept: Option<String> = redis.get(memory_key(ctx, who, request)).await.ok()?;
    serde_json::from_str(&kept?).ok()
}

async fn remember(ctx: &Context, who: &Who, answer: &Answer) {
    let Ok(text) = serde_json::to_string(answer) else {
        return;
    };
    let mut redis = ctx.redis.clone();
    let seconds = ctx.settings.session.request_memory.as_secs().max(1);
    if let Err(e) = redis
        .set_ex::<_, _, ()>(memory_key(ctx, who, answer.request()), text, seconds)
        .await
    {
        tracing::warn!("keeping a session answer failed: {e}");
    }
}

/// A refusal as the window reads it: a sentence. The server's own trouble is not explained.
fn refused(request: &str, e: BackendError) -> Answer {
    let error = match e {
        BackendError::Refused(message) | BackendError::Forbidden(message) => message,
        BackendError::Database(e) => {
            tracing::error!("session call failed: {e}");
            "The server could not do this just now.".into()
        }
    };
    Answer::Refused {
        request: request.to_owned(),
        error,
    }
}

/// Run one call and keep its answer. `None` for a frame that is not a call.
pub async fn run(ctx: &Context, who: &Who, frame: ClientFrame) -> Option<Answer> {
    let request = frame.request()?.to_owned();
    if let Some(answer) = remembered(ctx, who, &request).await {
        return Some(answer);
    }
    let answer = match call(ctx, who, frame).await {
        Ok(answer) => answer,
        Err(e) => refused(&request, e),
    };
    remember(ctx, who, &answer).await;
    Some(answer)
}

async fn call(ctx: &Context, who: &Who, frame: ClientFrame) -> Result<Answer, BackendError> {
    let accepted = |request: String, result: Value| Answer::Accepted { request, result };
    match frame {
        ClientFrame::Assign {
            request,
            input,
            deadman,
        } => {
            let assigned = backend::assign_with_status(
                ctx,
                &who.principal,
                &input,
                AssignOrigin::default(),
            )
            .await?;
            if let Some(seconds) = deadman {
                if assigned.created {
                    arm(ctx, who, assigned.task, Some(seconds)).await?;
                }
            }
            let record = topics::task_change(ctx, assigned.task).await?;
            Ok(Answer::Assigned {
                request,
                reference: assigned.reference,
                task: assigned.task.to_string(),
                created: assigned.created,
                record,
            })
        }
        ClientFrame::Cancel { request, task } => {
            control(ctx, who, &task, Control::Cancel).await?;
            Ok(accepted(request, json!({"task": task})))
        }
        ClientFrame::Interrupt { request, task } => {
            control(ctx, who, &task, Control::Interrupt).await?;
            Ok(accepted(request, json!({"task": task})))
        }
        ClientFrame::Pause { request, task } => {
            control(ctx, who, &task, Control::Pause).await?;
            Ok(accepted(request, json!({"task": task})))
        }
        ClientFrame::Resume {
            request,
            task,
            step,
        } => {
            control(ctx, who, &task, Control::Resume { step }).await?;
            Ok(accepted(request, json!({"task": task})))
        }
        ClientFrame::Decide {
            request,
            task,
            approved,
            answer,
            values,
            remember,
        } => {
            let task = crate::holds::decide_remembering(
                ctx,
                &who.principal,
                &task,
                approved,
                answer,
                values,
                crate::holds::Remember::parse(remember.as_deref())?,
            )
            .await?;
            Ok(accepted(request, json!({"task": task.to_string()})))
        }
        ClientFrame::Inquire { request, task } => {
            if crate::inquiry::caller_of(ctx, &task).await? != Some(who.caller) {
                return Err(BackendError::Forbidden("Not a task you assigned.".into()));
            }
            let inquiry = crate::inquiry::inquire(ctx, &task).await?;
            Ok(accepted(request, json!(inquiry)))
        }
        ClientFrame::Deadman {
            request,
            task,
            seconds,
        } => {
            let task_id = backend::parse_id(&task)?;
            arm(ctx, who, task_id, seconds).await?;
            Ok(accepted(
                request,
                json!({"task": task, "deadman_seconds": seconds}),
            ))
        }
        ClientFrame::Probe { request, input } => {
            let probe = probe_backend::probe(ctx, &who.principal, &input, "graphql").await?;
            Ok(accepted(request, json!(probe)))
        }
        ClientFrame::ProbeCancel { request, probe } => {
            let probe =
                probe_backend::control(ctx, &who.principal, &probe, ProbeControl::Cancel).await?;
            Ok(accepted(request, json!(probe)))
        }
        ClientFrame::ProbePause { request, probe } => {
            let probe =
                probe_backend::control(ctx, &who.principal, &probe, ProbeControl::Pause).await?;
            Ok(accepted(request, json!(probe)))
        }
        ClientFrame::ProbeResume { request, probe } => {
            let probe =
                probe_backend::control(ctx, &who.principal, &probe, ProbeControl::Resume).await?;
            Ok(accepted(request, json!(probe)))
        }
        _ => Err(BackendError::Refused("Not a call.".into())),
    }
}

/// A control, as the GraphQL mutations ask for it: the caller must own the task.
async fn control(ctx: &Context, who: &Who, task: &str, control: Control) -> Result<(), BackendError> {
    backend::request_control(ctx, task, control, Some(who.caller)).await?;
    Ok(())
}

/// Arm the dead man switch of `task` on this session (`seconds`), or disarm it (`None`). Any
/// session of the task's owner may; arming from another window takes the switch over.
pub async fn arm(
    ctx: &Context,
    who: &Who,
    task: i64,
    seconds: Option<u32>,
) -> Result<(), BackendError> {
    let seconds = seconds.map(|s| s.max(ctx.settings.session.deadman_min));
    let changed = sqlx::query(
        "UPDATE facade_task
            SET deadman_seconds = $3,
                deadman_session_id = CASE WHEN $3::int IS NULL THEN NULL ELSE $4 END
          WHERE id = $1 AND caller_id = $2 AND NOT is_done",
    )
    .bind(task)
    .bind(who.caller)
    .bind(seconds.map(|s| s as i32))
    .bind(&who.session)
    .execute(&ctx.db)
    .await
    .map_err(BackendError::Database)?;
    if changed.rows_affected() == 0 {
        return Err(BackendError::Refused(
            "Only a running task you started can be tied to your window.".into(),
        ));
    }
    Ok(())
}
