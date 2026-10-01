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
