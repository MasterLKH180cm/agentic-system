# Codex engineering instructions

- Apply spec-first development, MVP, real end-to-end test, then iterate.
- This repository is the execution plane; RAGs is the separate knowledge plane.
- Keep workflow state durable. All independent steps must be safe to run in parallel.
- Every job is at-least-once, so tool side effects must be idempotent.
- Always enforce lease fencing, bounded retries, dependency order and cancellation.
- New tools require schema validation, authorization, a security review and tests.
- Never add unbounded shell, generic SQL execution or arbitrary URL fetching.
- Approval must be distinct from requester credentials for effectful operations.
- Avoid arbitrary multi-agent delegation unless a measured use case needs it.
- Never commit secrets or log confidential user content.
- Run tests plus real two-service smoke before declaring completion.
- If writing any GitHub comment, its entire content must be exactly: @codex review
