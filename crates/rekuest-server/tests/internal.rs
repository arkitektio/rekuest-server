//! The internal API's service-token gate, served as agentd serves it (under the configuration's
//! prefix). Needs `AGENTD_TEST_DATABASE_URL` and `AGENTD_TEST_REDIS_URL`.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use facade::provenance::keys::InstanceKey;
use facade::service_trust;
use http_body_util::BodyExt;
use rekuest_server::{settings, urls, Configuration};
use serde_json::{json, Value};
use tower::ServiceExt;

const PEM: &str = "-----BEGIN PRIVATE KEY-----\n\
    MC4CAQAwBQYDK2VwBCIEILK+rl9gVEjfKGiye+mLLjfEUGIdoP0WPC8lMZS3NYK2\n\
    -----END PRIVATE KEY-----\n";
const ME: &str = "live.arkitekt.rekuest";

async fn app() -> Option<axum::Router> {
    let db_url = std::env::var("AGENTD_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("AGENTD_TEST_REDIS_URL").ok()?;
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
    Some(urls::router(Arc::new(urls::AppState {
        configuration,
        facade,
    })))
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

#[tokio::test]
async fn only_a_token_signed_for_this_very_request_gets_through_once() {
    let Some(app) = app().await else { return };
    let key = InstanceKey::from_pem(PEM).unwrap();
    let path = "/rekuest/internal/cancel";
    let body = json!({"task": "999999999999"});
    let sign = |path: &str, body: &Value| {
        service_trust::sign(&key, "POST", path, body.to_string().as_bytes(), ME, ME)
    };

    let (status, answer) = post(&app, path, &body, None).await;
    assert_eq!(
        (status, answer),
        (
            StatusCode::UNAUTHORIZED,
            json!({"error": "No service token"})
        )
    );

    // The signer signs the full path, prefix included.
    let (status, answer) = post(&app, path, &body, Some(sign("/internal/cancel", &body))).await;
    assert_eq!(
        (status, answer),
        (
            StatusCode::UNAUTHORIZED,
            json!({"error": "Service token was signed for another request"})
        )
    );

    let token = sign(path, &body);
    let (status, answer) = post(&app, path, &body, Some(token.clone())).await;
    assert_eq!(
        (status, answer),
        (
            StatusCode::BAD_REQUEST,
            json!({"error": "Task matching query does not exist."})
        )
    );
    let (status, answer) = post(&app, path, &body, Some(token)).await;
    assert_eq!(
        (status, answer),
        (
            StatusCode::CONFLICT,
            json!({"error": "Replayed service token"})
        )
    );
}
