"""What a key may do: its own bank, and a few harmless global reads. Nothing else."""

import re
import urllib.parse

# No bank needed: harmless, read-only.
GLOBAL_GET = {"/version", "/v1/version", "/health", "/health/live", "/health/ready", "/v1/bank-template-schema"}

# No key needed: health checks (probes), and the version, which Hindsight's UI asks for
# on its sign-in page, before anyone has a key.
KEYLESS = {"/health", "/health/live", "/health/ready", "/version", "/v1/version"}

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

# Global endpoints the gateway answers itself, scoped to the key's bank:
BANK_LIST = "/v1/default/banks"  # filtered to the key's bank
CHUNKS = "/v1/default/chunks/"  # forwarded, then returned only if the chunk is in the key's bank
FILES = "/v1/default/files/download/"  # allowed when the storage key names the key's bank

SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._~%:@!$&'()*+,;=-]+")
BANK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _safe(segments: list[str]) -> bool:
    for seg in segments:
        if not seg or not SAFE_SEGMENT.fullmatch(seg):
            return False
        dec = urllib.parse.unquote(seg)
        if dec in (".", "..") or "/" in dec or "\\" in dec:
            return False
    return True


def _file_bank(key: str) -> str | None:
    """The bank a stored file belongs to, read from its storage key the way Hindsight does:
    tenants/<schema>/banks/<bank>/... or banks/<bank>/... (raw: bank ids never need encoding)."""
    parts = key.split("/")
    if not _safe(parts):
        return None
    if len(parts) > 4 and parts[0] == "tenants" and parts[2] == "banks":
        return parts[3]
    if len(parts) > 2 and parts[0] == "banks":
        return parts[1]
    return None


def special(method: str, path: str) -> str | None:
    """'banks' or 'chunk' for global reads the proxy answers for the key's bank only."""
    if method != "GET":
        return None
    if path == BANK_LIST:
        return "banks"
    if path.startswith(CHUNKS) and _safe(path[len(CHUNKS):].split("/")):
        return "chunk"
    return None


def allowed(method: str, path: str, bank: str) -> bool:
    """May a key for `bank` send `method path` (path without the query string)?"""
    if method in ("GET", "HEAD") and path in GLOBAL_GET:
        return True
    if special(method, path):
        return True
    if method in ("GET", "HEAD") and path.startswith(FILES):
        return _file_bank(path[len(FILES):]) == bank
    parts = path.split("/")
    # "", "v1", "default", "banks", <bank>, [sub, ...]
    if len(parts) < 5 or parts[:4] != ["", "v1", "default", "banks"]:
        return False
    if not _safe(parts[1:]):
        return False
    if parts[4] != bank:  # compared raw: bank ids never need encoding
        return False
    if len(parts) == 5:
        # No DELETE: erasing a whole bank stays with the admin, so a button in a
        # per-person UI can't wipe someone's memory.
        return method in ("GET", "PUT", "PATCH")
    sub = parts[5]
    if sub not in BANK_SUBRESOURCES:
        return False
    if sub in READ_ONLY_SUBRESOURCES and method not in ("GET", "HEAD"):
        return False
    return True
