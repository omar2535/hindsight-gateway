"""What a key may do: its own bank, and a few harmless global reads. Nothing else."""

import re
import urllib.parse

# No bank needed: harmless, read-only.
GLOBAL_GET = {"/version", "/v1/version", "/health", "/health/live", "/health/ready", "/v1/bank-template-schema"}

# Health checks need no key (probes).
HEALTH = {"/health", "/health/live", "/health/ready"}

# What a key may do inside its own bank: the first path segment after the bank id.
# Left out on purpose: aliases, clone, transfer, document-transfer (they link or copy to
# other banks) and webhooks (Hindsight would call out to any URL). New Hindsight
# endpoints stay blocked until they're added here.
BANK_SUBRESOURCES = {
    "memories", "reflect", "documents", "entities", "graph", "mental-models", "directives",
    "operations", "observations", "stats", "tags", "profile", "config", "files",
    "attachments", "consolidate", "consolidation", "consolidation-strategies", "background",
    "knowledge-base", "pages", "health", "audit-logs", "llm-requests", "import", "export",
    "prompts",
}
READ_ONLY_SUBRESOURCES = {"config"}  # bank tuning stays with the admin (Hindsight UI)

SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._~%:@!$&'()*+,;=-]+")
BANK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def allowed(method: str, path: str, bank: str) -> bool:
    """May a key for `bank` send `method path` (path without the query string)?"""
    if method in ("GET", "HEAD") and path in GLOBAL_GET:
        return True
    parts = path.split("/")
    # "", "v1", "default", "banks", <bank>, [sub, ...]
    if len(parts) < 5 or parts[:4] != ["", "v1", "default", "banks"]:
        return False
    for seg in parts[1:]:
        if not seg or not SAFE_SEGMENT.fullmatch(seg):
            return False
        dec = urllib.parse.unquote(seg)
        if dec in (".", "..") or "/" in dec or "\\" in dec:
            return False
    if parts[4] != bank:  # compared raw: bank ids never need encoding
        return False
    if len(parts) == 5:
        return method in ("GET", "PUT", "PATCH", "DELETE")
    sub = parts[5]
    if sub not in BANK_SUBRESOURCES:
        return False
    if sub in READ_ONLY_SUBRESOURCES and method not in ("GET", "HEAD"):
        return False
    return True
