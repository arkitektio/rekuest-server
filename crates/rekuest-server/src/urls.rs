//! The routes (`rekuest/urls.py` and `asgi.py`): health, the agent socket, and the internal API
//! (`/internal/…`, [`crate::internal`]) the HookAgent intake (`/agi/http/{agent}`) and hub services' signals (`/agi/signal/{service}`). Everything is served
//! under the configuration's `force_script_name`, as Django serves it.

use std::sync::Arc;

use axum::{
    body::Bytes,
    extract::{OriginalUri, Path, State, WebSocketUpgrade},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
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
        .route("/agi", get(agent_socket))
        .route("/agi/http/{agent_id}", post(hook_intake))
        .route("/agi/signal/{service}", post(signal_intake))
        .merge(crate::internal::routes());
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

/// The HookAgent intake (`re_dynamicpath(r"agi/http/(?P<agent_id>[^/]+)$", hook_intake)`). Any
/// other method is a 405, as Django's view answers it. The full path, script name included, is
/// what a service token is signed for.
async fn hook_intake(
    State(state): State<Shared>,
    Path(agent_id): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let (status, body) =
        facade::http_intake::hook_intake(&state.facade, &agent_id, uri.path(), &headers, &body)
            .await;
    let status = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    (status, Json(body)).into_response()
}

/// A hub service's signal (`re_dynamicpath(r"agi/signal/(?P<service>[^/]+)$", signal_intake)`).
async fn signal_intake(
    State(state): State<Shared>,
    Path(service): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let (status, body) =
        facade::signal_intake::signal_intake(&state.facade, &service, uri.path(), &headers, &body)
            .await;
    let status = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    (status, Json(body)).into_response()
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
