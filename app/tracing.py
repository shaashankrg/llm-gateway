import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from app.circuit_breaker import circuit_breakers, CircuitState

trace.set_tracer_provider(TracerProvider())
tracer = trace.get_tracer(__name__)

# The ConsoleSpanExporter pretty-prints every span as multi-line JSON to
# stdout. Under load that's the dominant cost: each /generate emits several
# nested spans (auth, circuit-breaker, provider.call, retry.attempt, ...),
# and the synchronous stdout writes serialize the event loop — measured at
# ~90k log lines and ~2.5s p50 latency for 500 concurrent mock requests while
# every container sat <1% CPU. Gated behind an env flag so the tracing output
# is still available for debugging, but a load/throughput run can turn it off
# to measure the gateway's actual routing capacity rather than logging I/O.
if os.environ.get("DISABLE_CONSOLE_SPANS") != "1":
    trace.get_tracer_provider().add_span_processor(
        BatchSpanProcessor(ConsoleSpanExporter())
    )

_LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30)

_duration_view = View(
    instrument_name="gateway_*_duration_seconds",
    aggregation=ExplicitBucketHistogramAggregation(_LATENCY_BUCKETS),
)

metric_reader = PrometheusMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[metric_reader], views=[_duration_view]))
meter = metrics.get_meter(__name__)

requests_total = meter.create_counter(
    "gateway_requests_total",
    description="Total requests processed, labeled by team/provider/outcome",
)
errors_total = meter.create_counter(
    "gateway_errors_total",
    description="Total errors, labeled by provider/error_type",
)
request_duration = meter.create_histogram(
    "gateway_request_duration_seconds",
    description="Total request duration, worker_processing start to end",
    unit="s",
)
provider_call_duration = meter.create_histogram(
    "gateway_provider_call_duration_seconds",
    description="Duration of a single provider call attempt, labeled by provider",
    unit="s",
)
fallback_triggered_total = meter.create_counter(
    "gateway_fallback_triggered_total",
    description="Fallback events, labeled by from_provider/to_provider",
)
_CIRCUIT_STATE_VALUES = {
    CircuitState.CLOSED: 0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN: 2,
}


def _observe_circuit_breaker_state(options):
    return [
        Observation(_CIRCUIT_STATE_VALUES[breaker.state], {"provider": name})
        for name, breaker in circuit_breakers.items()
    ]


circuit_breaker_state = meter.create_observable_gauge(
    "gateway_circuit_breaker_state",
    callbacks=[_observe_circuit_breaker_state],
    description="Current circuit breaker state: 0=closed, 1=half_open, 2=open",
)
