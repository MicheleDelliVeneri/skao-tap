"""Autoscaling timings: T0 through T8, and what the gaps between them mean.

A single "scale-out took 47 seconds" is almost useless, because the fix for 47
seconds spent waiting for the scaler to notice is nothing like the fix for 47
seconds spent pulling an image. So the interval is cut at every point where
responsibility changes hands:

    T0  the load changed
    T1  the scaler's own metric crossed its threshold
    T2  the HPA asked for a different number of replicas
    T3  a Pod object existed
    T4  it was scheduled to a node
    T5  its container started
    T6  it reported Ready
    T7  it served traffic
    T8  latency was back inside the SLO

Each stage is read from the party responsible for it — KEDA's metric for T1,
the HPA's status for T2, the pod's own conditions for T3-T6, the request
samples for T7-T8 — rather than inferred from one clock.

Where a stage cannot be established the result is None and the reason is
recorded. A missing timing is a gap in the evidence; a guessed one is a wrong
answer that looks like evidence.
"""

from __future__ import annotations

import itertools
import logging
import typing

import numpy as np

log = logging.getLogger("tapbench.keda")


def _series(rows: list[dict], metric: str) -> list[tuple[float, float]]:
    return sorted((r["t"], r["value"]) for r in rows if r["metric"] == metric)


def scaling_up(steps: list[dict]) -> bool:
    """Whether the transition at the first step boundary raises the rate.

    A recovery scenario is the mirror of a scale-out: its T1 is the scaler
    metric falling back *through* the threshold, not crossing it. Read from the
    profile rather than assumed, because assuming a scale-out gave the
    4xC1-to-0.2xC1 scenario a two-second "detection" — the metric was already
    above the threshold from the phase before the transition, so the first
    crossing after it was found immediately and meant nothing.
    """
    if len(steps) < 2:
        return True
    before = steps[0].get("rate_end") or steps[0].get("rate") or 0.0
    after = steps[1].get("rate") or 0.0
    return after >= before


def _series_step(rows: list[dict]) -> float:
    """The Prometheus range step these rows were read at.

    Measured from the rows rather than assumed, because it is the resolution
    every stamp taken from a series inherits — a stage cannot be timed finer
    than the step it was sampled at.
    """
    stamps = sorted({r["t"] for r in rows})
    if len(stamps) < 2:
        return 1.0
    return float(np.median(np.diff(stamps)))


def _pod_stamps_from_watcher(
    watcher_samples: list[dict], t0: float, deployment: str
) -> tuple[float | None, float | None]:
    """Creation and first-Ready of the earliest pod created after `t0`.

    From the watcher's own polling, which is all that survives a pod deleted
    before the run ended. Ready is the first sample that reports it, so it is
    late by up to one polling interval — recorded as such by the caller rather
    than presented as the Pod's own stamp.
    """
    component = deployment.split("skao-tap-")[-1]
    created: dict[str, float] = {}
    ready_at: dict[str, float] = {}
    for sample in watcher_samples:
        for pod in sample.get("pods") or []:
            name = pod.get("name") or ""
            if component not in name or not pod.get("created"):
                continue
            created.setdefault(name, pod["created"])
            if pod.get("ready") and name not in ready_at:
                ready_at[name] = sample["t"]
    candidates = sorted((t, n) for n, t in created.items() if t >= t0)
    if not candidates:
        return None, None
    birth, name = candidates[0]
    return birth, ready_at.get(name)


def _first_after(
    points: typing.Sequence[tuple[float, float]], t0: float, predicate
) -> float | None:
    for timestamp, value in points:
        if timestamp >= t0 and predicate(value):
            return timestamp
    return None


