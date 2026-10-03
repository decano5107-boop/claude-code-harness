# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- The spec skill, the `/verify-spec` command, the `spec-critic` agent and the `guardrails.mjs`
  Bash hook, formerly the Spec & Verify plugin, with its Node test suite.
- The repository is a Claude Code plugin: `.claude-plugin/` manifests and `hooks/hooks.json`
  register every hook. Installing the plugin is the install path.
- `alias_contract.trust_project_contracts` (default `false`): a project's `ALIASES.md` is read
  only when it is set.
- `audience_check.chat_language_words`: the chat-language word list is configurable.
- CodeQL and Dependabot updates for GitHub Actions.

### Changed
- `examples/settings.example.json` holds only `permissions.deny` entries that protect the ledger
  and config from the Edit and Write tools.
- The exit gate keeps blocking on `stop_hook_active` until the critic runs or `max_blocks` is
  reached; critic names match whole; a STATUS.md BRIEF must name the file on its BRIEF line.
- GitHub Actions are pinned to full commit SHAs.
- SECURITY.md links the private reporting form and states the disclosure policy.

### Removed
- CITATION.cff and the OpenSSF Scorecard workflow.

### Fixed
- AWS guard: shell keywords, unknown runners, `xargs aws`, `python -m awscli`, shells reading
  stdin, and `ecr-public` credential calls no longer pass unexamined.
- Exit gate: deliverables written through Bash or renamed are found; a negated "skip critic" does
  not waive the critic; a user prompt starting with `<` starts a new turn.
- Alias contract: a project row cannot shadow a global one; outside `$HOME` only the working
  directory is searched; invisible characters are stripped.
- Guardrail hook: shell keywords, `${VAR}` and brace expansion, upper-case program names,
  `find -execdir`/`-ok`, `truncate -s0`, pushes to `@` and `heads/main`, `checkout -- :/`, and
  slow project regexes.

## [0.1.0] - 2026-09-29

### Added
- Initial public release.

[Unreleased]: https://github.com/decano5107-boop/claude-code-harness/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/decano5107-boop/claude-code-harness/releases/tag/v0.1.0
