use std::sync::Arc;

use anyhow::Context;
use rekuest_server::{settings, urls, Configuration};
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    let path = std::env::var("AGENTD_CONFIG").unwrap_or_else(|_| "config.yaml".into());
    let configuration = Configuration::load(&path)?;
    let bind = std::env::var("AGENTD_BIND").unwrap_or_else(|_| "0.0.0.0:8080".into());

    let db = sqlx::postgres::PgPoolOptions::new()
        .max_connections(32)
        .connect(&configuration.postgres.url())
        .await
        .context("connecting to postgres")?;
    let redis_client = redis::Client::open(configuration.redis.url()).context("redis url")?;
    let redis = redis::aio::ConnectionManager::new(redis_client.clone())
        .await
        .context("connecting to redis")?;

    let authentikate = authentikate::AuthentikateSettings::prepare(
        &configuration.authentikate,
        configuration.django.debug,
    )?;
    let facade = facade::Context {
        db,
        redis,
        redis_client,
        settings: Arc::new(settings::from_configuration(&configuration)),
        verifier: Arc::new(authentikate::Verifier::new(authentikate)),
        connections: facade::consumers::connections::Connections::default(),
    };
    let state = Arc::new(urls::AppState {
        configuration,
        facade,
    });
    let listener = tokio::net::TcpListener::bind(&bind).await?;
    tracing::info!("agentd listening on {bind}");
    axum::serve(listener, urls::router(state))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}
