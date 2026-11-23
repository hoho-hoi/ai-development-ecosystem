"""agora's history, sent as OpenTelemetry traces to the place's trace store
(Phoenix). agora keeps no history of its own: only the state it runs on.

One trace per turn, so a trace store shows a session as a conversation
(Phoenix groups traces by session.id and lists each root's input and output):

  turn <module>           the root: the text given (input.value) and the text
                          written at its end (output.value). The harness passes
                          the turn's traceparent on to the LLM gateway, so its
                          LLM calls nest here.
    <event kind>          events during the turn (message.send, session.spawn,
                          ...): one short span each, payload JSON in agora.payload
  <event kind>            events outside any turn (a session starting or
                          ending, mail arriving): a short root span of its own,
                          with the event as input.value and the payload as
                          output.value

Every span carries session.id (the session's id) and the session's module,
version, model and harness; a spawned session's spans carry agora.parent (the
session that spawned it).
"""

from __future__ import annotations

import json
from typing import Any

try:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from opentelemetry.context import Context
    from opentelemetry.trace import Status, StatusCode, set_span_in_context
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
except ImportError:  # telemetry is optional
    TracerProvider = None  # type: ignore[assignment,misc]


class Telemetry:
    """No-op unless an OTLP endpoint (or, in tests, an exporter) is given."""

    def __init__(self, endpoint: str | None = None, place: str = "", exporter: Any = None):
        self.enabled = bool(endpoint or exporter)
        self._sessions: dict[str, dict[str, Any]] = {}  # session id -> attributes for its spans
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

    def _attrs(self, session_id: str | None) -> dict[str, Any]:
        return {"session.id": session_id, **self._sessions.get(session_id or "", {})}

    def session_start(self, session_id: str, parent_id: str | None, attrs: dict[str, Any]) -> None:
        if self.enabled:
            self._sessions[session_id] = _clean(attrs)

    def session_end(self, session_id: str, state: str, reason: str) -> None:
        if not self.enabled:
            return
        turn = self._turns.pop(session_id, None)
        if turn is not None:
            if state == "failed":
                turn.set_status(Status(StatusCode.ERROR, reason))
            turn.end()
        self._sessions.pop(session_id, None)

    def turn_start(self, session_id: str, text: str, attrs: dict[str, Any]) -> None:
        if not self.enabled:
            return
        name = f"turn {self._sessions.get(session_id, {}).get('gen_ai.agent.name', '')}".strip()
        self._turns[session_id] = self.tracer.start_span(name, context=Context(), attributes=_clean({
            "openinference.span.kind": "AGENT", "input.value": text, **self._attrs(session_id), **attrs}))

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
        """One event: a short span under the session's current turn, or a root
        of its own outside a turn."""
        if not self.enabled:
            return
        body = json.dumps(payload, ensure_ascii=False, default=str)
        turn = self._turns.get(session_id or "")
        attrs = {"openinference.span.kind": "CHAIN", "agora.event": kind, **self._attrs(session_id),
                 "agora.session_id": session_id, "agora.thread_id": thread_id, "agora.message_id": message_id,
                 "agora.payload": body}
        if turn is None:  # a trace of its own: say what it is where a viewer shows a trace
            attrs.update({"input.value": kind, "output.value": body})
        ctx = set_span_in_context(turn) if turn is not None else Context()
        self.tracer.start_span(kind, context=ctx, attributes=_clean(attrs)).end()

    def shutdown(self) -> None:
        if self.enabled:
            self.provider.shutdown()


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v is not None and isinstance(v, (str, bool, int, float))}
