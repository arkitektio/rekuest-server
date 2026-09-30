//! rekuest-agentd: the rekuest agent protocol server.
//!
//! Agents connect here (`/agi` over websocket, `/agi/http/{id}` for hook agents) instead of the
//! Python server. It shares Postgres (Django's schema, never migrated here), the Redis agent
//! queues and the channels_redis layer with the Python server, which keeps GraphQL and the
//! subscriptions. The phases that fill it in are in README.md.

pub mod config;
pub mod server;

pub use config::Config;
