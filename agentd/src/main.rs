use std::sync::Arc;

use anyhow::Context;
use rekuest_agentd::{server, Config};
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    let path = std::env::var("AGENTD_CONFIG").unwrap_or_else(|_| "config.yaml".into());
    let config = Config::load(&path)?;
    let bind = std::env::var("AGENTD_BIND").unwrap_or_else(|_| "0.0.0.0:8080".into());

    let db = sqlx::postgres::PgPoolOptions::new()
        .max_connections(32)
        .connect(&config.postgres.url())
        .await
        .context("connecting to postgres")?;
    let redis = redis::aio::ConnectionManager::new(
        redis::Client::open(config.redis.url()).context("redis url")?,
    )
    .await
    .context("connecting to redis")?;

    let state = Arc::new(server::AppState { config, db, redis });
    let listener = tokio::net::TcpListener::bind(&bind).await?;
    tracing::info!("agentd listening on {bind}");
    axum::serve(listener, server::router(state))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}
