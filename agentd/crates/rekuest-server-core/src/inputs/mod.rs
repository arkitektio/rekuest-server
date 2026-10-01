//! The input models of a declaration and their validation (`rekuest_core/inputs/models.py`).
//!
//! Deserializing gives the shape (and pydantic's `extra="forbid"` where the Python model has
//! it); `validate()` then runs the model validators, bottom-up as pydantic does, and
//! canonicalizes what they canonicalize (a QUANTITY port's `dimension`). Serializing a model
//! gives its `model_dump()`: every field, defaults and `null`s included.

pub mod calls;
pub mod definitions;
pub mod ports;

pub use calls::*;
pub use definitions::*;
pub use ports::*;

/// Why a declaration is refused, worded as the Python validator words it.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ValidationError(pub String);

/// `Field(min_length=1)`.
pub(crate) fn min_length(value: &str, field: &str) -> Result<(), ValidationError> {
    if value.is_empty() {
        return Err(ValidationError(format!(
            "{field}: String should have at least 1 character"
        )));
    }
    Ok(())
}
