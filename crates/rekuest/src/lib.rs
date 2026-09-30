//! The project: the Rust twin of the Python server's `rekuest/` (Django project) package.
//!
//! [`configuration`] reads the shared `config.yaml` (`configuration.py`), [`settings`] turns it
//! into what the app reads (`settings.py`), [`urls`] routes the requests (`urls.py`/`asgi.py`).
//! The `agentd` binary runs it.

pub mod configuration;
pub mod settings;
pub mod urls;

pub use configuration::Configuration;
