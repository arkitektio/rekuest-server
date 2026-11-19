//! Logging, and (when the config names a collector) traces and metrics over OTLP/HTTP.
//!
//! takt follows the server's own `telemetry` block. Without an endpoint there is no exporter,
//! no provider and no extra thread: spans are only what `tracing` logs, and a trace a request
//! arrived in is passed on unchanged (`facade::trace`). With one, every `tracing` span at INFO
//! is an OpenTelemetry span, and requests continue the trace they arrive in.

use std::time::Duration;

use anyhow::Context as _;
use axum::extract::{MatchedPath, Request};
use axum::http::header::UPGRADE;
use axum::middleware::Next;
use axum::response::Response;
use opentelemetry::global;
use opentelemetry::trace::TracerProvider as _;
use opentelemetry_otlp::{MetricExporter, Protocol, SpanExporter, WithExportConfig};
use opentelemetry_sdk::metrics::SdkMeterProvider;
use opentelemetry_sdk::trace::{Sampler, SdkTracerProvider};
use opentelemetry_sdk::Resource;
use tracing::Instrument;
use tracing_subscriber::filter::LevelFilter;
use tracing_subscriber::prelude::*;
use tracing_subscriber::EnvFilter;

use facade::consumers::connections::Connections;
use facade::trace;

use crate::configuration::TelemetryBlock;

/// What takt is called beside the server, in a trace.
const SERVICE: &str = "rekuest-takt";

/// What was started, to be stopped (flushed) when takt stops.
pub struct Telemetry {
    tracers: Option<SdkTracerProvider>,
    meters: Option<SdkMeterProvider>,
}

/// Set up logging, and recording when `block` names a collector. Once, before anything logs.
pub fn start(block: &TelemetryBlock) -> anyhow::Result<Telemetry> {
    let endpoint = block
        .endpoint
        .as_deref()
        .map(|e| e.trim_end_matches('/'))
        .filter(|e| !e.is_empty());
    let resource = Resource::builder().with_service_name(SERVICE).build();
    let tracers = match endpoint {
        Some(endpoint) => {
            let exporter = SpanExporter::builder()
                .with_http()
                .with_protocol(Protocol::HttpBinary)
                .with_endpoint(format!("{endpoint}/v1/traces"))
                .build()
                .context("building the trace exporter")?;
            Some(
                SdkTracerProvider::builder()
                    .with_batch_exporter(exporter)
                    .with_sampler(Sampler::ParentBased(Box::new(Sampler::TraceIdRatioBased(
                        block.sample_ratio,
                    ))))
                    .with_resource(resource.clone())
                    .build(),
            )
        }
        None => None,
    };
    let meters = match endpoint {
        Some(endpoint) if block.metrics => {
            let exporter = MetricExporter::builder()
                .with_http()
                .with_protocol(Protocol::HttpBinary)
                .with_endpoint(format!("{endpoint}/v1/metrics"))
                .build()
                .context("building the metric exporter")?;
            let provider = SdkMeterProvider::builder()
                .with_periodic_exporter(exporter)
                .with_resource(resource)
                .build();
            global::set_meter_provider(provider.clone());
            Some(provider)
        }
        _ => None,
    };
    let spans = tracers.as_ref().map(|provider| {
        tracing_opentelemetry::layer()
            .with_tracer(provider.tracer("takt"))
            .with_filter(LevelFilter::INFO)
    });
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::fmt::layer()
                .with_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into())),
        )
        .with(spans)
        .init();
    if let Some(endpoint) = endpoint {
        tracing::info!(
            "telemetry goes to {endpoint} ({}% of traces{})",
            (block.sample_ratio * 100.0).round(),
            if meters.is_some() {
                ", with metrics"
            } else {
                ""
            }
        );
    }
    Ok(Telemetry { tracers, meters })
}

impl Telemetry {
    /// Report how many agents this process holds a connection of.
    pub fn observe(&self, connections: &Connections) {
        if self.meters.is_none() {
            return;
        }
        let connections = connections.clone();
        let _gauge = global::meter("takt")
            .u64_observable_gauge("rekuest.agents.connected")
            .with_description("Agents with a connection to this takt process.")
            .with_callback(move |observer| observer.observe(connections.agents() as u64, &[]))
            .build();
    }

    /// Send what is buffered and stop the exporters.
    pub fn stop(self) {
        if let Some(tracers) = self.tracers {
            let _ = tracers.shutdown_with_timeout(Duration::from_secs(3));
        }
        if let Some(meters) = self.meters {
            let _ = meters.shutdown_with_timeout(Duration::from_secs(3));
        }
    }
}

/// Every HTTP request is served inside the trace it arrived in, and is a span of its own where
/// takt records. Not the health check, and not a socket: a span as long as a connection says
/// nothing.
pub async fn traced(request: Request, next: Next) -> Response {
    let arrived = request
        .headers()
        .get(trace::TRACEPARENT)
        .and_then(|value| value.to_str().ok())
        .map(str::to_owned);
    let path = request.extensions().get::<MatchedPath>().map_or_else(
        || request.uri().path().to_owned(),
        |m| m.as_str().to_owned(),
    );
    if path.ends_with("/ht") || request.headers().contains_key(UPGRADE) {
        return next.run(request).await;
    }
    let method = request.method().as_str().to_owned();
    let span = tracing::info_span!(
        "request",
        otel.name = format!("{method} {path}"),
        otel.kind = "server",
        http.request.method = method,
        http.route = path,
        http.response.status_code = tracing::field::Empty,
        otel.status_code = tracing::field::Empty,
    );
    trace::continue_in(&span, arrived.as_deref());
    let response = trace::arrived_in(arrived, next.run(request))
        .instrument(span.clone())
        .await;
    span.record("http.response.status_code", response.status().as_u16());
    if response.status().is_server_error() {
        span.record("otel.status_code", "ERROR");
    }
    response
}
