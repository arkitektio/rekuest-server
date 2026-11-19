//! The trace a task is part of (W3C Trace Context), carried whether or not takt records.
//!
//! A task remembers where in a trace it was assigned (`facade_task.traceparent`) and every
//! dispatch of it says so (`Assign.traceparent`): what the agent does for the task, and what
//! it asks of others on its behalf, then belongs to that trace.
//!
//! Recording is the binary's business (`telemetry` in the config): with it, `tracing` spans
//! are OpenTelemetry spans, and a task is assigned from inside takt's own `assign` span.
//! Without it nothing here costs anything, and a trace is passed on as it arrived.

use std::collections::HashMap;
use std::future::Future;

use opentelemetry::propagation::TextMapPropagator;
use opentelemetry::trace::TraceContextExt;
use opentelemetry::Context;
use opentelemetry_sdk::propagation::TraceContextPropagator;
use tracing_opentelemetry::OpenTelemetrySpanExt;

/// The request header, and the wire field, a trace travels in.
pub const TRACEPARENT: &str = "traceparent";

tokio::task_local! {
    /// The trace the request being served arrived in.
    static ARRIVED: Option<String>;
}

/// Serve `work` as part of the trace its request arrived in (`None`: in none).
pub async fn arrived_in<F: Future>(traceparent: Option<String>, work: F) -> F::Output {
    ARRIVED.scope(traceparent, work).await
}

/// The place in a trace a `traceparent` names; `None` for one that names none (malformed).
pub fn context_of(traceparent: &str) -> Option<Context> {
    let carrier = HashMap::from([(TRACEPARENT.to_owned(), traceparent.to_owned())]);
    let context = TraceContextPropagator::new().extract(&carrier);
    context.span().span_context().is_valid().then_some(context)
}

/// `traceparent`, if it names a place in a trace: what an agent sent is not stored unread.
pub fn valid(traceparent: Option<&str>) -> Option<&str> {
    traceparent.filter(|given| context_of(given).is_some())
}

/// Make `span` the continuation of `traceparent` (nothing, where takt does not record).
pub fn continue_in(span: &tracing::Span, traceparent: Option<&str>) {
    if let Some(context) = traceparent.and_then(context_of) {
        let _ = span.set_parent(context);
    }
}

/// The span, as a `traceparent`; `None` where takt does not record.
pub fn of_span(span: &tracing::Span) -> Option<String> {
    let context = span.context();
    if !context.span().span_context().is_valid() {
        return None;
    }
    let mut carrier = HashMap::new();
    TraceContextPropagator::new().inject_context(&context, &mut carrier);
    carrier.remove(TRACEPARENT)
}

/// Where in a trace the running code is: takt's own span where it records; else `given` (what
/// the caller named); else what the request arrived in.
pub fn here(given: Option<&str>) -> Option<String> {
    of_span(&tracing::Span::current())
        .or_else(|| given.map(str::to_owned))
        .or_else(|| ARRIVED.try_with(Clone::clone).ok().flatten())
}

/// What takt measures of tasks. The global meter's no-ops until the binary names a collector.
pub mod measures {
    use std::sync::OnceLock;

    use chrono::{DateTime, Utc};
    use opentelemetry::metrics::{Counter, Histogram};
    use opentelemetry::{global, KeyValue};

    struct Instruments {
        assigned: Counter<u64>,
        pickup: Histogram<f64>,
        duration: Histogram<f64>,
    }

    /// Made at first use, which is after the binary set its provider: an instrument made
    /// before that stays a no-op.
    fn instruments() -> &'static Instruments {
        static INSTRUMENTS: OnceLock<Instruments> = OnceLock::new();
        INSTRUMENTS.get_or_init(|| {
            let meter = global::meter("takt");
            Instruments {
                assigned: meter
                    .u64_counter("rekuest.tasks.assigned")
                    .with_description("Tasks created by an assign.")
                    .build(),
                pickup: meter
                    .f64_histogram("rekuest.task.pickup.duration")
                    .with_unit("s")
                    .with_description("From a task's dispatch to its agent picking it up.")
                    .build(),
                duration: meter
                    .f64_histogram("rekuest.task.duration")
                    .with_unit("s")
                    .with_description("From a task's creation to its end, by how it ended.")
                    .build(),
            }
        })
    }

    fn seconds_since(then: DateTime<Utc>) -> f64 {
        (Utc::now() - then).num_milliseconds().max(0) as f64 / 1000.0
    }

    /// A task was created.
    pub fn assigned() {
        instruments().assigned.add(1, &[]);
    }

    /// A task was picked up, having waited since `dispatched`.
    pub fn picked_up(dispatched: DateTime<Utc>) {
        instruments().pickup.record(seconds_since(dispatched), &[]);
    }

    /// A task created at `created` ended as `kind` (`DONE`, `ERROR`, `LOST`, ...).
    pub fn ended(created: DateTime<Utc>, kind: &str) {
        instruments().duration.record(
            seconds_since(created),
            &[KeyValue::new("outcome", kind.to_owned())],
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PARENT: &str = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";

    #[test]
    fn a_malformed_traceparent_names_no_place() {
        assert!(valid(Some(PARENT)).is_some());
        assert!(valid(Some("not-a-trace")).is_none());
        assert!(valid(Some(
            "00-00000000000000000000000000000000-00f067aa0ba902b7-01"
        ))
        .is_none());
        assert!(valid(None).is_none());
    }

    #[tokio::test]
    async fn without_recording_a_trace_is_passed_on_as_it_arrived() {
        assert_eq!(here(None), None, "outside of any request");
        let seen = arrived_in(Some(PARENT.to_owned()), async { here(None) }).await;
        assert_eq!(seen.as_deref(), Some(PARENT));
        let named = arrived_in(Some(PARENT.to_owned()), async {
            here(Some(
                "00-11111111111111111111111111111111-2222222222222222-01",
            ))
        })
        .await;
        assert_eq!(
            named.as_deref(),
            Some("00-11111111111111111111111111111111-2222222222222222-01"),
            "what the caller named comes before what the request arrived in"
        );
    }
}
