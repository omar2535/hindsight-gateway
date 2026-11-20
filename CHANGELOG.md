# Changelog

Each release's section here is also its GitHub Release notes.

## [Unreleased]

## [0.3.0] - 2026-11-20

Enough for Hindsight's control-plane UI to run with one person's key and show only their bank.

- `GET /v1/default/banks` answers with the key's bank only (it was refused). `q`, `limit` and `offset` still work, within that one bank.
- `GET /v1/default/chunks/{id}` is forwarded, and the chunk comes back only if it belongs to the key's bank; otherwise 404.
- `GET /v1/default/files/download/{key}` (export archives) is allowed when the storage key names the key's bank, read the same way Hindsight reads it.
- **Breaking:** `DELETE /v1/default/banks/{bank}` is refused, even for the key's own bank. Deleting a whole bank is an admin job (Hindsight's own UI), so one click in a per-person UI can't erase someone's memory.

## [0.2.0] - 2026-11-13

- One hostname for both: `HGW_API_PREFIX` (e.g. `/api`) and `HGW_ADMIN_PREFIX` (e.g. `/ui`). The API also answers under its prefix and forwards to Hindsight without it; bare paths keep working. The UI's links, redirects and session cookie live under its prefix.
- Clients that hang up mid-request (a closed port-forward, say) no longer leave a traceback in the log.

## [0.1.0] - 2026-11-13

First release, moved out of [Hermify](https://github.com/omar2535/Hermify) (where it was a single script) into its own project.

- The proxy: each key opens one Hindsight bank; other banks, the bank list, aliases, clones, transfers, webhooks, global lookups and config changes are refused.
- A SQLite key store (hashes only) with seed keys imported from the environment.
- An admin UI to create, rename, move, disable, rotate and delete keys, with an activity log.
- A container image for amd64 and arm64.

[Unreleased]: https://github.com/omar2535/hindsight-gateway/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/omar2535/hindsight-gateway/releases/tag/v0.3.0
[0.2.0]: https://github.com/omar2535/hindsight-gateway/releases/tag/v0.2.0
[0.1.0]: https://github.com/omar2535/hindsight-gateway/releases/tag/v0.1.0
