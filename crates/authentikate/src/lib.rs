//! Token authentication: the Rust twin of the Python `authentikate` package (4.1.1, the
//! version the rekuest server pins). Module names follow it: [`base_models`] (the token and
//! the settings), [`decode`] (verification), [`revocation`], [`utils`] (`authenticate_token`),
//! and [`expand`] (a token to its user, organization, membership and client rows).

pub mod base_models;
pub mod decode;
pub mod errors;
pub mod expand;
pub mod revocation;
pub mod utils;

pub use base_models::{AuthentikateSettings, JwtToken};
pub use decode::Verifier;
pub use errors::AuthentikateError;
pub use utils::{authenticate_token, authenticate_token_or_none};