def rolling_percentile(
    samples, percentile: float, window_s: float = 10.0
) -> list[tuple[float, float]]:
    """Percentile per fixed window, on request completion time.

    Bucketed by when a request *finished*, because that is when its latency
    became known; bucketing by start time would credit a slow request to the
    moment the system was still healthy.
    """
    if not samples:
        return []
    finished = np.asarray([s.t_start + s.latency_s for s in samples])
    latencies = np.asarray([s.latency_s for s in samples])
    order = np.argsort(finished)
    finished, latencies = finished[order], latencies[order]
    start, end = finished[0], finished[-1]
    out = []
    edge = start
    while edge < end:
        mask = (finished >= edge) & (finished < edge + window_s)
        if mask.sum() >= 5:  # below this a percentile is a single sample
            out.append((edge + window_s / 2, float(np.percentile(latencies[mask], percentile))))
        edge += window_s
    return out


def rolling_rate(
    samples, window_s: float = 10.0, *, successful_only: bool = False
) -> list[tuple[float, float]]:
    if not samples:
        return []
    chosen = [s for s in samples if not successful_only or (200 <= s.status < 300 and not s.error)]
    if not chosen:
        return []
    finished = np.sort(np.asarray([s.t_start + s.latency_s for s in chosen]))
    start, end = finished[0], finished[-1]
    out = []
    edge = start
    while edge < end:
        count = int(((finished >= edge) & (finished < edge + window_s)).sum())
        out.append((edge + window_s / 2, count / window_s))
        edge += window_s
    return out


