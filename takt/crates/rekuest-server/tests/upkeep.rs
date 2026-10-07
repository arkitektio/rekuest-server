//! Upkeep: takt asks the Python server for its job when it is due (`facade::upkeep`).
//! The server here is a stand-in on a real socket that checks the service token as the Python
//! one does. Needs `TAKT_TEST_DATABASE_URL` and `TAKT_TEST_REDIS_URL`.

use std::sync::{Arc, Mutex};

use axum::body::Bytes;
use axum::extract::{OriginalUri, Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::routing::post;
use axum::{Json, Router};
use facade::provenance::keys::InstanceKey;
use facade::settings::Settings;
use facade::upkeep::{self, Outcome};
use facade::{service_trust, Context};
use serde_json::{json, Value};

const PEM: &str = "-----BEGIN PRIVATE KEY-----\n\
    MC4CAQAwBQYDK2VwBCIEILK+rl9gVEjfKGiye+mLLjfEUGIdoP0WPC8lMZS3NYK2\n\
    -----END PRIVATE KEY-----\n";
const ME: &str = "live.arkitekt.rekuest";

/// What the stand-in server answers next, and what it was asked.
#[derive(Default)]
struct Server {
    answers: Mutex<Vec<(StatusCode, Value)>>,
    asked: Mutex<Vec<String>>,
}

async fn job(
    State(server): State<Arc<Server>>,
    Path(job): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
    body: Bytes,
) -> (StatusCode, Json<Value>) {
    let key = InstanceKey::from_pem(PEM).unwrap();
    let authorization = headers.get("authorization").and_then(|v| v.to_str().ok());
    if let Err(e) = service_trust::verify(&key, "POST", uri.path(), &body, authorization, ME) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": e.0})));
    }
    server.asked.lock().unwrap().push(job);
    let mut answers = server.answers.lock().unwrap();
    let (status, answer) = if answers.is_empty() {
        (StatusCode::OK, json!({"ok": true, "failed": []}))
    } else {
        answers.remove(0)
    };
    (status, Json(answer))
}

/// The stand-in, listening; its base URL (script name included, as `rekuest.server_url` is).
async fn serve() -> (Arc<Server>, String) {
    let server = Arc::new(Server::default());
    let app = Router::new()
        .route("/rekuest/_rekuest/upkeep/{job}", post(job))
        .with_state(server.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/rekuest", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (server, url)
}

async fn context(server_url: Option<String>) -> Option<Context> {
    let db_url = std::env::var("TAKT_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("TAKT_TEST_REDIS_URL").ok()?;
    let redis_client = redis::Client::open(redis_url).unwrap();
    Some(Context {
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
        settings: Arc::new(Settings {
            // A namespace of its own: when a job is next due is a redis key.
            redis_key_prefix: format!("upkeep-test-{}", uuid::Uuid::new_v4().simple()),
            instance_key: Some(Arc::new(InstanceKey::from_pem(PEM).unwrap())),
            server_url,
            ..Settings::default()
        }),
        verifier: Arc::new(authentikate::Verifier::new(
            authentikate::AuthentikateSettings::prepare(&json!({"audience": "rekuest"}), true)
                .unwrap(),
        )),
        connections: facade::consumers::connections::Connections::default(),
    })
}

fn asked(server: &Server) -> Vec<String> {
    server.asked.lock().unwrap().clone()
}

#[tokio::test]
async fn the_server_accepts_takts_request_as_its_own() {
    let (server, url) = serve().await;
    let Some(ctx) = context(Some(url)).await else {
        return;
    };

    let answer = upkeep::call(&ctx.settings, "provision").await.unwrap();

    assert_eq!(answer, json!({"ok": true, "failed": []}));
    assert_eq!(asked(&server), ["provision"]);
}

#[tokio::test]
async fn a_done_job_is_not_due_again_for_any_replica() {
    let (server, url) = serve().await;
    let Some(ctx) = context(Some(url)).await else {
        return;
    };

    assert_eq!(upkeep::run(&ctx, &upkeep::PROVISION).await, Outcome::Done);

    // Another replica shares the redis: for it the job is not due either.
    assert!(!upkeep::take(&ctx, &upkeep::PROVISION).await);
    let another = upkeep::Job {
        name: "another",
        ..upkeep::PROVISION
    };
    assert!(
        upkeep::take(&ctx, &another).await,
        "each job has its own time"
    );
    assert_eq!(asked(&server), ["provision"]);
}

#[tokio::test]
async fn a_failed_pass_is_due_again_sooner() {
    let (server, url) = serve().await;
    let Some(ctx) = context(Some(url)).await else {
        return;
    };
    let job = upkeep::Job {
        name: "provision",
        every: std::time::Duration::from_secs(300),
        retry: std::time::Duration::from_millis(100),
    };
    server.answers.lock().unwrap().push((
        StatusCode::OK,
        json!({"ok": false, "skipped": false, "failed": ["mikro"]}),
    ));

    let outcome = upkeep::run(&ctx, &job).await;

    assert!(matches!(outcome, Outcome::Failed(why) if why.contains("mikro")));
    assert!(!upkeep::take(&ctx, &job).await, "not before the retry");
    tokio::time::sleep(std::time::Duration::from_millis(150)).await;
    assert!(upkeep::take(&ctx, &job).await, "but long before `every`");
}

#[tokio::test]
async fn an_unreachable_or_refusing_server_is_a_failed_pass() {
    let Some(ctx) = context(Some("http://127.0.0.1:9/rekuest".into())).await else {
        return;
    };
    assert!(matches!(
        upkeep::run(&ctx, &upkeep::PROVISION).await,
        Outcome::Failed(why) if why.contains("unreachable")
    ));

    let (server, url) = serve().await;
    let ctx = context(Some(url)).await.unwrap();
    server.answers.lock().unwrap().push((
        StatusCode::SERVICE_UNAVAILABLE,
        json!({"error": "Replay guard unavailable"}),
    ));
    assert!(matches!(
        upkeep::run(&ctx, &upkeep::PROVISION).await,
        Outcome::Failed(why) if why.contains("Replay guard unavailable")
    ));
}

#[tokio::test]
async fn without_a_server_url_nothing_is_asked() {
    let Some(ctx) = context(None).await else {
        return;
    };
    assert!(upkeep::call(&ctx.settings, "provision").await.is_err());
    // Returns at once instead of keeping the job.
    tokio::time::timeout(std::time::Duration::from_secs(2), upkeep::run_forever(ctx))
        .await
        .expect("upkeep is off");
}
