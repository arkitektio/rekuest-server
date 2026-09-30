//! The routes (`rekuest/urls.py` and `asgi.py`): health and the agent socket; the hook intake
//! and the internal API as their phases land. Everything is served under the configuration's
//! `force_script_name`, as Django serves it.

use std::sync::Arc;

use axum::{
    extract::{State, WebSocketUpgrade},
    http::StatusCode,
    response::Response,
    routing::get,
    Router,
};

use crate::Configuration;

/// What every handler shares: the app's context, and the configuration it came from.
pub struct AppState {
    pub configuration: Configuration,
    pub facade: facade::Context,
}

pub type Shared = Arc<AppState>;

pub fn router(state: Shared) -> Router {
    let prefix = state
        .configuration
        .django
        .force_script_name
        .trim_matches('/')
        .to_owned();
    let routes = Router::new()
        .route("/ht", get(health))
        .route("/agi", get(agent_socket));
    let routes = if prefix.is_empty() {
        routes
    } else {
        Router::new().nest(&format!("/{prefix}"), routes)
    };
    routes.with_state(state)
}

/// The agent websocket (`re_dynamicpath(r"agi", AgentConsumer.as_asgi())`).
async fn agent_socket(State(state): State<Shared>, upgrade: WebSocketUpgrade) -> Response {
    let facade = state.facade.clone();
    upgrade.on_upgrade(move |socket| facade::consumers::agent_protocol::serve(facade, socket))
}

/// Ready when Postgres and Redis both answer.
async fn health(State(state): State<Shared>) -> StatusCode {
    let db = sqlx::query("SELECT 1")
        .execute(&state.facade.db)
        .await
        .is_ok();
    let mut redis = state.facade.redis.clone();
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
