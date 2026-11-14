//! Asking about a task: is its agent still on it?
//!
//! Whoever waits for a task may ask. The server asks the agent the task was given to
//! (`INQUIRE`), which answers at once from memory (`INQUIRY_ANSWER`), and hands the answer on.
//! Asking is a read: nothing is sent again, nothing is started, and how long to wait for a
//! result stays with whoever waits.
//!
//! One answer is acted on: the agent a task was given to saying it does not know it. Nobody
//! else can have the task, so it is ended LOST.
//!
//! The agent is only asked when its word settles something. While it is away, or has not yet
//! acknowledged the assign (the assign is still on its way: "unknown" would be true and mean
//! nothing), the server answers with what it knows.

use std::time::Duration;

use chrono::Utc;
use serde::{Deserialize, Serialize};

use crate::backend::{parse_id, BackendError, BackendResult};
use crate::context::Context;
use crate::messages::{TaskStatus, ToAgent};
use crate::persist::transitions;
use crate::redis_keys;
use crate::transport;

/// How long an agent has to answer. It answers from its message loop, so one that does not is
/// not reading: the caller is told so rather than kept waiting.
pub const ANSWER_TIMEOUT: Duration = Duration::from_secs(5);
const ANSWER_POLL: Duration = Duration::from_millis(50);
/// How long an answer nobody collected is kept.
const ANSWER_TTL_SECONDS: i64 = 30;

/// What is known of a task that was asked about.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Inquiry {
    pub task: String,
    /// What the agent said, or `ENDED` for a task the server already closed. None where the
    /// agent was not asked or did not answer: `detail` says why.
    pub status: Option<TaskStatus>,
    /// Whether the agent was asked.
    pub asked: bool,
    pub detail: Option<String>,
    /// The last step the task reported, as the agent has it.
    pub step: Option<u64>,
    /// When the task last reported anything, as the agent has it (epoch seconds).
    pub reported_at: Option<f64>,
    /// What the task last said of its progress, as the agent has it.
    pub progress: Option<i64>,
    pub message: Option<String>,
}

impl Inquiry {
    fn known(task: &str, status: Option<TaskStatus>, detail: impl Into<String>) -> Self {
        Self {
            task: task.to_owned(),
            status,
            asked: false,
            detail: Some(detail.into()),
            step: None,
            reported_at: None,
            progress: None,
            message: None,
        }
    }
}

/// An agent's answer, as it waits in redis for whoever asked.
#[derive(Debug, Serialize, Deserialize)]
pub struct Answer {
    pub status: TaskStatus,
    pub step: Option<u64>,
    pub reported_at: Option<f64>,
    pub progress: Option<i64>,
    pub message: Option<String>,
}

fn answer_key(ctx: &Context, request: &str) -> String {
    redis_keys::key(&ctx.settings, &[&"inquiry", &request])
}

/// The caller a task was assigned by (who may ask about it).
pub async fn caller_of(ctx: &Context, task: &str) -> BackendResult<Option<i64>> {
    let caller: Option<Option<i64>> =
        sqlx::query_scalar("SELECT caller_id FROM facade_task WHERE id = $1")
            .bind(parse_id(task)?)
            .fetch_optional(&ctx.db)
            .await?;
    caller.ok_or_else(|| BackendError::Refused(format!("Task {task} does not exist.")))
}

/// What the server knows of a task before it asks: its agent, whether it is done, whether the
/// assign was acknowledged, its latest event, the agent's kind, and whether the agent is live.
type Asked = (
    Option<i64>,
    bool,
    bool,
    String,
    Option<String>,
    Option<bool>,
);

