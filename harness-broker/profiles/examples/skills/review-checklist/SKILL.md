---
name: review-checklist
description: Checklist for reviewing a diff. Use when asked to review code changes.
---

1. Does the change do what the request says, and nothing else?
2. Error paths: what happens on empty input, timeouts, partial failure?
3. Concurrency and shared state.
4. Security: injection, secrets in logs, permissions.
5. Tests: do they cover the new behaviour and the failure modes?
