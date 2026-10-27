//! The blok language v2, as arkitekt-spec defines it (`arkitekt_spec/blok`): its vocabulary,
//! the catalogs a blok renders with, the expression grammar and the checker. The JSON under
//! `data/` is vendored byte for byte from the spec; `tests/bsx.rs` holds the parser and the
//! checker to the spec's corpus.

pub mod check;
pub mod expr;
pub mod infer;
pub mod view;
pub mod vocabulary;

pub use check::{check_blok_tree, BlokScope};
pub use expr::BsxError;
pub use view::{blok_view, default_view, BlokView, Shadow};
pub use vocabulary::{vocabulary, Finding, Level, Vocabulary};
