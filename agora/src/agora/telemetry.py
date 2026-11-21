"""agora's history, sent as OpenTelemetry traces to the place's trace store
(Phoenix). agora keeps no history of its own: only the state it runs on.

  invoke_agent <module>   one per session; a spawned session is a child of
                          the session that spawned it
    turn                  one per prompt turn: the text given (input.value)
                          and the text written at its end (output.value).
                          The harness passes the turn's traceparent on to the
                          LLM gateway, so its LLM calls nest here.
    <event kind>          one short span per event (message.send,
                          session.spawn, session.model, ...): the event's
                          payload as JSON in agora.payload

Every span carries session.id (the session's id), which the store uses to
group a session's traces.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from opentelemetry.trace import Status, StatusCode, set_span_in_context
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
except ImportError:  # telemetry is optional
    TracerProvider = None  # type: ignore[assignment,misc]


class Telemetry:
    """No-op unless an OTLP endpoint (or, in tests, an exporter) is given."""

    def __init__(self, endpoint: str | None = None, place: str = "", exporter: Any = None):
        self.enabled = bool(endpoint or exporter)
        self._spans: dict[str, Any] = {}  # session id -> session span
        self._turns: dict[str, Any] = {}  # session id -> current turn span
        if not self.enabled:
            return
        if TracerProvider is None:
            raise RuntimeError("telemetry needs the opentelemetry-sdk and opentelemetry-exporter-otlp-proto-http packages")
        self.provider = TracerProvider(resource=Resource.create({"service.name": "agora", "agora.place": place}))
        if exporter is not None:
            self.provider.add_span_processor(SimpleSpanProcessor(exporter))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            self.provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        self.tracer = self.provider.get_tracer("agora")

    def _parent(self, session_id: str | None) -> Any:
        if not session_id:
            return None
        return self._turns.get(session_id) or self._spans.get(session_id)

    def session_start(self, session_id: str, parent_id: str | None, attrs: dict[str, Any]) -> None:
        if not self.enabled:
            return
        parent = self._spans.get(parent_id or "")
        ctx = set_span_in_context(parent) if parent is not None else None
        name = f"invoke_agent {attrs.get('gen_ai.agent.name', '')}".strip()
        self._spans[session_id] = self.tracer.start_span(name, context=ctx, attributes={
            "gen_ai.operation.name": "invoke_agent", "openinference.span.kind": "AGENT", "session.id": session_id,
            **_clean(attrs)})

    def session_end(self, session_id: str, state: str, reason: str) -> None:
        if not self.enabled:
            return
        turn = self._turns.pop(session_id, None)
        if turn is not None:
            turn.end()
        span = self._spans.pop(session_id, None)
        if span is not None:
            span.set_attribute("agora.session.state", state)
            span.set_attribute("agora.session.end_reason", reason)
            if state == "failed":
                span.set_status(Status(StatusCode.ERROR, reason))
            span.end()

    def turn_start(self, session_id: str, text: str, attrs: dict[str, Any]) -> None:
        if not self.enabled or session_id not in self._spans:
            return
        ctx = set_span_in_context(self._spans[session_id])
        self._turns[session_id] = self.tracer.start_span("turn", context=ctx, attributes={
            "openinference.span.kind": "CHAIN", "session.id": session_id, "input.value": text, **_clean(attrs)})

    def turn_end(self, session_id: str, text: str, attrs: dict[str, Any]) -> None:
        if not self.enabled:
            return
        span = self._turns.pop(session_id, None)
        if span is not None:
            span.set_attribute("output.value", text)
            for k, v in _clean(attrs).items():
                span.set_attribute(k, v)
            span.end()

    def traceparent(self, session_id: str) -> str | None:
        """The W3C traceparent of the session's current turn, for the harness
        to send with its LLM calls."""
        span = self._turns.get(session_id) if self.enabled else None
        if span is None:
            return None
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier, context=set_span_in_context(span))
        return carrier.get("traceparent")

    def record(self, kind: str, session_id: str | None, thread_id: str | None, message_id: str | None,
               payload: dict[str, Any]) -> None:
        """One event, as a short span under the session's turn (or session)."""
        if not self.enabled:
            return
        parent = self._parent(session_id)
        ctx = set_span_in_context(parent) if parent is not None else None
        attrs = {"openinference.span.kind": "CHAIN", "agora.event": kind, "session.id": session_id,
                 "agora.session_id": session_id, "agora.thread_id": thread_id, "agora.message_id": message_id,
                 "agora.payload": json.dumps(payload, ensure_ascii=False, default=str)}
        self.tracer.start_span(kind, context=ctx, attributes=_clean(attrs)).end()

    def shutdown(self) -> None:
        if self.enabled:
            self.provider.shutdown()


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v is not None and isinstance(v, (str, bool, int, float))}
