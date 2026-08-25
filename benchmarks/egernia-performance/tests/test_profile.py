"""Tests for the profile family (package 18).

The family makes two claims, and each has one way of being quietly wrong.

The attribution claim — "this much of a request's CPU is spent in that
subsystem" — is wrong if samples land on the wrong frame. A profiler's raw
output is a tree rooted in uvicorn with stdlib leaves, so attributing to
either end names nothing: every sample would be "the HTTP server" or
"re.match". So the rules are tested against a stack whose right answer is
neither.

The authentication claim — "a verified bearer token costs this much" — is
wrong if the tokens were refused, and a rung of 401s is cheap, fast and looks
like a measurement. So a minted token is verified here by the service's own
``IAMTokenVerifier``, against the same two documents the in-cluster stub
serves, fetched over HTTP the way the pod fetches them.
"""

import dataclasses
import http.server
import inspect
import json
import pathlib
import sys
import threading
import typing

import pytest
import yaml

SUITE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE))

from egernia_bench.collect import oidc, pyspy  # noqa: E402
from egernia_bench.orchestrate import runner  # noqa: E402


def _config():
    return yaml.safe_load((SUITE / "config/scenarios.yaml").read_text())


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

# One stack per line, root first, leaf last, then a sample count — py-spy's
# raw (folded) format. Every one of these is rooted in uvicorn and most end in
# a stdlib leaf, which is the shape the rules have to cope with.
FOLDED = "\n".join(
    [
        # translation: the leaf is stdlib `re`, the answer is the translator
        "run (uvicorn/server.py);run_asgi (uvicorn/protocols/http/h11_impl.py);"
        "app (fastapi/applications.py);prepare_query (egernia_api/queries/query.py);"
        "translate (egernia_core/query/adql.py);to_postgresql (queryparser/adql/adqltranslator.py);"
        "match (re/__init__.py) 400",
        # serialisation: the leaf is stdlib `csv`, the answer is the writer
        "run (uvicorn/server.py);run_asgi (uvicorn/protocols/http/h11_impl.py);"
        "_result_chunks (egernia_api/queries/query.py);"
        "stream_dsv (egernia_core/query/results.py);writerow (csv.py) 300",
        # the row conversion the writers are fed from
        "run (uvicorn/server.py);_result_chunks (egernia_api/queries/query.py);"
        "stream (psycopg/cursor.py);_fetch (psycopg/generators.py) 100",
        # the framework's own dependency solving, below no application frame
        "run (uvicorn/server.py);app (fastapi/applications.py);"
        "solve_dependencies (fastapi/dependencies/utils.py) 100",
        # the HTTP server with nothing of ours above it
        "run (uvicorn/server.py);run_asgi (uvicorn/protocols/http/h11_impl.py);"
        "send (h11/_connection.py) 50",
        # a stack that names no subsystem at all: the residual
        "start (some_vendor_thing/main.py);tick (some_vendor_thing/loop.py) 50",
        # a torn write that lost the frames and kept the count — seen once in
        # the published pass, as a lone `193`
        "100",
    ]
)


def test_a_sample_is_attributed_to_the_innermost_named_subsystem():
    """Not the leaf, and not the root.

    The leaf of the busiest stack here is `re.match` and its root is uvicorn.
    Attributing to either would produce a breakdown that is arithmetically
    right and says nothing an engineer can act on, which is the failure this
    family exists to end.
    """
    buckets, frames, _unattributed = pyspy.attribute(FOLDED)
    assert buckets["adql translation"] == pytest.approx(400 / 1100)
    assert buckets["result writers"] == pytest.approx(300 / 1100)
    assert buckets["psycopg and row conversion"] == pytest.approx(100 / 1100)
    assert buckets["asgi routing and dependencies"] == pytest.approx(100 / 1100)
    assert buckets["http server"] == pytest.approx(50 / 1100)
    # The busiest named frame is the translator, not `re.match`.
    assert frames[0][0].startswith("to_postgresql ")


