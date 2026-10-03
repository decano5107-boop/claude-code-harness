# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Use GitHub's private vulnerability reporting on this repository:
**Security → Report a vulnerability** (<https://github.com/decano5107-boop/claude-code-harness/security/advisories/new>). It creates a private thread visible only to the maintainer.

This is maintained by one person, so treat these as targets rather than guarantees: an
acknowledgement within 7 days, and a status update within 30.

Disclosure is coordinated: once a fix is released, the advisory is published with credit to the
reporter, unless they prefer to stay anonymous.

## Scope

This project runs locally as developer tooling. The most relevant risks are:

- Prompt or template content that causes an agent to take an unintended action
- A command the guard hooks should stop but let through, or a deliverable the exit gate should
  hold but let go (the README's **Limits** lists what is out of scope by design)
- Repository content (a project's `ALIASES.md` or `guardrails.config.json`) that changes what the
  hooks inject or decide
- Configuration examples that could lead a user to commit a credential

## Supported versions

The latest released minor version receives fixes. Older versions do not.
