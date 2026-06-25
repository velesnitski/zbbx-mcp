"""Pure helpers for triaging a free-text alert line against Zabbix ground truth.

Two lessons from dogfooding an AI Slack alert-feed by hand (tasks 164/165)
shape this module:

  (a) The feed's STATE is never trustworthy — it lags Zabbix in both
      directions (stale "RESOLVED" over live incidents; "REAL PROBLEM" over
      already-recovered triggers). So the tool must always re-query; nothing
      here decides a verdict from the feed's claimed state alone.
  (b) The alert host-name is not the Zabbix host object — protocol/probe
      triggers embed the server in the trigger text, domain checks live in a
      Web-Check group, some names don't resolve at all. So host resolution is
      a first-class, fallible step that returns AMBIGUOUS rather than guess.

Everything here is pure (no I/O) so it is unit-tested without a server; the
async orchestration lives in ``tools/triage.py``.
"""

import re

__all__ = [
    "parse_alert_line",
    "extract_host_candidates",
    "detect_severity",
    "detect_state",
    "classify_match",
    "classify_host_triage",
    "top_severity_label",
]

_SEVERITIES = ("disaster", "high", "average", "warning", "information", "info")
_SEV_RANK = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
}
_SEV_LABEL = {
    0: "Not classified", 1: "Information", 2: "Warning",
    3: "Average", 4: "High", 5: "Disaster",
}

# A host-like token: alphanumerics joined by hyphens/dots (node-eu-a1,
# example.com, db7.local, api-3.dc.example.net).
_HOST_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)+")
# A bare short host (db1, db14) — no hyphen/dot. Kept separate so we can drop
# the ones that are really multi-VIP suffixes (the "bb2" in "…-a1 bb2").
_BARE_HOST_RE = re.compile(r"\b[a-z]{2,4}\d+\b")
_WS_RE = re.compile(r"\s+")


def detect_severity(text):
    """Return the first Zabbix severity word in the line (capitalised), or None."""
    low = (text or "").lower()
    for s in _SEVERITIES:
        if re.search(rf"\b{s}\b", low):
            return "Information" if s == "info" else s.capitalize()
    return None


def detect_state(text):
    """Best-effort read of the feed's CLAIMED state — advisory only.

    Returns 'resolved' | 'problem' | None. The tool re-queries Zabbix
    regardless (lesson a), so this never drives the verdict.
    """
    low = (text or "").lower()
    if any(w in low for w in ("resolved", "recovered", "cleared", "✅", "🟢")):
        return "resolved"
    if any(w in low for w in ("real problem", "🔴", "🚨", "firing", "problem")):
        return "problem"
    return None


def extract_host_candidates(text):
    """Pull host/domain-looking tokens out of a free-text alert line.

    Liberal on candidates (the resolver decides and can say AMBIGUOUS), but
    drops a bare token that immediately follows a hyphen-host + space — that
    is a multi-VIP suffix (``node-eu-a1 bb2``), not its own host. Order is
    preserved and duplicates removed.
    """
    text = text or ""
    hspans = [(m.start(), m.end()) for m in _HOST_TOKEN_RE.finditer(text)]
    spans = list(hspans)
    for m in _BARE_HOST_RE.finditer(text):
        s, e = m.start(), m.end()
        # Inside a hyphen-host token ("br3" within "node-eu-br3") — skip.
        if any(hs <= s and e <= he for hs, he in hspans):
            continue
        # Multi-VIP suffix ("bb2" right after "…-a1 ") — skip.
        if s > 0 and text[s - 1] == " " and any(he == s - 1 for _, he in hspans):
            continue
        spans.append((s, e))
    out, seen = [], set()
    for start, end in sorted(spans):
        tok = text[start:end].strip(".,;:|()[]{}")
        key = tok.lower()
        if tok and key not in seen:
            seen.add(key)
            out.append(tok)
    return out


def parse_alert_line(text):
    """Parse one alert line into {severity, claimed_state, hosts, trigger}."""
    text = (text or "").strip()
    return {
        "severity": detect_severity(text),
        "claimed_state": detect_state(text),
        "hosts": extract_host_candidates(text),
        "trigger": _WS_RE.sub(" ", text),
    }


def classify_match(candidate, found_hosts):
    """Decide how a candidate resolved, given host.get results (pure).

    EXACT (one host whose ``host`` equals the candidate) wins even amid fuzzy
    extras; one fuzzy hit is FUZZY; several with no unique exact is AMBIGUOUS
    (never guessed); none is NOT_FOUND. Returns {status, chosen, candidates}.
    """
    found = found_hosts or []
    if not found:
        return {"status": "NOT_FOUND", "chosen": None, "candidates": []}
    exact = [h for h in found if (h.get("host") or "").lower() == candidate.lower()]
    if len(exact) == 1:
        return {"status": "EXACT", "chosen": exact[0], "candidates": found}
    if len(found) == 1:
        return {"status": "FUZZY", "chosen": found[0], "candidates": found}
    return {"status": "AMBIGUOUS", "chosen": None, "candidates": exact or found}


def top_severity_label(problems):
    """Highest-severity label across a problem list."""
    rank = max((_SEV_RANK.get(str(p.get("severity", "0")), 0) for p in problems), default=0)
    return _SEV_LABEL[rank]


def classify_host_triage(active_problems, is_symptom):
    """Verdict for one resolved host from its CURRENT Zabbix state (pure).

    ``active_problems`` are this host's non-suppressed problems right now;
    ``is_symptom`` means every one of them is a dependency symptom of another
    firing trigger. Returns (verdict, action).
    """
    if not active_problems:
        return (
            "recovered",
            "No active problem on this host in Zabbix now — feed is stale; no action.",
        )
    if is_symptom:
        return (
            "symptom_of_cluster",
            f"{len(active_problems)} active, but firing as a dependency symptom — "
            "triage the root trigger / cluster, not this host.",
        )
    return (
        "real_now",
        f"Confirmed active in Zabbix: {len(active_problems)} problem(s), top severity "
        f"{top_severity_label(active_problems)}. Investigate (run diagnose_host for depth).",
    )
