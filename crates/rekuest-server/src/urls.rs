//! The routes (`rekuest/urls.py` and `asgi.py`): health now; the agent socket, the hook
//! intake and the internal API as the phases land.

use std::sync::Arc;

use axum::{extract::State, http::StatusCode, routing::get, Router};
use sqlx::PgPool;

use facade::settings::Settings;

use crate::Configuration;

/// What every handler shares.
pub struct AppState {
    pub configuration: Configuration,
    pub settings: Settings,
    pub db: PgPool,
    pub redis: redis::aio::ConnectionManager,
}

pub type Shared = Arc<AppState>;

pub fn router(state: Shared) -> Router {
    let prefix = state
        .configuration
        .django
        .force_script_name
        .trim_matches('/')
        .to_owned();
    let routes = Router::new().route("/ht", get(health));
    let routes = if prefix.is_empty() {
        routes
    } else {
        Router::new().nest(&format!("/{prefix}"), routes)
    };
    routes.with_state(state)
}

/// Ready when Postgres and Redis both answer.
async fn health(State(state): State<Shared>) -> StatusCode {
    let db = sqlx::query("SELECT 1").execute(&state.db).await.is_ok();
    let mut redis = state.redis.clone();
    let redis = redis::cmd("PING")
        .query_async::<String>(&mut redis)
        .await
        .is_ok();
    if db && redis {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}
