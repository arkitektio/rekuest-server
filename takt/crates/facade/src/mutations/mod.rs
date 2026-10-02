//! The writes behind registration (`facade/mutations/`): only the parts the agent protocol
//! needs; the GraphQL mutations themselves stay in Python.

pub mod agent;
pub mod blok;
pub mod higher_order;
pub mod implementation;

use crate::deletion::DeleteError;

/// Why a registration was refused: a `ValueError` of the checks, or what the database refused
/// (worded as Postgres words it, as `IntegrityError` carries it on the Python side).
#[derive(Debug, thiserror::Error)]
pub enum Refusal {
    #[error("{0}")]
    Invalid(String),
    #[error("{}", database_message(.0))]
    Db(#[from] sqlx::Error),
}

fn database_message(e: &sqlx::Error) -> String {
    e.as_database_error()
        .map_or_else(|| e.to_string(), |db| db.message().to_owned())
}

impl From<DeleteError> for Refusal {
    fn from(e: DeleteError) -> Self {
        match e {
            DeleteError::Db(e) => Self::Db(e),
            protected @ DeleteError::Protected => Self::Invalid(protected.to_string()),
        }
    }
}

impl From<String> for Refusal {
    fn from(message: String) -> Self {
        Self::Invalid(message)
    }
}
