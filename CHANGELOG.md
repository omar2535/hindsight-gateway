# Changelog

Each release's section here is also its GitHub Release notes.

## [Unreleased]

- Clients that hang up mid-request (a closed port-forward, say) no longer leave a traceback in the log.

## [0.1.0] - 2026-11-13

First release, moved out of [Hermify](https://github.com/omar2535/Hermify) (where it was a single script) into its own project.

- The proxy: each key opens one Hindsight bank; other banks, the bank list, aliases, clones, transfers, webhooks, global lookups and config changes are refused.
- A SQLite key store (hashes only) with seed keys imported from the environment.
- An admin UI to create, rename, move, disable, rotate and delete keys, with an activity log.
- A container image for amd64 and arm64.

[Unreleased]: https://github.com/omar2535/hindsight-gateway/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/omar2535/hindsight-gateway/releases/tag/v0.1.0