def timings(
    *,
    t0: float,
    metrics_rows: list[dict],
    watcher_samples: list[dict],
    pod_timings: list[dict],
    samples,
    deployment: str,
    threshold: float,
    slo_p95_s: float,
    scale_up: bool = True,
) -> dict:
    """The nine stamps and the seven latencies derived from them."""
    notes: list[str] = []

    # T1 — the scaler's metric crossed the threshold it is compared against.
    scaler_values = _series(metrics_rows, "keda_scaler_metrics_value")
    if not scaler_values:
        # Non-KEDA (CPU HPA) scenarios have no such series; fall back to the
        # service's own queue-depth gauge, which is the same quantity KEDA
        # reads — the threshold is denominated in queued jobs, so the age
        # gauge would be the wrong unit here.
        scaler_values = _series(metrics_rows, "tap_jobs_queued")
        if scaler_values:
            notes.append(
                'T1 from tap_jobs{phase="QUEUED"}: KEDA published '
                "no keda_scaler_metrics_value for this window"
            )
    t1 = _first_after(
        scaler_values,
        t0,
        (lambda v: v > threshold) if scale_up else (lambda v: v < threshold),
    )
    if t1 is None and scaler_values:
        notes.append(f"the scaler metric never crossed {threshold}")

    # T2 — the HPA changed what it was asking for. Taken from the watcher's
    # direct polling rather than kube-state-metrics: the same 2 s cadence, but
    # it also carries the reason, and a scale decision without its reason is
    # not diagnosable.
    t2 = None
    baseline_spec = None
    for sample in watcher_samples:
        entry = (sample.get("deployments") or {}).get(deployment)
        if not entry:
            continue
        spec = entry.get("spec_replicas")
        if sample["t"] < t0:
            baseline_spec = spec
            continue
        if baseline_spec is None:
            baseline_spec = spec
        if spec is not None and spec != baseline_spec:
            t2 = sample["t"]
            break
    if t2 is None:
        notes.append("no replica-count change was observed after the transition")

    # The stages describe one scale-out. Where the autoscaler moved more than
    # once, T1 is the first threshold crossing after the transition while the
    # Pod stamps belong to whichever pod appeared first, and the two need not
    # be from the same cycle — which is how a `hpa_decision` of -90s arises on
    # a scenario that scaled up, down and up again. Said plainly here rather
    # than left for a reader to infer from a stage that looks merely odd.
    after = [
        s
        for s in watcher_samples
        if s["t"] >= t0 and (s.get("deployments") or {}).get(deployment, {}).get("spec_replicas")
    ]
    counts = [s["deployments"][deployment]["spec_replicas"] for s in after]
    moves = sum(1 for a, b in itertools.pairwise(counts) if a != b)
    if moves > 1:
        notes.append(
            f"the autoscaler moved {moves} times after the transition; these stages "
            "describe a single scaling transition, so any stage pairing a series "
            "stamp with a Pod stamp may be pairing different cycles"
        )

    # T3-T6 — the new pod's own lifecycle. "New" means created after the load
    # changed; on a scale-down there is none, and these stay None.
    new_pods = [
        p
        for p in pod_timings
        if p.get("created") and p["created"] >= t0 and p["component"] in deployment
    ]
    new_pods.sort(key=lambda p: p["created"])
    first = new_pods[0] if new_pods else None
    t3 = first["created"] if first else None
    t4 = first.get("scheduled") if first else None
    t5 = first.get("container_started") if first else None
    t6 = first.get("ready") if first else None

    # The Pod lifecycle above is read at the end of the run, so a scenario that
    # scaled up and back down again has had its evidence deleted: the pods that
    # served the scale-out no longer exist to be asked. The watcher saw them
    # while they lived, at its own 2 s cadence — coarser than the Pod's own
    # stamps, and the only thing left for exactly the flapping scenarios whose
    # scale-out timing is most worth having.
    if first is None:
        t3, t6 = _pod_stamps_from_watcher(watcher_samples, t0, deployment)
        if t3 is not None:
            notes.append(
                "the pods that served this scale-out were gone by the end of the run; "
                "T3 and T6 come from the state watcher at its 2s cadence, and the "
                "scheduling and container-start stages it does not record stay absent"
            )

    # T7 — traffic actually served by a new replica.
    #
    # The service does not label responses with the pod that produced them, so
    # this cannot be observed directly from a sample. Where per-pod CPU shows
    # the new pod doing work, that is used; otherwise the first successful
    # request completing after Ready is the closest honest proxy, and the
    # method is recorded either way rather than left for the reader to assume.
    t7 = None
    t7_method = "unavailable"
    if first:
        per_pod = [
            r
            for r in metrics_rows
            if r["metric"] == "tap_api_cpu_cores_per_pod"
            and first["pod"] in r["labels"]
            and r["value"] > 0.01
        ]
        if per_pod:
            t7 = min(r["t"] for r in per_pod)
            t7_method = "first non-idle CPU sample for the new pod"
        elif t6:
            after_ready = [
                s.t_start + s.latency_s
                for s in samples
                if s.t_start >= t6 and 200 <= s.status < 300 and not s.error
            ]
            if after_ready:
                t7 = min(after_ready)
                t7_method = "first successful request completing after Ready (proxy)"

    # T8 — latency back inside the SLO, and holding. A single window under the
    # line during a recovery is noise, so three consecutive windows are
    # required.
    t8 = None
    p95_windows = [w for w in rolling_percentile(samples, 95) if w[0] >= t0]
    consecutive = 0
    for timestamp, value in p95_windows:
        if value <= slo_p95_s:
            consecutive += 1
            if consecutive >= 3:
                t8 = timestamp
                break
        else:
            consecutive = 0
    if t8 is None and p95_windows:
        notes.append(f"p95 never held under the {slo_p95_s}s SLO for 3 windows")

    # The stamps come off clocks of different resolution: T1 and T2 are read
    # from a Prometheus range query and so are quantised to its step, while a
    # Pod's lifecycle is stamped to the whole second. A stage shorter than
    # either can therefore come out ordered backwards — one scenario published
    # a `pod_creation` of -1.5s — and a negative duration is not a fast stage,
    # it is two clocks disagreeing. Published as a number it invites being
    # averaged, so it is not published as one.
    tolerance_s = _series_step(metrics_rows) + 1.0

    def gap(a: float | None, b: float | None, name: str = "") -> float | None:
        if a is None or b is None:
            return None
        delta = b - a
        if delta >= 0:
            return delta
        if -delta <= tolerance_s:
            notes.append(
                f"{name} completed inside the {tolerance_s:.0f}s the two clocks can "
                f"resolve (raw {delta:.1f}s), so it is reported as 0 rather than negative"
            )
            return 0.0
        notes.append(
            f"{name} not established: its stamps are {-delta:.1f}s out of order, more "
            f"than the {tolerance_s:.0f}s the clocks' resolution explains"
        )
        return None

    return {
        "stamps": {
            "T0": t0,
            "T1": t1,
            "T2": t2,
            "T3": t3,
            "T4": t4,
            "T5": t5,
            "T6": t6,
            "T7": t7,
            "T8": t8,
        },
        "t7_method": t7_method,
        "latencies_s": {
            "detection": gap(t0, t1, "detection"),
            "hpa_decision": gap(t1, t2, "hpa_decision"),
            "pod_creation": gap(t2, t3, "pod_creation"),
            "scheduling": gap(t3, t4, "scheduling"),
            "container_start": gap(t4, t5, "container_start"),
            "readiness": gap(t5, t6, "readiness"),
            "pod_provisioning": gap(t3, t6, "pod_provisioning"),
            "routing": gap(t6, t7, "routing"),
            "total_scale_out": gap(t0, t7, "total_scale_out"),
            "capacity_recovery": gap(t0, t8, "capacity_recovery"),
        },
        "new_pods": [p["pod"] for p in new_pods],
        "notes": notes,
    }


