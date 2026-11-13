# Security

## Reporting a vulnerability

Please don't open a public issue. Use GitHub's private reporting: the **Security** tab of this repo → **Report a vulnerability**.

Especially welcome:

- a way for a key to read or change a bank other than its own
- a way to reach Hindsight's API through the gateway without a key
- a way into the admin UI without the password, or to act in it without a valid session (CSRF, session forgery)
- a key's value ending up anywhere it shouldn't (logs, the database, pages after the one-time display)

## Supported versions

Fixes go into the latest release.
