//! The server's project: the Rust twin of the Python server's `rekuest/` (Django project)
//! package. Named `rekuest_server`, because `rekuest` is the client.
//!
//! [`configuration`] reads the shared `config.yaml` (`configuration.py`), [`settings`] turns it
//! into what the app reads (`settings.py`), [`urls`] routes the requests (`urls.py`/`asgi.py`), [`internal`] is the internal API.
//! The `agentd` binary runs it.

pub mod configuration;
pub mod internal;
pub mod settings;
pub mod urls;

pub use configuration::Configuration;