def scale_behaviour(
    watcher_samples: list[dict], deployment: str, *, offered_capacity_replicas: float | None = None
) -> dict:
    """How the autoscaler behaved, as opposed to how fast it was."""
    timeline = [
        (
            s["t"],
            (s.get("deployments") or {}).get(deployment, {}).get("spec_replicas"),
            (s.get("deployments") or {}).get(deployment, {}).get("ready"),
        )
        for s in watcher_samples
        if (s.get("deployments") or {}).get(deployment)
    ]
    timeline = [(t, spec, ready) for t, spec, ready in timeline if spec is not None]
    if not timeline:
        return {"samples": 0}

    changes = []
    for (_, spec_prev, _), (t_now, spec_now, _) in itertools.pairwise(timeline):
        if spec_now != spec_prev:
            changes.append(
                {
                    "t": t_now,
                    "from": spec_prev,
                    "to": spec_now,
                    "direction": 1 if spec_now > spec_prev else -1,
                }
            )

    reversals = sum(1 for a, b in itertools.pairwise(changes) if a["direction"] != b["direction"])
    duration = timeline[-1][0] - timeline[0][0]
    # Replica-seconds: what the scaling actually cost, and the only fair way to
    # compare an autoscaler that overshoots briefly with one that ramps slowly.
    replica_seconds = 0.0
    for (t_prev, spec_prev, _), (t_now, _, _) in itertools.pairwise(timeline):
        replica_seconds += spec_prev * (t_now - t_prev)

    peak = max(spec for _, spec, _ in timeline)
    settled = timeline[-1][1]
    result = {
        "samples": len(timeline),
        "duration_s": duration,
        "min_replicas_seen": min(spec for _, spec, _ in timeline),
        "peak_replicas": peak,
        "final_replicas": settled,
        "replica_seconds": replica_seconds,
        "mean_replicas": replica_seconds / duration if duration else None,
        "scale_events": len(changes),
        "scale_ups": sum(1 for c in changes if c["direction"] > 0),
        "scale_downs": sum(1 for c in changes if c["direction"] < 0),
        "direction_reversals": reversals,
        "replica_changes_per_minute": len(changes) / (duration / 60) if duration else None,
        "changes": changes,
    }
    if offered_capacity_replicas:
        # Overshoot and undershoot against the replica count the offered load
        # actually needed. Both matter and they are not symmetric: overshoot
        # costs money, undershoot costs latency.
        result["required_replicas"] = offered_capacity_replicas
        result["overshoot_replicas"] = max(0.0, peak - offered_capacity_replicas)
        result["undershoot_replicas"] = max(0.0, offered_capacity_replicas - peak)
        result["overshoot_fraction"] = (
            peak - offered_capacity_replicas
        ) / offered_capacity_replicas
    return result
