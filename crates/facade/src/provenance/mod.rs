//! Provenance token issuing (`facade/provenance/`).
//!
//! Rekuest is the provenance authority: at dispatch it mints a signed (Ed25519) JWT attesting
//! who caused an assignment and with which inputs. An attestation to be recorded downstream,
//! never an authorization grant. Verification, single-use `jti` and the provenance store live
//! downstream.

pub mod audience;
pub mod canonical;
pub mod keys;
pub mod mint;
pub mod principal;

pub use mint::{mint_token_for_task, MintError, MintTask};
