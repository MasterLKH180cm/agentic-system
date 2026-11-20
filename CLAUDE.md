# Claude Code engineering instructions

AGENTS.md is the canonical shared contract for both Codex and Claude Code.
Follow it for architecture, security, concurrency, tests and scope. Keep
production behavior independent of which coding assistant is used.

MCP tool integration is documented in README.md. Model provider Anthropic
is optional and independent of use of Claude Code as a developer tool.
If writing any GitHub comment, its entire content must be exactly: @codex review
