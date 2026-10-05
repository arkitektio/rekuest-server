//! The two listeners' routes, served as takt serves them (under the configuration's prefix). Needs `TAKT_TEST_DATABASE_URL` and `TAKT_TEST_REDIS_URL`.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use rekuest_server::{settings, urls, Configuration};
use serde_json::{json, Value};
use tower::ServiceExt;

const PEM: &str = "-----BEGIN PRIVATE KEY-----\n\
    MC4CAQAwBQYDK2VwBCIEILK+rl9gVEjfKGiye+mLLjfEUGIdoP0WPC8lMZS3NYK2\n\
    -----END PRIVATE KEY-----\n";

/// The public listener's router, and the internal one's.
async fn apps() -> Option<(axum::Router, axum::Router)> {
    let db_url = std::env::var("TAKT_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("TAKT_TEST_REDIS_URL").ok()?;
    let configuration: Configuration = serde_yaml::from_value(serde_yaml::to_value(json!({
        "django": {"force_script_name": "rekuest"},
        "postgres": {"db_name": "rekuest", "username": "u", "password": "p", "host": "localhost"},
        "redis": {"host": "localhost"},
        "instance": {"private_key": PEM},
    })).unwrap())
    .unwrap();
    let redis_client = redis::Client::open(redis_url).unwrap();
    let facade = facade::Context {
        db: sqlx::PgPool::connect(&db_url).await.unwrap(),
        redis: redis::aio::ConnectionManager::new(redis_client.clone())
            .await
            .unwrap(),
        channel_layer: kante::ChannelLayer::new(
            redis_client.clone(),
            kante::ChannelLayerConfig::default(),
        )
        .await
        .unwrap(),
        redis_client,
        settings: Arc::new(settings::from_configuration(&configuration).unwrap()),
        verifier: Arc::new(authentikate::Verifier::new(
            authentikate::AuthentikateSettings::prepare(&json!({"audience": "rekuest"}), true)
                .unwrap(),
        )),
        connections: facade::consumers::connections::Connections::default(),
    };
    let state = Arc::new(urls::AppState {
        configuration,
        facade,
    });
    Some((urls::router(state.clone()), urls::internal_router(state)))
}

async fn post(
    app: &axum::Router,
    path: &str,
    body: &Value,
    authorization: Option<String>,
) -> (StatusCode, Value) {
    let mut request = Request::post(path).header("content-type", "application/json");
    if let Some(authorization) = authorization {
        request = request.header("authorization", authorization);
    }
    let response = app
        .clone()
        .oneshot(request.body(Body::from(body.to_string())).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// Reaching the internal listener is the gate: a request there needs no token.
#[tokio::test]
async fn the_internal_listener_asks_for_no_token() {
    let Some((_, app)) = apps().await else { return };
    let body = json!({"task": "999999999999"});

    let (status, answer) = post(&app, "/rekuest/internal/cancel", &body, None).await;
    assert_eq!(
        (status, answer),
        (
            StatusCode::BAD_REQUEST,
            json!({"error": "Task matching query does not exist."})
        )
    );
}

/// `agent` is the endpoint's name and `agi` its former one: both are served, each as a route
/// of its own, since a service token is signed for the path it was sent to.
#[tokio::test]
async fn the_agent_endpoint_answers_under_both_its_names() {
    let Some((app, _)) = apps().await else { return };
    for name in ["agent", "agi"] {
        // No websocket handshake: refused by the socket handler, not as an unknown path.
        let response = app
            .clone()
            .oneshot(
                Request::get(format!("/rekuest/{name}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_ne!(response.status(), StatusCode::NOT_FOUND, "{name}");
    }
    // The intakes answer in JSON, whichever name they were asked under; a path nobody serves
    // has no body at all.
    for intake in ["http/999999999999", "signal/nobody"] {
        let (status, by_name) =
            post(&app, &format!("/rekuest/agent/{intake}"), &json!({}), None).await;
        let (former_status, by_former_name) =
            post(&app, &format!("/rekuest/agi/{intake}"), &json!({}), None).await;
        assert!(status.is_client_error(), "{intake}: {status}");
        assert_ne!(by_name, Value::Null, "{intake}: answered by the intake");
        assert_eq!(
            (status, by_name),
            (former_status, by_former_name),
            "{intake}"
        );
    }
    let (status, answer) = post(&app, "/rekuest/agents/http/1", &json!({}), None).await;
    assert_eq!((status, answer), (StatusCode::NOT_FOUND, Value::Null));
}

/// The internal API is on its own listener: where agents and services connect there is no such
/// path, and where the server connects there is no agent endpoint.
#[tokio::test]
async fn each_listener_serves_only_its_own_routes() {
    let Some((public, internal)) = apps().await else {
        return;
    };
    let path = "/rekuest/internal/schedule/upcoming";
    let body = json!({"timings": [
        {"interval_seconds": 60, "timezone": "UTC", "created_at": "2026-01-01T00:00:00Z", "count": 2},
        {"cron": "whenever", "timezone": "UTC", "created_at": "2026-01-01T00:00:00Z", "count": 2},
    ]});

    let (status, answer) = post(&public, path, &body, None).await;
    assert_eq!((status, answer), (StatusCode::NOT_FOUND, Value::Null));

    // One request for several timings; one takt cannot read fails alone.
    let (status, answer) = post(&internal, path, &body, None).await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let upcoming = answer["upcoming"].as_array().unwrap();
    assert_eq!(upcoming[0]["slots"].as_array().unwrap().len(), 2);
    assert!(upcoming[1]["error"].as_str().unwrap().contains("whenever"));

    for (app, health) in [(&public, StatusCode::OK), (&internal, StatusCode::OK)] {
        let response = app
            .clone()
            .oneshot(Request::get("/rekuest/ht").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), health);
    }
    let (status, _) = post(&internal, "/rekuest/agent/signal/nobody", &json!({}), None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

/// The server's health check tells the listeners apart by a route only the internal one has:
/// asked with the wrong method it is refused there (405) and unknown on the public one (404).
/// A server pointed at the address agents reach must not take that for its takt.
#[tokio::test]
async fn the_listeners_are_told_apart_by_an_internal_route() {
    let Some((public, internal)) = apps().await else {
        return;
    };
    let ask = |app: &axum::Router| {
        app.clone().oneshot(
            Request::get("/rekuest/internal/assign")
                .body(Body::empty())
                .unwrap(),
        )
    };
    assert_eq!(
        ask(&internal).await.unwrap().status(),
        StatusCode::METHOD_NOT_ALLOWED
    );
    assert_eq!(ask(&public).await.unwrap().status(), StatusCode::NOT_FOUND);
}