/// Ask about `task`. See the module docs.
pub async fn inquire(ctx: &Context, task: &str) -> BackendResult<Inquiry> {
    let id = parse_id(task)?;
    let now = Utc::now();
    let cutoff = now
        - chrono::Duration::from_std(ctx.settings.agent_stale_after)
            .unwrap_or(chrono::Duration::MAX);
    let row: Option<Asked> = sqlx::query_as(
        "SELECT t.agent_id, t.is_done, t.picked_up_at IS NOT NULL, t.latest_event_kind, a.kind,
                    (a.connected AND a.last_seen IS NOT NULL AND a.last_seen > $2)
               FROM facade_task t LEFT JOIN facade_agent a ON a.id = t.agent_id
              WHERE t.id = $1",
    )
    .bind(id)
    .bind(cutoff)
    .fetch_optional(&ctx.db)
    .await?;
    let Some((agent, done, acknowledged, kind, agent_kind, live)) = row else {
        return Err(BackendError::Refused(format!(
            "Task {task} does not exist."
        )));
    };
    if done {
        return Ok(Inquiry::known(
            task,
            Some(TaskStatus::Ended),
            format!("it ended {kind}"),
        ));
    }
    let Some(agent) = agent else {
        return Ok(Inquiry::known(task, None, "no agent has been given it yet"));
    };
    if agent_kind.as_deref() == Some("WEBHOOK") {
        return Ok(Inquiry::known(
            task,
            None,
            "its agent takes work over HTTP and cannot be asked",
        ));
    }
    if live != Some(true) {
        let detail = match acknowledged {
            true => "its agent is not connected; it had acknowledged the assign",
            false => "its agent is not connected; the assign waits for it",
        };
        return Ok(Inquiry::known(task, None, detail));
    }
    if !acknowledged {
        return Ok(Inquiry::known(
            task,
            None,
            "its agent has not acknowledged the assign yet",
        ));
    }

    let request = uuid::Uuid::new_v4().to_string();
    let question = ToAgent::Inquire {
        task: task.to_owned(),
        request: Some(request.clone()),
    };
    // On the lane that jumps the backlog and keeps nothing: a question is worth an answer now.
    if let Err(e) = transport::deliver_to_agent(ctx, agent, question, true).await {
        return Ok(Inquiry::known(
            task,
            None,
            format!("its agent could not be asked: {e}"),
        ));
    }
    let Some(answer) = await_answer(ctx, &request).await else {
        return Ok(Inquiry {
            asked: true,
            ..Inquiry::known(
                task,
                None,
                format!(
                    "its agent did not answer within {}s",
                    ANSWER_TIMEOUT.as_secs()
                ),
            )
        });
    };
    if answer.status == TaskStatus::Unknown {
        // The one agent that could have it says it does not. That is how it ended.
        transitions::finalize_lost(
            ctx,
            id,
            "Its agent, asked about it, does not know this task.",
            true,
            None,
            false,
        )
        .await?;
    }
    Ok(Inquiry {
        task: task.to_owned(),
        status: Some(answer.status),
        asked: true,
        detail: None,
        step: answer.step,
        reported_at: answer.reported_at,
        progress: answer.progress,
        message: answer.message,
    })
}

/// An agent answered a question (`INQUIRY_ANSWER`): leave it for whoever asked.
pub async fn answered(ctx: &Context, request: &str, answer: &Answer) -> redis::RedisResult<()> {
    let key = answer_key(ctx, request);
    let answer = serde_json::to_string(answer).expect("an answer serializes");
    let mut redis = ctx.redis.clone();
    redis::pipe()
        .rpush(&key, answer)
        .ignore()
        .expire(&key, ANSWER_TTL_SECONDS)
        .ignore()
        .query_async(&mut redis)
        .await
}

/// Wait for the answer to `request`, on whatever replica the agent's connection is.
async fn await_answer(ctx: &Context, request: &str) -> Option<Answer> {
    let key = answer_key(ctx, request);
    let mut redis = ctx.redis.clone();
    let deadline = tokio::time::Instant::now() + ANSWER_TIMEOUT;
    loop {
        // Polled, not a blocking pop: this connection is shared with everything else.
        let popped: redis::RedisResult<Option<String>> =
            redis::cmd("LPOP").arg(&key).query_async(&mut redis).await;
        match popped {
            Ok(Some(answer)) => return serde_json::from_str(&answer).ok(),
            Ok(None) => {}
            Err(e) => {
                tracing::error!(request, "reading an inquiry's answer failed: {e}");
                return None;
            }
        }
        if tokio::time::Instant::now() >= deadline {
            return None;
        }
        tokio::time::sleep(ANSWER_POLL).await;
    }
}
