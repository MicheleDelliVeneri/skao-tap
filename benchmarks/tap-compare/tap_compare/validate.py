"""The agreement gate: the same query must mean the same thing everywhere.

"Identical logical corpus" is a premise the whole comparison rests on, so it
is verified rather than assumed: a fixed set of queries per class runs once
against every target, and row counts plus an order-independent checksum over
``obs_publisher_did`` must agree. A class that disagrees is excluded from
the comparison for that run — recorded, never silently dropped.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging

import httpx

from . import corpus as corpus_mod

log = logging.getLogger("tap_compare.validate")

#: queries per class in the agreement probe; enough to catch a translation
#: or ingestion difference without turning the gate into a load test
PROBES_PER_CLASS = 5

#: classes whose content legitimately differs across servers: Q01 lists the
#: server's own TAP_SCHEMA, which includes each server's own system tables.
#: The gate only checks these answer successfully.
STATUS_ONLY = {"Q01"}


def deterministic_variant(entry: corpus_mod.CorpusEntry) -> str:
    """The probe query, made order-deterministic.

    A ``TOP N`` over more than N matching rows leaves *which* N rows to the
    server — two conformant servers may return different, equally correct
    subsets. The timed rungs keep the original queries (that freedom is part
    of what a server does); the agreement gate appends an ORDER BY over the
    unique publisher DID so truncation picks the same rows everywhere.
    """
    if entry.query_class in STATUS_ONLY or "GROUP BY" in entry.adql:
        return entry.adql
    return f"{entry.adql} ORDER BY obs_publisher_did"


def fingerprint(csv_text: str) -> dict:
    """Row count and an order-independent checksum of obs_publisher_did.

    Order-independent because TAP imposes no result order without ORDER BY:
    two servers may return the same rows differently ordered, and that is
    conformant. Falls back to hashing whole rows when the projection carries
    no obs_publisher_did (Q01's tap_schema listing).
    """
    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader, [])
    try:
        key_index = [h.strip().lower() for h in header].index("obs_publisher_did")
    except ValueError:
        key_index = None
    keys = []
    for row in reader:
        if not row:
            continue
        keys.append(row[key_index] if key_index is not None else ",".join(row))
    digest = hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()
    return {"rows": len(keys), "sha256": digest}


def probe(base_url: str, adql: str, maxrec: int, timeout_s: float = 120.0) -> dict:
    with httpx.Client(timeout=timeout_s) as client:
        response = client.post(
            f"{base_url}/sync",
            data={
                "LANG": "ADQL",
                "QUERY": adql,
                "RESPONSEFORMAT": "csv",
                "MAXREC": str(maxrec),
            },
        )
    if response.status_code != 200:
        return {"error": f"HTTP {response.status_code}", "body": response.text[:500]}
    return fingerprint(response.text)


def agreement(
    targets: dict[str, str],
    entries: list[corpus_mod.CorpusEntry],
    maxrec: int,
) -> dict:
    """Run the probes against every target; return per-class verdicts.

    ``targets`` maps target name -> TAP base URL. Returns
    {"classes": {cls: {"agrees": bool, "detail": [...]}}, "agreed", "disagreed"}.
    """
    grouped = corpus_mod.by_class(entries)
    verdicts: dict[str, dict] = {}
    for cls, pool in sorted(grouped.items()):
        detail = []
        agrees = True
        for entry in pool[:PROBES_PER_CLASS]:
            adql = deterministic_variant(entry)
            results = {name: probe(url, adql, maxrec) for name, url in targets.items()}
            errored = any("error" in r for r in results.values())
            if cls in STATUS_ONLY:
                same = not errored and all(r.get("rows", 0) > 0 for r in results.values())
            else:
                fingerprints = {
                    name: (r.get("rows"), r.get("sha256")) for name, r in results.items()
                }
                same = len(set(fingerprints.values())) == 1 and not errored
            if not same:
                agrees = False
            detail.append({"query_id": entry.query_id, "same": same, "results": results})
        verdicts[cls] = {"agrees": agrees, "detail": detail}
        log.info("agreement %s: %s", cls, "ok" if agrees else "DISAGREES")
    return {
        "classes": verdicts,
        "agreed": sorted(c for c, v in verdicts.items() if v["agrees"]),
        "disagreed": sorted(c for c, v in verdicts.items() if not v["agrees"]),
    }
