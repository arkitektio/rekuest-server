//! The blok language v2, as arkitekt-spec defines it (`arkitekt_spec/blok`): its vocabulary,
//! the expression grammar and the checker. `data/vocabulary.json` is vendored byte for byte
//! from the spec; `tests/bsx.rs` holds the parser and the checker to the spec's corpus.
//!
//! The server holds a blok to the language alone. It reads no catalog: what a component
//! takes, where it may sit and which catalog it is of is checked by whoever draws the blok.

pub mod check;
pub mod declared;
pub mod expr;
pub mod infer;
pub mod reads;
pub mod vocabulary;

pub use check::{check_blok, check_blok_tree, BlokScope};
pub use declared::{blok_header, BlokHeader};
pub use expr::BsxError;
pub use vocabulary::{vocabulary, Finding, Level, Vocabulary};
