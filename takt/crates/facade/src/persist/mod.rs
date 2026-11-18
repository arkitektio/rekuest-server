//! What the agent protocol persists (`facade/persist/`).

pub mod caller_ops;
pub mod leases;
pub mod positions;
pub mod reconcile;
pub mod reports;
pub mod state;
pub mod transitions;

/// Why persisting a frame did not happen.
#[derive(Debug, thiserror::Error)]
pub enum PersistError {
    /// Refused on purpose: an unknown state, another agent's task, a duplicate revision, bad
    /// input (the Python side's `ValueError`, `LookupError`, `ObjectDoesNotExist`,
    /// `IntegrityError`). Logged; a numbered frame counts as handled.
    #[error("{0}")]
    Refused(String),
    /// Anything else (the database is down): a numbered frame is released for its resend.
    #[error("database: {0}")]
    Database(sqlx::Error),
}

impl From<sqlx::Error> for PersistError {
    fn from(e: sqlx::Error) -> Self {
        match &e {
            sqlx::Error::RowNotFound => PersistError::Refused("not found".into()),
            sqlx::Error::Database(db)
                if db.is_unique_violation()
                    || db.is_foreign_key_violation()
                    || db.is_check_violation() =>
            {
                PersistError::Refused(db.message().to_owned())
            }
            _ => PersistError::Database(e),
        }
    }
}

pub type PersistResult<T> = Result<T, PersistError>;

/// Why a frame projected inside a transaction of its caller's did not go through.
///
/// Unlike [`PersistError`], a database error stays one: after it the transaction is lost,
/// whatever the error was, and only whoever owns the transaction can decide what that means
/// for the frames it held.
#[derive(Debug, thiserror::Error)]
pub enum InTxError {
    /// Refused on purpose, before anything was written: the transaction goes on, and a numbered
    /// frame counts as handled.
    #[error("{0}")]
    Refused(String),
    #[error("database: {0}")]
    Database(#[from] sqlx::Error),
}

impl From<InTxError> for PersistError {
    fn from(e: InTxError) -> Self {
        match e {
            InTxError::Refused(reason) => PersistError::Refused(reason),
            InTxError::Database(e) => e.into(),
        }
    }
}

pub type InTxResult<T> = Result<T, InTxError>;

/// What a projection inside a transaction leaves to do once that transaction is committed:
/// telling whoever follows. Never before: a subscriber told of a row reads it at once.
#[derive(Debug, Clone, PartialEq)]
pub enum After {
    /// A task event was recorded (`signals::task_event_created`).
    Event(i64),
    /// A `PROGRESS` was recorded: fanned out at most once per interval (`progress_gate`).
    Progress { task: i64, event: i64 },
    /// A task's row changed (`signals::task_saved`).
    TaskSaved(i64),
    /// A patch was recorded (`signals::patch_created`).
    Patch(i64),
    /// An agent's locks changed (`signals::agent_saved`).
    AgentSaved(i64),
}

/// Tell whoever follows, in the order things were written.
pub async fn run_after(ctx: &crate::context::Context, after: Vec<After>) {
    for step in after {
        match step {
            After::Event(event) => crate::signals::task_event_created(ctx, event).await,
            After::Progress { task, event } => {
                crate::progress_gate::progress_recorded(ctx, task, event).await
            }
            After::TaskSaved(task) => crate::signals::task_saved(ctx, task, false).await,
            After::Patch(patch) => crate::signals::patch_created(ctx, patch).await,
            After::AgentSaved(agent) => crate::signals::agent_saved(ctx, agent, false).await,
        }
    }
}
