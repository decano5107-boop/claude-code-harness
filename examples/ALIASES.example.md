# ALIASES — example alias contract

Copy to `~/.claude/ALIASES.md` (global) or to a project root (`ALIASES.md`).
`hooks/alias_contract.py` reads every table row whose first cell holds quoted
phrases and whose third cell is a policy; everything else in the file is for humans.
All names below are fictional.

- **FIXED** — the phrase has exactly one target; the hook injects it and the model uses it without asking.
- **VOLATILE** — someone else owns the target and it changes; there is no default, so unless the prompt
  carries an exact name (catalog.schema.table, URL, ARN, id) the model asks before acting.
- **REJECTED** — the phrase sounds like a name but points at nothing real; the model asks for the explicit name.
- A phrase shaped like an alias ("the X table", "the X account") that is in no contract is **UNRESOLVED**: ask, don't guess.
- Policy words may also be written in Spanish (FIJO, VOLÁTIL, RECHAZADO).

| Phrases | Resolution | Policy | Evidence |
|---|---|---|---|
| "the warehouse", "orders daily" | `analytics.sales.orders_daily` (one row per order per day) | FIXED | Data catalog entry owned by this team |
| "the prod account", "production account" | AWS account 111122223333, CLI profile `example-prod`, read-only role | FIXED | Account inventory page |
| "the sandbox account" | AWS account 444455556666, CLI profile `example-sandbox` | FIXED | Account inventory page |
| "the feature store" | Owned by the ML platform team; table names change between releases | VOLATILE | Their release notes, not ours |
| "the big table" | Used loosely for several different tables; never refers to one thing | REJECTED | Ambiguous; name the table instead |
