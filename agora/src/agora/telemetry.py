"""OpenTelemetry traces of sessions, for viewing in any OTLP backend
(e.g. Phoenix). The append-only history stays the record; this is a view.

Spans follow the OpenTelemetry GenAI conventions where they apply:
  invoke_agent <module>      one per session; a spawned session is a child
                             of the session that spawned it
    turn                     one per prompt turn
      execute_tool <title>   one per ACP tool call
Messages sent are span events on the sender's session span.
"""

from __future__ import annotations

from typing import Any

try:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
    from opentelemetry.trace import Span, Status, StatusCode, set_span_in_context
except ImportError:  # telemetry is optional
    TracerProvider = None  # type: ignore[assignment,misc]


class Telemetry:
    """No-op unless an OTLP endpoint (or, in tests, an exporter) is given."""

    def __init__(self, endpoint: str | None = None, place: str = "", exporter: Any = None):
        self.enabled = bool(endpoint or exporter)
        self._spans: dict[str, Any] = {}  # session id -> session span
        self._turns: dict[str, Any] = {}  # session id -> current turn span
        self._tools: dict[tuple[str, str], Any] = {}  # (session id, toolCallId) -> span
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

    def session_start(self, session_id: str, parent_id: str | None, attrs: dict[str, Any]) -> None:
        if not self.enabled:
            return
        parent = self._spans.get(parent_id or "")
        ctx = set_span_in_context(parent) if parent is not None else None
        name = f"invoke_agent {attrs.get('gen_ai.agent.name', '')}".strip()
        self._spans[session_id] = self.tracer.start_span(
            name, context=ctx, attributes={"gen_ai.operation.name": "invoke_agent", **_clean(attrs)}
        )

    def session_end(self, session_id: str, state: str, reason: str) -> None:
        if not self.enabled:
            return
        for key in [k for k in self._tools if k[0] == session_id]:
            self._tools.pop(key).end()
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

    def turn_start(self, session_id: str, attrs: dict[str, Any]) -> None:
        if not self.enabled or session_id not in self._spans:
            return
        ctx = set_span_in_context(self._spans[session_id])
        self._turns[session_id] = self.tracer.start_span("turn", context=ctx, attributes=_clean(attrs))

    def turn_end(self, session_id: str, attrs: dict[str, Any]) -> None:
        if not self.enabled:
            return
        span = self._turns.pop(session_id, None)
        if span is not None:
            for k, v in _clean(attrs).items():
                span.set_attribute(k, v)
            span.end()

    def acp_update(self, session_id: str, update: dict[str, Any]) -> None:
        """Tool calls from ACP session/update notifications."""
        if not self.enabled:
            return
        kind = update.get("sessionUpdate")
        call = str(update.get("toolCallId") or "")
        if kind == "tool_call" and call:
            parent = self._turns.get(session_id) or self._spans.get(session_id)
            if parent is None:
                return
            title = str(update.get("title") or "tool")
            self._tools[(session_id, call)] = self.tracer.start_span(
                f"execute_tool {title}", context=set_span_in_context(parent),
                attributes=_clean({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": title,
                                   "gen_ai.tool.call.id": call, "agora.tool.kind": update.get("kind")}),
            )
        elif kind == "tool_call_update" and update.get("status") in ("completed", "failed"):
            span = self._tools.pop((session_id, call), None)
            if span is not None:
                if update.get("status") == "failed":
                    span.set_status(Status(StatusCode.ERROR))
                span.end()

    def event(self, session_id: str, name: str, attrs: dict[str, Any]) -> None:
        if not self.enabled:
            return
        span = self._spans.get(session_id)
        if span is not None:
            span.add_event(name, _clean(attrs))

    def shutdown(self) -> None:
        if self.enabled:
            self.provider.shutdown()


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v is not None and isinstance(v, (str, bool, int, float))}