def test_the_translation_bucket_survives_the_vendored_translator():
    """Package 21 moves the translator into `egernia_core/query/_adql/`.

    A bucket keyed only on `queryparser/` would then empty into
    "egernia (other)" and the breakdown would read as translation having
    vanished — the same class of mistake as attributing the ceiling to
    translation after the fast path removed it.
    """
    vendored = (
        "run (uvicorn/server.py);prepare_query (egernia_api/queries/query.py);"
        "translate (egernia_core/query/_adql/translator.py);"
        "visit (egernia_core/query/_adql/ADQLParser.py) 100"
    )
    buckets, _frames, unattributed = pyspy.attribute(vendored)
    assert buckets == {"adql translation": 1.0}
    assert unattributed == 0.0


def test_stacks_naming_no_subsystem_are_a_reported_residual():
    """The one number that must never be quietly rounded into a bucket.

    "88% of the ceiling is unaccounted for" was the state package 18 started
    from, so a profile that cannot say how much it failed to attribute cannot
    answer it.
    """
    buckets, _frames, unattributed = pyspy.attribute(FOLDED)
    # the stack naming nothing (50) *and* the stackless count (100): both are
    # samples the profiler took, and neither was attributed
    assert unattributed == pytest.approx(150 / 1100)
    assert sum(buckets.values()) + unattributed == pytest.approx(1.0)


def test_application_work_is_separable_from_the_machinery():
    """The application's share, not the server's.

    An 80% attribution made up of the event loop and the HTTP parser would
    answer the package by relabelling rather than by explaining, so the two
    groups are counted apart.
    """
    profile = pyspy.Profile(
        pid=1,
        gil_only=True,
        nonblocking=False,
        rate=100,
        duration_s=10.0,
        samples=1000,
        errors=0,
        path=pathlib.Path("unused"),
        buckets=pyspy.attribute(FOLDED)[0],
        frames=[],
        unattributed=pyspy.attribute(FOLDED)[2],
    )
    assert profile.named_fraction == pytest.approx(1.0 - 150 / 1100)
    # translation + writers + psycopg = 800 of 1100; fastapi, h11, the unnamed
    # stack and the stackless count are not the application's own work.
    assert profile.application_fraction == pytest.approx(800 / 1100)
    assert set(pyspy.APPLICATION_BUCKETS).isdisjoint(
        {"http server", "asgi routing and dependencies", "event loop", "threadpool handoff"}
    )


