//! What every part of the app shares: the database, redis, the settings, the token verifier,
//! and this process's live connections. The Python side reaches these through Django's
//! globals (`settings`, the ORM, `persist_backend`); here they are passed.

use std::sync::Arc;

use authentikate::Verifier;
use sqlx::PgPool;

use crate::consumers::connections::Connections;
use crate::settings::Settings;

#[derive(Clone)]
pub struct Context {
    pub db: PgPool,
    /// A shared, multiplexed connection for everything that does not block.
    pub redis: redis::aio::ConnectionManager,
    /// For a connection of one's own: a blocking pop must not stall everyone else's commands.
    pub redis_client: redis::Client,
    pub settings: Arc<Settings>,
    pub verifier: Arc<Verifier>,
    pub connections: Connections,
}
