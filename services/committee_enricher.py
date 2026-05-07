"""Daily committee enrichment — civic-literacy MVP Phase 3.F.3.

Pulls the canonical `committees-current.yaml` and
`committee-membership-current.yaml` from
`unitedstates/congress-legislators` and writes the resulting committee
lists directly into `politician_profiles.committees`.

Companion to `scripts/scrape_committees.py` (the developer tool that
mutates the CSV). This module is the in-process equivalent: same
indexing logic, but updates the DB directly rather than touching the
CSV. The scheduler in `app/main.py` calls `refresh_committees()` once
per cycle — daily by default. The CSV-based script stays for one-off
manual refreshes during development or quarterly snapshot updates.

No API key. No rate limit. ~30s per cycle (two YAML downloads + a few
hundred per-row UPDATEs).
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

import asyncpg
import yaml

logger = logging.getLogger("sift-api.committee_enricher")

UNITEDSTATES_BASE = (
    "https://raw.githubusercontent.com/"
    "unitedstates/congress-legislators/main"
)
COMMITTEES_URL = f"{UNITEDSTATES_BASE}/committees-current.yaml"
MEMBERSHIP_URL = f"{UNITEDSTATES_BASE}/committee-membership-current.yaml"

# Display-form prefix strippers — applied in order, longest match first
# (mirrors scripts/scrape_committees.py exactly so the two paths produce
# identical committee names).
PREFIXES_TO_STRIP = [
    "United States Senate Committee on the ",
    "United States Senate Committee on ",
    "United States House Committee on the ",
    "United States House Committee on ",
    "Senate Committee on the ",
    "Senate Committee on ",
    "House Committee on the ",
    "House Committee on ",
    "Joint Committee of Congress on the ",
    "Joint Committee of Congress on ",
    "Joint Committee on the ",
    "Joint Committee on ",
]

DEFAULT_TIMEOUT_SECONDS = 30


def _strip_prefix(name: str) -> str:
    """Return the display form of a committee name (no chamber prefix)."""
    n = name.strip()
    for prefix in PREFIXES_TO_STRIP:
        if n.startswith(prefix):
            n = n[len(prefix):].strip()
            break
    if n.lower().startswith("the "):
        n = n[4:].strip()
    return n


def _http_get_yaml(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    """Synchronous YAML fetch + parse. Used inside an asyncio.to_thread."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "sift-civic-literacy/1.0 (contact: kristenmartino on GitHub)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return yaml.safe_load(body)


def build_bioguide_to_committees(
    committees: list[dict[str, Any]] | None,
    membership: dict[str, Any] | None,
) -> dict[str, list[str]]:
    """Index `{bioguide_id → [display committee name, ...]}` from the two
    parsed YAML files. Top-level committees only — subcommittee
    thomas_ids are silently dropped because they're not present in the
    `committees-current` source.
    """
    if not isinstance(committees, list) or not isinstance(membership, dict):
        return {}

    name_by_thomas_id: dict[str, str] = {}
    for committee in committees:
        if not isinstance(committee, dict):
            continue
        thomas_id = committee.get("thomas_id")
        name = committee.get("name")
        if not thomas_id or not name:
            continue
        name_by_thomas_id[str(thomas_id)] = _strip_prefix(str(name))

    out: dict[str, list[str]] = {}
    for thomas_id, members in membership.items():
        if thomas_id not in name_by_thomas_id:
            continue
        committee_name = name_by_thomas_id[thomas_id]
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            bioguide = member.get("bioguide")
            if not isinstance(bioguide, str) or not bioguide.strip():
                continue
            bioguide = bioguide.strip()
            out.setdefault(bioguide, [])
            if committee_name not in out[bioguide]:
                out[bioguide].append(committee_name)

    for bioguide in out:
        out[bioguide].sort()

    return out


async def refresh_committees(pool: asyncpg.Pool) -> dict[str, int]:
    """Fetch the latest committee data + UPDATE politician_profiles rows.

    Returns a stats dict:
      {
        "indexed": <total bioguides found in source data>,
        "updated": <politician_profiles rows whose committees changed>,
        "unchanged": <rows whose committees already matched>,
        "missing_in_source": <politician_profiles rows not found in source>,
      }

    Tolerant of: network failure, missing politician_profiles table,
    missing pyyaml. On any unrecoverable error, returns counts of zero
    and logs at WARN — caller (the scheduler) treats this as a soft
    failure and continues.
    """
    stats = {"indexed": 0, "updated": 0, "unchanged": 0, "missing_in_source": 0}

    import asyncio

    try:
        committees_yaml, membership_yaml = await asyncio.gather(
            asyncio.to_thread(_http_get_yaml, COMMITTEES_URL),
            asyncio.to_thread(_http_get_yaml, MEMBERSHIP_URL),
        )
    except Exception as e:
        logger.warning("committee_enricher: YAML fetch failed: %s", e)
        return stats

    index = build_bioguide_to_committees(committees_yaml, membership_yaml)
    stats["indexed"] = len(index)
    if not index:
        logger.info("committee_enricher: empty index from source — no updates.")
        return stats

    try:
        rows = await pool.fetch("SELECT bioguide_id, committees FROM politician_profiles")
    except asyncpg.UndefinedTableError:
        logger.info(
            "committee_enricher: politician_profiles table missing — "
            "schedule will retry on next cycle.",
        )
        return stats
    except Exception as e:
        logger.warning("committee_enricher: SELECT failed: %s", e)
        return stats

    for row in rows:
        bid = row["bioguide_id"]
        new_committees = index.get(bid)
        if new_committees is None:
            stats["missing_in_source"] += 1
            continue

        # Compare against existing JSONB to skip no-op UPDATEs.
        existing = row["committees"]
        # asyncpg returns JSONB as already-parsed Python lists.
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except json.JSONDecodeError:
                existing = []
        if existing == new_committees:
            stats["unchanged"] += 1
            continue

        try:
            await pool.execute(
                "UPDATE politician_profiles SET committees = $1::jsonb, "
                "refreshed_at = NOW() WHERE bioguide_id = $2",
                json.dumps(new_committees),
                bid,
            )
            stats["updated"] += 1
        except Exception as e:
            logger.warning(
                "committee_enricher: UPDATE failed for %s: %s", bid, e,
            )

    logger.info(
        "committee_enricher: indexed=%d updated=%d unchanged=%d missing=%d",
        stats["indexed"], stats["updated"], stats["unchanged"], stats["missing_in_source"],
    )
    return stats