def test_the_total_comes_from_the_cgroup_and_the_split_from_the_samples():
    """A sampling profiler must not be a source of throughput figures.

    `by_subsystem_ms` has to be the cgroup's CPU per request split by the
    profile's shares. Deriving it from the sample count instead would make
    every subsystem's cost a function of the sampling rate.
    """
    profile = pyspy.Profile(
        pid=1,
        gil_only=True,
        nonblocking=False,
        rate=100,
        duration_s=100.0,
        # Half the window's stacks were idle and not sampled, so the sample
        # count alone would say a request occupies half what it does.
        samples=5_000,
        errors=0,
        path=pathlib.Path("unused"),
        buckets={"adql translation": 0.25, "result writers": 0.75},
        frames=[],
        unattributed=0.0,
    )
    summary = pyspy.summarise(profile, requests=1_000, window_s=100.0, cpu_cores_mean=0.1)
    # 0.1 cores x 100 s / 1000 requests = 10 ms of CPU per request
    assert summary["cgroup_cpu_ms_per_request"] == pytest.approx(10.0)
    assert summary["by_subsystem_ms"]["adql translation"] == pytest.approx(2.5)
    assert summary["by_subsystem_ms"]["result writers"] == pytest.approx(7.5)
    # …and the profiler's own occupancy figure stays separate from it.
    assert summary["profiled_occupancy_ms_per_request"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# The window that is profiled
# ---------------------------------------------------------------------------


def test_the_profiler_covers_the_measured_window_only():
    """py-spy runs for the measured phase, not for the warmup before it.

    `measure()` enters `during_load` inside the load phase, and the profiled
    rungs ask for no warmup — so the samples and the requests they are divided
    by describe the same seconds. A profiled rung with a warmup would divide a
    window's samples by a subset of its requests.
    """
    measure_source = runner.measure.__doc__ or ""
    assert "during_load" in measure_source
    source = inspect.getsource(runner.profile_api_cpu)
    for rung in ("-gil", "-all", "-authgil"):
        assert f'c{{concurrency}}{rung}"' in source, rung
    # every profiled rung: one repetition over the profile window, no warmup
    assert source.count("warmup_s=0.0") == 3
    assert source.count("measure_s=profile_s") == 3
    assert source.count("repetitions=1") == 3


def test_the_authenticated_rungs_are_bracketed_by_unauthenticated_ones():
    """Two helm upgrades and a pod restart separate them from the base rung.

    Without a second unauthenticated rung afterwards, any drift over the run
    would be indistinguishable from the cost of verifying a token — which is
    the whole quantity being measured.
    """
    source = inspect.getsource(runner.profile_api_cpu)
    assert source.index("-base") < source.index("-authverify") < source.index("-noauth")
    report = runner.profile_report(
        [
            _rung("prof-D1-c4-base-r0", rps=100.0, cpu=1.0, authenticated=False),
            _rung("prof-D1-c4-authverify-r0", rps=90.0, cpu=1.0, authenticated=True),
            _rung("prof-D1-c4-noauth-r0", rps=98.0, cpu=1.0, authenticated=False),
        ],
        4,
        {"enabled": True},
    )
    cost = report["authentication_cost"]
    assert cost["unauthenticated_rps"] == pytest.approx(99.0)
    assert cost["authverify"]["throughput_cost_fraction"] == pytest.approx(9.0 / 99.0)


def test_the_profiled_authenticated_rung_is_not_averaged_into_the_gil_rung():
    """`authgil` ends in "gil" and must not be folded into `gil`.

    One is an unauthenticated profile and the other is not; averaging them
    would put the cost of token verification into the unauthenticated
    breakdown, where it does not exist.
    """
    results = [
        _rung("prof-D1-c4-gil-r0", rps=100.0, cpu=1.0, authenticated=False),
        _rung("prof-D1-c4-authgil-r0", rps=90.0, cpu=1.0, authenticated=True),
    ]
    report = runner.profile_report(results, 4, {"enabled": True})
    assert report["rungs"]["gil"]["keys"] == ["prof-D1-c4-gil-r0"]
    assert report["rungs"]["gil"]["authenticated"] is False
    assert report["rungs"]["authgil"]["keys"] == ["prof-D1-c4-authgil-r0"]


def _ladder_rung(concurrency: int, rps: float, p95_ms: float, classification: str) -> dict:
    """The fields choose_profile_concurrency() reads, and nothing else."""
    return {
        "key": f"conc-D1-c{concurrency}-r0",
        "kind": "concurrency",
        "concurrency": concurrency,
        "http": {"rps": rps, "latency": {"p95_s": p95_ms / 1000.0}},
        "bottleneck": [{"classification": classification}],
    }


def _rung_invalid(key: str) -> dict:
    return {"key": key, "kind": "profile", "invalid": True}


def _rung(key: str, *, rps: float, cpu: float, authenticated: bool) -> dict:
    """The fields profile_report() reads, and nothing else."""
    window = 100.0
    return {
        "key": key,
        "kind": "profile",
        "authenticated": authenticated,
        "window_seconds": window,
        "http": {
            "requests": int(rps * window),
            "rps": rps,
            "error_fraction": 0.0,
            "latency": {"p95_s": 0.05},
        },
        "resources": {"tap_api_cpu_cores_mean": cpu},
    }


# ---------------------------------------------------------------------------
# The authenticated rung really authenticates
# ---------------------------------------------------------------------------


class _Documents(http.server.BaseHTTPRequestHandler):
    """The two files the ConfigMap mounts, at the paths it mounts them at."""

    documents: typing.ClassVar[dict[str, str]] = {}

    def do_GET(self):
        body = self.documents.get(self.path)
        if body is None:
            self.send_error(404)
            return
        payload = body.encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@pytest.fixture
def served_issuer():
    """An issuer whose documents are served over HTTP from loopback.

    Served rather than stubbed because the thing being proved is that the
    service's own verifier can complete the whole exchange it does in the
    cluster: fetch discovery, follow `jwks_uri`, find the `kid`, check the
    signature and the claims.
    """
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Documents)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    issuer = oidc.keypair(bits=2048, issuer_url=url)
    _Documents.documents = {
        "/.well-known/openid-configuration": json.dumps(issuer.discovery),
        "/jwks.json": json.dumps(issuer.jwks),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield issuer
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_a_minted_token_verifies_through_the_services_own_verifier(served_issuer):
    """Otherwise the authenticated rung measures the cost of refusing tokens.

    A rung of 401s is faster than a rung of queries, so the mistake would show
    up as authentication making the service *quicker*, or as a small cost that
    looked plausible.
    """
    pytest.importorskip("jwt")
    tokens = pytest.importorskip("egernia_core.auth.tokens")

    verifier = tokens.IAMTokenVerifier(issuer=served_issuer.issuer, audience=served_issuer.audience)
    principal = verifier.verify(served_issuer.mint(subject="s-1"))
    assert principal.subject == "s-1"
    assert oidc.GROUP in principal.groups
    assert oidc.SCOPE in principal.scopes
    # …and a token for another audience is still refused, so the rung is not
    # passing because verification was effectively switched off.
    other = tokens.IAMTokenVerifier(issuer=served_issuer.issuer, audience="somebody-else")
    with pytest.raises(Exception, match="not addressed to this service"):
        other.verify(served_issuer.mint())


def test_the_group_the_tokens_carry_is_the_group_the_chart_grants(served_issuer):
    """The gated rung has to measure a decision that succeeds.

    An authorised rung where every request is denied measures the 403 path,
    and the chart refuses to deploy a gated operation it has no rule for — so
    the policy and the token have to agree, in this one place.
    """
    values = yaml.safe_load((SUITE / "config/auth-values.yaml").read_text())["auth"]
    assert values["iam"]["issuer"] == oidc.ISSUER
    assert values["iam"]["audience"] == oidc.AUDIENCE
    assert values["requireToken"] is True
    # …and anonymous queries stay closed, or /tap/sync would be exempt from
    # the token requirement and the rung would measure nothing.
    assert values["anonymousQueries"] is False
    for operation in runner.GATED_OPERATIONS:
        assert values["roles"][operation]["groups"] == [oidc.GROUP]
    minted = served_issuer.mint()
    import jwt

    claims = jwt.decode(minted, options={"verify_signature": False}, audience=oidc.AUDIENCE)
    assert claims["groups"] == [oidc.GROUP]


def test_the_query_surface_is_gated_whole():
    """The chart and the service both refuse a partial query surface.

    A rung that gated `query.sync` alone would fail to deploy, so the family's
    list is the whole set or the family does not run.
    """
    query_operations = {"jobs.create", "jobs.mutate", "jobs.delete", "query.sync"}
    assert query_operations <= set(runner.GATED_OPERATIONS)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_the_profile_is_taken_at_one_worker_in_one_pod():
    """ "Per request" and "per worker" have to be the same statement.

    The reciprocal of throughput is only interpretable as one request's share
    of an interpreter lock when there is exactly one of them, so the family
    holds replicas and workers at 1 — which is also what the chart values
    already deploy.
    """
    chart = yaml.safe_load((SUITE / "config/chart-values.yaml").read_text())
    assert chart["tapApi"]["replicas"] == 1
    assert chart["tapApi"]["workers"] == 1
    assert 'cluster.scale("tap-api", 1)' in inspect.getsource(runner.profile_api_cpu)


def test_the_profile_window_is_long_enough_for_the_rate_the_sampler_achieves():
    """A share worth naming has to be more than a handful of samples.

    The requested rate is not the achieved one, and most of a short window is
    py-spy's startup: a 30-second nonblocking pass returned 80 samples where a
    600-second one returned 24,400. So the window is sized against the rate the
    sampler actually sustains (~41 Hz), which puts a one-percent share at a
    couple of hundred samples.
    """
    plan = _config()["profile"]
    observed_hz = 41.0
    assert plan["profile_seconds"] * observed_hz >= 10_000
    assert plan["profile_seconds"] >= plan["measure_seconds"]


def test_occupancy_is_not_reported_when_the_sampler_missed_its_rate():
    """The one figure nonblocking sampling cannot support.

    Sample count over sampling rate is a duration only if the sampler kept up.
    At the ~41 Hz it manages of a requested 100 it is out by a factor of two and
    a half, and published as interpreter-lock occupancy per request it would
    have said a request occupies a worker for 4 ms rather than 10.
    """
    starved = pyspy.Profile(
        pid=1,
        gil_only=True,
        nonblocking=True,
        rate=100,
        duration_s=600.0,
        samples=24_400,
        errors=1_200,
        path=pathlib.Path("unused"),
        buckets={"adql translation": 1.0},
        frames=[],
        unattributed=0.0,
    )
    summary = pyspy.summarise(starved, requests=57_000, window_s=600.0, cpu_cores_mean=1.04)
    assert summary["profiled_occupancy_ms_per_request"] is None
    assert "40.7 Hz of the 100 Hz" in summary["occupancy_unavailable_reason"]
    # …while the split, and the total it is applied to, are unaffected
    assert summary["cgroup_cpu_ms_per_request"] == pytest.approx(10.947, abs=0.01)
    assert summary["by_subsystem_ms"]["adql translation"] == pytest.approx(10.947, abs=0.01)

    kept_up = dataclasses.replace(starved, nonblocking=False, samples=60_000, errors=0)
    summary = pyspy.summarise(kept_up, requests=57_000, window_s=600.0, cpu_cores_mean=1.04)
    assert summary["profiled_occupancy_ms_per_request"] == pytest.approx(10.526, abs=0.01)
    assert summary["occupancy_unavailable_reason"] is None


# ---------------------------------------------------------------------------
# Refusals: the two ways a rung can look measured and be meaningless
# ---------------------------------------------------------------------------


def test_the_profiler_is_killed_on_the_way_out():
    """An orphaned py-spy keeps pausing the worker, and that restarts pods.

    It happened: the first blocking pass was interrupted, `sudo`'s child
    outlived the harness, and the worker kept stalling until it was killed by
    hand. So the pass stops the profiler unconditionally on exit — and by the
    output path, because signalling the `sudo` wrapper leaves the profiler
    itself attached.
    """
    exit_source = inspect.getsource(pyspy.Pass.__exit__)
    assert "stop(self.out_path)" in exit_source
    # unconditional: after the join, outside any success branch
    assert exit_source.rindex("stop(self.out_path)") > exit_source.rindex("join(")
    stop_source = inspect.getsource(pyspy.stop)
    assert '"sudo", "-n", "pkill", "-f", str(out_path)' in stop_source


def test_a_rung_is_refused_when_the_pods_do_not_carry_its_policy(monkeypatch):
    """Configuration reaches these pods through a ConfigMap, read at startup.

    So a `helm upgrade` that changes only ConfigMap data succeeds while every
    running pod keeps the old policy — an "authenticated" rung that measured
    the unauthenticated service and reported the difference as zero. The chart
    now hashes the ConfigMap into the pod template; this is the check that the
    rollout actually happened.
    """
    monkeypatch.setattr(runner, "deployed_auth_policy", lambda: {"enabled": False})
    with pytest.raises(RuntimeError, match="the rollout did not happen"):
        runner.require_auth_policy(enabled=True)

    # …and enabled-but-reopened is refused too: with anonymous TAP queries on,
    # /tap/sync is exempt from the token requirement and verifies nothing.
    monkeypatch.setattr(
        runner,
        "deployed_auth_policy",
        lambda: {"enabled": True, "anonymous_tap_queries": True, "gated_operations": {}},
    )
    with pytest.raises(RuntimeError, match="anonymous callers query TAP"):
        runner.require_auth_policy(enabled=True)

    monkeypatch.setattr(
        runner,
        "deployed_auth_policy",
        lambda: {
            "enabled": True,
            "anonymous_tap_queries": False,
            "gated_operations": {"query.sync": "…"},
        },
    )
    # the gated rung wants the whole query surface, not one operation of it
    with pytest.raises(RuntimeError, match="this rung needs"):
        runner.require_auth_policy(enabled=True, gated=runner.GATED_OPERATIONS)
    # and the verify-only rung wants nothing gated
    with pytest.raises(RuntimeError, match="this rung needs"):
        runner.require_auth_policy(enabled=True)


def test_a_profile_that_moved_the_throughput_is_marked_invalid():
    """py-spy pauses the worker to walk its stacks.

    Measured at ~30x slower on an asyncio-heavy process, which would leave a
    breakdown of a worker nobody runs, taken at a concurrency it is no longer
    saturated at. The overhead is compared against the unprofiled rung before
    it and the profile is refused above the configured ceiling.
    """
    plan = _config()["profile"]
    assert 0 < plan["max_overhead_fraction"] <= 0.2
    # Nonblocking by default, and that is a measurement rather than caution:
    # pausing the worker to sample it cost 74% of its throughput and then
    # stalled it past its one-second liveness timeout, so the kubelet restarted
    # a worker that was busy rather than broken. Run
    # 20260825T155319Z-44a69b9c-profile is where those numbers come from.
    assert plan["nonblocking"] is True
    source = inspect.getsource(runner._profile_rung)
    assert "reference_rps" in source
    assert "the profiler cost" in source
    # marked on the measurement too, so profile_report() leaves it out rather
    # than publishing a breakdown of a worker its own profiler slowed down
    assert 'result["invalid"] = True' in source
    assert runner._rung_group([_rung_invalid("prof-D1-c4-gil-r0")], "gil") == []
    # the reference is the neighbouring unprofiled rung, not a published figure
    family = inspect.getsource(runner.profile_api_cpu)
    assert "reference_rps=base_rps" in family
    assert "reference_rps=verify_rps" in family


def test_the_profiled_point_is_the_knee_of_the_ladder():
    """Each of the three wrong answers here was measured, not imagined.

    * The **saturation stop** answers "has the ladder stopped rising?", which
      is what a capacity sweep needs. Asked to *pick* a rung on the short
      windows a selection ladder can afford, it picked c=2 — which tripped
      `throughput_plateau` while c=4 served 10% more.
    * The **busiest rung** is not the ceiling either: the same ladder measured
      95.1 rps at c=4 with p95 66 ms and 95.5 rps at c=8 with p95 131 ms, so
      everything c=8 added was queue, and a profile there would attribute event
      loop and threadpool work that only exists past the knee.
    * An **unsaturated** rung attributes an idle event loop.

    What is left is the knee: the lowest CPU-bound rung reaching the ladder's
    best throughput, where "reaching" is the suite's own threshold for two
    throughputs being the same one.
    """
    # The ladder this family measured on 2026-08-25, rung for rung.
    ladder = [
        _ladder_rung(1, 84.0, 19, "UNKNOWN"),
        _ladder_rung(2, 86.5, 37, "TAP_CPU_BOUND"),
        _ladder_rung(4, 95.1, 66, "TAP_CPU_BOUND"),
        _ladder_rung(8, 95.5, 131, "TAP_CPU_BOUND"),
    ]
    tolerance = _config()["concurrency_sweep"]["signals"]["throughput_gain_below_fraction"]
    chosen, best = runner.choose_profile_concurrency(ladder, tolerance=tolerance)
    assert chosen["concurrency"] == 4, "c=8 is queue; c=2 is below the ceiling"
    assert best == 95.5

    # An unsaturated ladder still yields its fastest point rather than nothing:
    # a profile of a service that never became CPU-bound is a finding too.
    only_unknown = [_ladder_rung(1, 40.0, 10, "DATABASE_IO_BOUND")]
    chosen, _best = runner.choose_profile_concurrency(only_unknown, tolerance=tolerance)
    assert chosen["concurrency"] == 1
    assert runner.choose_profile_concurrency([], tolerance=tolerance) == (None, 0.0)

    # …and the ladder that feeds it climbs to the top rather than stopping at
    # the first plateau, or the knee would never be visible.
    source = inspect.getsource(__import__("egernia_bench.__main__", fromlist=["x"]).cmd_profile)
    assert "stop_on_saturation=False" in source
    assert "refine_saturation=False" in source

    plan = _config()["profile"]
    ladder = plan["ladder"]
    assert ladder == sorted(ladder), "the ladder must climb"
    assert ladder[-1] > 4, (
        "D1 saturates a single worker at four clients, so the ladder has to go"
        " past it: a ladder whose last rung is the chosen one cannot tell a"
        " ceiling from the ladder running out"
    )


def test_the_ladder_climbs_past_a_tripped_stop_when_asked_to():
    """`stop_on_saturation=False` must keep measuring, not just skip the break."""
    source = inspect.getsource(runner.concurrency_sweep)
    assert "if saturated_here and not stop_on_saturation:" in source
    # it has to advance the comparison baseline and go round again, or the next
    # rung would be judged against the rung before the plateau
    stop_block = source.split("if saturated_here and not stop_on_saturation:")[1]
    head = stop_block.split("if saturated_here and not refine_saturation:")[0]
    assert "previous = current" in head
    assert "continue" in head
