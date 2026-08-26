"""The ADQL query corpus.

Fourteen parameterised classes and a deterministic corpus of parameter
combinations. Deterministic matters twice over: two runs have to issue the
same queries to be comparable at all, and the corpus is hashed into the
provenance so a result can never be quietly compared against a different
workload.

The parameters are drawn from the *generated data*, not from thin air. The
generator derives every value from a hash of (seed, row index, field), and
that hash is reimplemented here — so the corpus can aim a cone search at a
position where objects actually are without querying the database first. A
corpus of coordinates chosen independently of the data would spend its time
measuring empty results, and a corpus of one fixed coordinate would measure
the page cache.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import typing

# ---------------------------------------------------------------------------
# The generator's PRNG, on this side of the wire
# ---------------------------------------------------------------------------


def rnd(seed: str, index: int, salt: str) -> float:
    """bench.rnd() from generate.sql, in Python.

    Identical by construction: the same md5, the same 32-bit slice, the same
    mask. The mask is what makes the sign of PostgreSQL's bit(32)::bigint cast
    irrelevant — the low 31 bits are the same either way.
    """
    digest = hashlib.md5(f"{seed}:{index}:{salt}".encode()).hexdigest()
    return (int(digest[:8], 16) & 0x7FFFFFFF) / 2147483648.0


def object_position(seed: str, index: int) -> tuple[float, float]:
    """Where observation `index` actually points."""
    ra = 360.0 * rnd(seed, index, "ra")
    dec = math.degrees(math.asin(2.0 * rnd(seed, index, "dec") - 1.0))
    return ra, dec


class Deterministic:
    """A small counter-based PRNG.

    Not `random.Random`: this has to produce the same corpus on any Python
    build for the corpus hash to be a meaningful part of the provenance, and
    the stdlib generator's stream is an implementation detail rather than a
    promise.
    """

    def __init__(self, seed: int | str) -> None:
        self._seed = str(seed)
        self._counter = 0

    def _next(self) -> float:
        self._counter += 1
        return rnd(self._seed, self._counter, "corpus")

    def uniform(self, lo: float, hi: float) -> float:
        return lo + (hi - lo) * self._next()

    def randint(self, lo: float, hi: float) -> int:
        return int(lo + math.floor(self._next() * (hi - lo + 1)))

    def choice(self, items: typing.Sequence):
        return items[self.randint(0, len(items) - 1)]


# ---------------------------------------------------------------------------
# Query classes
# ---------------------------------------------------------------------------

OBSCORE_COLUMNS = (
    "obs_publisher_did, obs_id, obs_collection, dataproduct_type, "
    "calib_level, s_ra, s_dec, s_fov, t_min, t_max, access_url"
)


@dataclasses.dataclass(frozen=True)
class QueryClass:
    id: str
    name: str
    template: str
    stress: bool = False

    def render(self, params: dict) -> str:
        return " ".join(self.template.format(**params).split())


# What each class exercises is documented in the README's corpus table.
CLASSES: dict[str, QueryClass] = {
    "Q01": QueryClass(
        "Q01",
        "TAP_SCHEMA metadata",
        "SELECT table_name, description FROM tap_schema.tables",
    ),
    "Q02": QueryClass(
        "Q02",
        "indexed identifier lookup",
        f"SELECT {OBSCORE_COLUMNS} FROM ivoa.obscore WHERE obs_id = '{{obs_id}}'",
    ),
    "Q03": QueryClass(
        "Q03",
        "indexed categorical filter",
        "SELECT TOP 100 " + OBSCORE_COLUMNS + " FROM ivoa.obscore "
        "WHERE obs_collection = '{collection}' AND dataproduct_type = '{dptype}'",
    ),
    "Q04": QueryClass(
        "Q04",
        "temporal range",
        "SELECT TOP 200 " + OBSCORE_COLUMNS + " FROM ivoa.obscore "
        "WHERE t_min > {t_lo} AND t_max < {t_hi}",
    ),
    "Q05": QueryClass(
        "Q05",
        "small cone search",
        "SELECT TOP 100 " + OBSCORE_COLUMNS + " FROM ivoa.obscore "
        "WHERE 1 = CONTAINS(POINT('ICRS', s_ra, s_dec), "
        "CIRCLE('ICRS', {ra}, {dec}, {radius}))",
    ),
    "Q06": QueryClass(
        "Q06",
        "medium cone search",
        "SELECT TOP 500 " + OBSCORE_COLUMNS + " FROM ivoa.obscore "
        "WHERE 1 = CONTAINS(POINT('ICRS', s_ra, s_dec), "
        "CIRCLE('ICRS', {ra}, {dec}, {radius}))",
    ),
    "Q07": QueryClass(
        "Q07",
        "spatial + temporal + metadata",
        "SELECT TOP 200 " + OBSCORE_COLUMNS + " FROM ivoa.obscore "
        "WHERE 1 = CONTAINS(POINT('ICRS', s_ra, s_dec), "
        "CIRCLE('ICRS', {ra}, {dec}, {radius})) "
        "AND t_min > {t_lo} AND calib_level >= {calib}",
    ),
    "Q08": QueryClass(
        "Q08",
        "Observation-to-Plane join",
        "SELECT TOP 200 o.obs_id, o.collection, o.instrument_name, "
        "p.plane_id, p.calib_level, p.data_product_type "
        "FROM caom.observation AS o JOIN caom.plane AS p ON o.obs_id = p.obs_id "
        "WHERE o.collection = '{collection}' AND p.calib_level = {calib}",
    ),
    "Q09": QueryClass(
        "Q09",
        "four-level CAOM join",
        "SELECT TOP 500 o.obs_id, p.plane_id, a.artifact_id, pt.part_id "
        "FROM caom.observation AS o "
        "JOIN caom.plane AS p ON o.obs_id = p.obs_id "
        "JOIN caom.artifact AS a ON p.plane_id = a.plane_id "
        "JOIN caom.part AS pt ON a.artifact_id = pt.artifact_id "
        "WHERE o.instrument_name = '{instrument}' AND p.calib_level = {calib}",
        stress=True,
    ),
    "Q10": QueryClass(
        "Q10",
        "thousand-row result",
        "SELECT TOP 1000 " + OBSCORE_COLUMNS + " FROM ivoa.obscore "
        "WHERE obs_collection = '{collection}' AND t_min > {t_lo}",
    ),
    "Q11": QueryClass(
        "Q11",
        "ten-thousand-row result",
        "SELECT TOP 10000 " + OBSCORE_COLUMNS + " FROM ivoa.obscore "
        "WHERE obs_collection = '{collection}'",
        stress=True,
    ),
    "Q12": QueryClass(
        "Q12",
        "empty spatial result",
        "SELECT TOP 100 " + OBSCORE_COLUMNS + " FROM ivoa.obscore "
        "WHERE 1 = CONTAINS(POINT('ICRS', s_ra, s_dec), "
        "CIRCLE('ICRS', {ra}, {dec}, 0.0002))",
    ),
    "Q13": QueryClass(
        "Q13",
        "aggregation and grouping",
        "SELECT obs_collection, dataproduct_type, COUNT(*) AS n, "
        "MIN(t_min) AS first_obs, MAX(t_max) AS last_obs "
        "FROM ivoa.obscore WHERE calib_level >= {calib} "
        "GROUP BY obs_collection, dataproduct_type",
        stress=True,
    ),
    "Q14": QueryClass(
        "Q14",
        "deliberately expensive",
        "SELECT TOP 2000 o.obs_id, o.target_name, p.plane_id, c.chunk_id "
        "FROM caom.observation AS o "
        "JOIN caom.plane AS p ON o.obs_id = p.obs_id "
        "JOIN caom.artifact AS a ON p.plane_id = a.plane_id "
        "JOIN caom.part AS pt ON a.artifact_id = pt.artifact_id "
        "JOIN caom.chunk AS c ON pt.part_id = c.part_id "
        "WHERE o.target_name LIKE '{target_prefix}%' "
        "ORDER BY o.obs_id, p.plane_id, c.chunk_id",
        stress=True,
    ),
}


def _parameters(rng: Deterministic, cls: str, seed: str, observations: int, cfg: dict) -> dict:
    gen = cfg["generation"]
    mjd_lo, mjd_hi = gen["mjd_range"]
    span = mjd_hi - mjd_lo

    # Cone centres: mostly aimed at a real object, some deliberately not. A
    # corpus that only ever aims at objects never measures the empty path, and
    # one that never does measures nothing but it.
    def cone_centre() -> tuple[float, float]:
        if rng._next() < 0.8:
            return object_position(seed, rng.randint(1, max(observations, 1)))
        return rng.uniform(0.0, 360.0), math.degrees(math.asin(rng.uniform(-1.0, 1.0)))

    if cls == "Q01":
        return {}
    if cls == "Q02":
        return {"obs_id": f"ska:obs:{rng.randint(1, max(observations, 1)):012d}"}
    if cls in ("Q03", "Q11"):
        return {
            "collection": rng.choice(gen["collections"]),
            "dptype": rng.choice(gen["dataproduct_types"]),
        }
    if cls == "Q04":
        t_lo = rng.uniform(mjd_lo, mjd_hi - span * 0.02)
        return {"t_lo": round(t_lo, 4), "t_hi": round(t_lo + span * 0.02, 4)}
    if cls == "Q05":
        ra, dec = cone_centre()
        return {
            "ra": round(ra, 5),
            "dec": round(dec, 5),
            "radius": round(rng.uniform(0.05, 0.25), 5),
        }
    if cls == "Q12":
        # Never aimed at an object: this class exists to measure the cost of
        # finding nothing, and a centre drawn from the data would sometimes
        # find something and quietly turn it into a second small-cone class.
        return {
            "ra": round(rng.uniform(0.0, 360.0), 5),
            "dec": round(math.degrees(math.asin(rng.uniform(-1.0, 1.0))), 5),
        }
    if cls == "Q06":
        ra, dec = cone_centre()
        return {"ra": round(ra, 5), "dec": round(dec, 5), "radius": round(rng.uniform(1.0, 3.0), 5)}
    if cls == "Q07":
        ra, dec = cone_centre()
        return {
            "ra": round(ra, 5),
            "dec": round(dec, 5),
            "radius": round(rng.uniform(0.5, 2.0), 5),
            "t_lo": round(rng.uniform(mjd_lo, mjd_hi - span * 0.1), 4),
            "calib": rng.randint(0, 2),
        }
    if cls in ("Q08", "Q09"):
        return {
            "collection": rng.choice(gen["collections"]),
            "instrument": rng.choice(gen["instruments"]),
            "calib": rng.randint(0, 3),
        }
    if cls == "Q10":
        return {
            "collection": rng.choice(gen["collections"]),
            "t_lo": round(rng.uniform(mjd_lo, mjd_hi - span * 0.5), 4),
        }
    if cls == "Q13":
        return {"calib": rng.randint(0, 3)}
    if cls == "Q14":
        return {"target_prefix": f"FIELD-{rng.randint(1, 40)}"}
    raise KeyError(cls)


@dataclasses.dataclass(frozen=True)
class CorpusEntry:
    query_id: str  # stable identity of this exact query text
    query_class: str
    adql: str

    def as_dict(self) -> dict:
        return {"query_id": self.query_id, "query_class": self.query_class, "adql": self.adql}


def build(cfg: dict, dataset_cfg: dict, observations: int) -> list[CorpusEntry]:
    """A deterministic corpus spanning every class.

    Every class gets an equal share of the combinations; the *mix* decides how
    often each is issued at run time. Keeping those two separate means the
    weights can change without changing the queries, so a re-weighted run is
    still comparable query-for-query.
    """
    seed = str(dataset_cfg["generation"]["seed"])
    target = cfg["corpus"]["combinations"]
    rng = Deterministic(cfg["corpus"]["seed"])
    seen: dict[str, set[str]] = {cls: set() for cls in CLASSES}
    entries: list[CorpusEntry] = []

    def draw(cls: str) -> bool:
        """One attempt; True if it produced a combination not seen before."""
        params = _parameters(rng, cls, seed, observations, dataset_cfg)
        adql = CLASSES[cls].render(params)
        if adql in seen[cls]:
            return False
        seen[cls].add(adql)
        entries.append(
            CorpusEntry(
                query_id=hashlib.sha256(adql.encode()).hexdigest()[:16],
                query_class=cls,
                adql=adql,
            )
        )
        return True

    # An equal share first, so no class is under-represented...
    share = max(1, target // len(CLASSES))
    for cls in CLASSES:
        for _ in range(share):
            draw(cls)

    # ...then top up from the classes that can still produce new combinations.
    # Several classes are bounded by their own parameter space — Q01 has no
    # parameters at all, Q13 has four — so an equal share alone lands well
    # short of the target. Rather than pad the corpus with repeats, the
    # remainder goes to the classes with room, and a class is dropped from the
    # rotation once it stops yielding anything new.
    exhausted: set[str] = set()
    while len(entries) < target and len(exhausted) < len(CLASSES):
        for cls in CLASSES:
            if cls in exhausted or len(entries) >= target:
                continue
            misses = 0
            while misses < 64:
                if draw(cls):
                    break
                misses += 1
            else:
                exhausted.add(cls)
    return entries


def corpus_hash(entries: list[CorpusEntry]) -> str:
    payload = json.dumps([e.as_dict() for e in entries], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def by_class(entries: list[CorpusEntry]) -> dict[str, list[CorpusEntry]]:
    grouped: dict[str, list[CorpusEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.query_class, []).append(entry)
    return grouped
