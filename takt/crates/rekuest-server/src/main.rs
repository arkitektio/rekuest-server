use std::sync::Arc;

use anyhow::Context;
use rekuest_server::{settings, urls, Configuration};
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    // The server's own file: `ARKITEKT_CONFIG_FILE` names it for both, `TAKT_CONFIG` for
    // takt alone.
    let path = std::env::var("TAKT_CONFIG")
        .or_else(|_| std::env::var("ARKITEKT_CONFIG_FILE"))
        .unwrap_or_else(|_| "config.yaml".into());
    let configuration = Configuration::load(&path)?;
    // takt signs what it asks of the server (the upkeep jobs) and of HookAgents with the
    // instance key: without one, none of that would be accepted.
    anyhow::ensure!(
        configuration.instance.is_some(),
        "{path} has no `instance` block: takt needs the instance key it signs its requests with"
    );
    let bind = std::env::var("TAKT_BIND").unwrap_or_else(|_| "0.0.0.0:8080".into());
    // The internal API has a listener of its own, which only the rekuest server reaches: a unix
    // socket both mount (`unix:/run/takt/internal.sock`), or an address. Reaching it is the only
    // gate, so by default it is this machine alone: a deployment says where the server is.
    let internal_bind =
        std::env::var("TAKT_INTERNAL_BIND").unwrap_or_else(|_| "127.0.0.1:8081".into());
    // `takt healthcheck`: is the takt of this configuration serving? For the container's
    // HEALTHCHECK, in an image that carries no HTTP client.
    if std::env::args().nth(1).as_deref() == Some("healthcheck") {
        return healthcheck(&configuration, &bind).await;
    }

    let db = sqlx::postgres::PgPoolOptions::new()
        .max_connections(32)
        .connect(&configuration.postgres.url())
        .await
        .context("connecting to postgres")?;
    // Before anything is served or swept: the tables must be the ones this build was written for.
    facade::schema::wait_until_migrated(&db).await;
    let redis_client = redis::Client::open(configuration.redis.url()).context("redis url")?;
    let redis = redis::aio::ConnectionManager::new(redis_client.clone())
        .await
        .context("connecting to redis")?;

    let authentikate = authentikate::AuthentikateSettings::prepare(
        &configuration.authentikate,
        configuration.django.debug,
    )?;
    let channel_layer = kante::ChannelLayer::new(
        redis_client.clone(),
        kante::ChannelLayerConfig {
            prefix: configuration.redis.channel_prefix.clone(),
            capacity: configuration.redis.channel_capacity,
            ..kante::ChannelLayerConfig::default()
        },
    )
    .await
    .context("connecting the channel layer")?;
    let facade = facade::Context {
        db,
        redis,
        redis_client,
        settings: Arc::new(settings::from_configuration(&configuration)?),
        verifier: Arc::new(authentikate::Verifier::new(authentikate)),
        channel_layer,
        connections: facade::consumers::connections::Connections::default(),
    };
    // The reaper sweeps every deadline from inside this process. Any number of replicas run
    // it: a tick token in redis lets one of them sweep per tick.
    tokio::spawn(facade::reaper::run_forever(facade.clone()));
    tracing::info!("reaper running every {:?}", facade.settings.sweep_interval);
    // The two jobs only the Python server can do, asked for when they are due.
    tokio::spawn(facade::upkeep::run_forever(facade.clone()));
    // What the server says about its schedules (a NOTIFY in the transaction that changes one).
    tokio::spawn(facade::schedule_notices::run_forever(facade.clone()));
    let state = Arc::new(urls::AppState {
        configuration,
        facade,
    });
    serve_internal(&internal_bind, urls::internal_router(state.clone())).await?;
    let listener = tokio::net::TcpListener::bind(&bind).await?;
    tracing::info!("takt listening on {bind}");
    axum::serve(listener, urls::router(state))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}

/// Serve the internal API on `bind`: `unix:<path>` is a socket file (a stale one, left by a
/// takt that was killed, is replaced), anything else an address.
async fn serve_internal(bind: &str, router: axum::Router) -> anyhow::Result<()> {
    if let Some(path) = bind.strip_prefix("unix:") {
        match std::fs::remove_file(path) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => {
                return Err(e).context(format!("replacing the socket at {path}"))
            }
            _ => {}
        }
        let listener = tokio::net::UnixListener::bind(path)
            .with_context(|| format!("binding the internal socket at {path}"))?;
        tokio::spawn(async move { axum::serve(listener, router).await });
    } else {
        let listener = tokio::net::TcpListener::bind(bind)
            .await
            .with_context(|| format!("binding the internal API to {bind}"))?;
        tokio::spawn(async move { axum::serve(listener, router).await });
    }
    tracing::info!("takt's internal API listening on {bind}");
    Ok(())
}

/// `GET {prefix}/ht` on this machine's port; an error unless it answers 200.
async fn healthcheck(configuration: &Configuration, bind: &str) -> anyhow::Result<()> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let port = bind.rsplit(':').next().unwrap_or("8080");
    let prefix = configuration.django.force_script_name.trim_matches('/');
    let path = if prefix.is_empty() {
        "/ht".to_owned()
    } else {
        format!("/{prefix}/ht")
    };
    let mut stream = tokio::net::TcpStream::connect(format!("127.0.0.1:{port}"))
        .await
        .context("takt is not listening")?;
    stream
        .write_all(format!("GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n").as_bytes())
        .await?;
    let mut answer = String::new();
    stream.read_to_string(&mut answer).await?;
    let status = answer.lines().next().unwrap_or_default();
    anyhow::ensure!(status.contains(" 200 "), "takt answered {status:?}");
    Ok(())
}
