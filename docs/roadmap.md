# Roadmap

Follow-up work is organized in numbered packages, referenced by number in
issues, PRs and discussions. Package numbers are stable: delivered packages
are removed from this page but their numbers are not reused.

Delivered and no longer tracked here: package 1 (typed, streaming result
pipeline with Parquet/Arrow output), package 2 (TAP table upload:
inline multipart and http(s) `UPLOAD` on /sync and /async, per-query
`TAP_UPLOAD` temp tables, `uploadMethods`/`uploadLimit` in capabilities,
configurable limits), and package 3 (UWS completeness: `WAIT` blocking
requests on the job and phase resources, `AFTER` job-list filtering, and
real `ABORT` that cancels the executing statement via the backend PID and
`pg_cancel_backend()`), and package 6 (plugin-based metadata databases:
domains bind a pydantic model package to a SQL schema and mount point via
a small contract, the shared machinery in `tapcore.metadata`
does the rest, third-party packages register through the
`skao_tap.models` entry-point group, and `TAP_MODEL_PLUGINS` selects what
a deployment activates; the ODP/srcnet and software-discovery domains
ship built in — the user data product domain follows when its model
package exists), and package 5 (scaling, resilience and backup: default
soft anti-affinity/zone-spread and PodDisruptionBudgets for multi-replica
services, opt-in VerticalPodAutoscalers per service, `postgresql.tuning`
server arguments for right-sizing the in-chart database, a scheduled
`pg_dump` backup CronJob with retention, and documented HA-PostgreSQL,
PITR and restore procedures — see the deployment guide), and package 8
(unified SRCNet logging and observability: `ska-src-logging` in every
service, `X-Request-ID` correlation carried from the API through the job row
and the SQL itself into the executor's records, seven Prometheus metrics
chosen from what recent performance work needed and did not have, an opt-in
in-chart Prometheus for trying them out, and OTLP tracing when a collector is
configured — see the observability guide), and package 9
(horizontal autoscaling: an opt-in CPU HorizontalPodAutoscaler for tap-api and
queue-backlog scaling for tap-executor through a KEDA ScaledObject or a plain
external-metric HPA, with the chart refusing scale-to-zero on a metric the
executor itself exports, a CPU HPA beside a VPA that controls CPU, a CPU
target with no CPU request, and a replica maximum whose pools would exceed
max_connections — see the autoscaling guide), and package 4
(identity and registry: INDIGO IAM bearer tokens verified against the
issuer's JWKS, authorisation behind a plugin with the SRCNet Permissions API
and local IAM groups shipped, seven gateable operations with the query surface
enforced as a group, IVOA AuthVO challenges naming the IAM, job ownership and
attributable deletion, and a VOResource record at /tap/registry — see the
authentication and registry guides).

## Measured findings

A running log of what the benchmark suite
(`benchmarks/tap-performance`, see [Benchmarking](benchmarking.md)) has
established, newest first. Each entry is a measurement rather than an opinion,
so it can be checked and it can go stale — the run that produced it is named.

### 2026-08-24 — the four service-side answers shipped

Not a measurement — the response to the four entries below, so the log says
what changed as well as what was found:

- **The saturating signal is replaced.** The chart's ScaledObject and the
  external-metric path now scale on queue *depth*, `tap_jobs{phase="QUEUED"}`,
  against `queuedJobsPerReplica` (default 10 ≈ 5 s of queue at ~2 jobs/s per
  executor). The old seconds-denominated values are refused by the chart with
  a message naming the replacements, and an empty queue reports depth 0
  rather than letting the series vanish. The age gauge stays exported as a
  latency figure. Answers
  [three pods against a three-thousand-job queue](#2026-08-24-the-executor-autoscaler-asks-for-three-pods-against-a-three-thousand-job-queue).
- **The executor's CPU limit stops lying.** The benchmark deployment's limit
  drops from 2 to 1 — the single thread pins at ~0.96 cores, and the second
  core was headroom that cannot exist. Companion to
  [a pinned executor had no rule looking at it](#2026-08-24-a-pinned-executor-had-no-rule-looking-at-it-fixed).
- **Routing becomes measurable.** Every API response carries `X-Served-By`
  (the pod name), so the 83–243 s routing stage can be confirmed or dismissed
  directly rather than inferred through a proxy that, on a deep queue,
  reports the queue.
- **Overload can refuse instead of resetting.** `tapApi.backlog` sizes the
  accept queue and `tapApi.limitConcurrency` makes uvicorn answer 503 past a
  per-worker connection ceiling — off by default until
  [the reset onset](#2026-08-24-a-third-of-the-shed-load-is-a-reset-not-a-refusal-wherever-the-pool-tips)
  is established.

### 2026-08-24 — a pinned executor had no rule looking at it (fixed)

Two defects found by finally *looking* at an autoscaling dashboard, after fixing
the path that had left half of every one of them blank.

**The three request panels of every dashboard were empty.** The series path was
corrected earlier to `metrics/keda-K3.parquet`; the samples path three lines
below it still said `samples/K3.parquet`. So offered-against-served, latency and
error rate drew nothing on all seven dashboards in every KEDA run — captioned,
axed, titled and empty. An empty panel under a heading reads as "measured, and
flat". A panel with no data now says so on its face, rather than leaving
matplotlib's "no artists with labels" warning as the only report of it.

**And the resource that was actually at its ceiling had no rule.** The dashboard
showed executor CPU at 2.9 cores across 3 pods. Per pod that is **0.95 to 0.97
of a core against a two-core cgroup, with zero CFS throttling** — the chart's
own GIL argument, stated in its values file: "one executor runs one query at a
time". The classifier compared API CPU against the API's ceiling and PostgreSQL
against PostgreSQL's, and never looked at the executor at all. So this family's
real resource ceiling was invisible, `UNKNOWN` was the verdict on the sustained
overload, and I described these runs as having "nothing busy".

`EXECUTOR_CPU_BOUND` is now a class, with the ceiling at one core a pod times
the pods that were ready. It fires on four of the seven scenarios, and where it
appears alongside `KEDA_SCALE_LAG` the pair is the entire story of an autoscaling
shortfall: **the pods that existed were full, and more were never asked for.**
`UNKNOWN` has gone from the family's tally.

Both defects are the species this day kept turning up: a thing that was written,
looked present, and silently reported nothing. That is now six of them.

### 2026-08-24 — the autoscaler's answer to every load is three pods, and the delay is never the pod

Run `20260824T102832Z-29507cbb-keda`: all seven scenarios again, with the
generator's in-flight cap raised so it stops abandoning arrivals. Six of the
seven are valid — the [first attempt](#2026-08-24-the-executor-autoscaler-asks-for-three-pods-against-a-three-thousand-job-queue)
had four invalid — so this is the family's real result.

| | max queued | oldest | desired | p95 | errors |
| --- | --- | --- | --- | --- | --- |
| K1 idle to 0.5xC1 | 9 | 1 s | 1 | 1.3 s | 0.00% |
| K2 step to 3.5xC1 | 2,689 | 139 s | 3 | 315 s | 0.02% |
| K3 spike to 6xC1 | 4,094 | 197 s | 3 | 946 s | 9.79% |
| K4 ramp to 6xC1 | 3,863 | 185 s | 3 | 567 s | 0.09% |
| K5 alternating 0.5x/4xC1 | 1,767 | 119 s | 3 | 124 s | 0.05% |
| K6 sustained 6xC1 ⚠ | 4,028 | 189 s | 3 | 1,203 s | 17.31% |
| K7 4xC1 to 0.2xC1 | 3,041 | 154 s | 3 | 327 s | 0.02% |

**Three replicas, every time, out of a permitted eight.** Queues from 1,700 to
4,100 jobs; offered rates spanning twelve-fold; profiles as different as a cold
spike, a ten-minute ramp, a two-minute square wave and a sustained overload. The
answer does not move, because `desired = ceil(oldest_queued_age / 60)` and the
head-of-queue age saturates near 190 s whatever the depth behind it. K1 is the
control: below the threshold nothing scales and p95 stays at 1.3 s inside the
SLO, which is correct behaviour.

**And the stage breakdown, now complete for five scenarios:**

| | detect | HPA | provision | routing | total | recovery |
| --- | --- | --- | --- | --- | --- | --- |
| K2 | 98 s | 19 s | **1 s** | 129 s | 246 s | — |
| K3 | 106 s | 149 s | **0 s** | 243 s | 497 s | — |
| K4 | 250 s | 21 s | **1 s** | 83 s | 352 s | 26 s |
| K5 | 340 s | — | **0 s** | 121 s | 476 s | 216 s |
| K7 | 220 s | 270 s | — | — | — | 254 s |

Provisioning a Pod — created, scheduled, container started, Ready — is **zero to
one second in every scenario**. Scale-out totals are 246 to 497 s. Kubernetes is
not the delay and there is no point tuning it. The delay is detection (98 to
340 s: the 60 s threshold, the scaler's polling interval, and the time the head
of the queue takes to age into the threshold at all) and routing (83 to 243 s
from Ready to demonstrably serving, measured by proxy and worth confirming).

The slowest detection is K5's 340 s, and its profile explains it: alternating
bursts let the queue drain between them, so the head-age signal keeps falling
back under the threshold and the scaler spends most of the window unconvinced.
Bursty traffic is the case this signal handles worst.

`KEDA_SCALE_LAG` is now the leading verdict on five of the seven, which is the
right name: capacity existed, and it arrived four to eight minutes late.

**K6 is invalid and cannot be made otherwise.** It abandoned 19% of arrivals
even at a 20,000-request cap, because a sustained overload grows its queue
without bound and no finite cap can hold it. That is a property of the scenario,
not a bug to fix: a sustained-overload point is not measurable open-loop, and the
honest version of it is a bounded-concurrency run. Its numbers are kept as
evidence — 4,028 queued, 17.3% of jobs never finishing — and not as a
measurement of the offered rate on its label.

### 2026-08-24 — the undershoot is not the harness, and provisioning is not the slow part

Run `20260824T100103Z-d14a207c-keda`: the 0-to-6xC1 spike scenario alone, with
the generator's in-flight cap raised so it abandons nothing. Run to settle two
questions the [capped family](#2026-08-24-the-executor-autoscaler-asks-for-three-pods-against-a-three-thousand-job-queue)
left open, before spending two hours re-running all seven.

**The undershoot survives an honest queue.** Every guard passes: arrivals 0.5 s
late at p95, nothing abandoned, generator at 7% of the host's cores. The queue
reached **3,895 jobs** and the oldest waited **190 s** — and the autoscaler
still asked for **3 replicas out of 8**. The capped run's 2,968-job queue was
not what limited it; the signal is.

What the cap had been hiding is how bad the overload actually is. The same
scenario measured at 305 s p95 with 0.05% errors while it was quietly shedding
39% of its arrivals. Offered in full: **982 s p95 and 13.9% errors**, of which
1,441 are jobs still `PENDING` when the client gave up at 600 s. A measurement
that drops a third of its load reports a service that is coping better than it
is.

**And the first complete scale-out breakdown for this scenario:**

| stage | |
| --- | --- |
| detection — metric crosses threshold | 86 s |
| HPA decision — replica request changes | 4 s |
| pod creation → scheduled → started → ready | 1 s |
| routing — Ready to serving traffic | 161 s |
| **total scale-out** | **251 s** |

Kubernetes is not the slow part. Provisioning a pod takes **one second**;
detection and routing are 247 of the 251. Detection is the 60 s threshold plus
the scaler's polling, and it is the part a threshold change would move.
Routing — 161 s from a pod being Ready to it demonstrably serving — is the part
worth understanding next, and it is measured by proxy (the first successful
request completing after Ready), which on a queue this deep may be reporting
the queue rather than the routing.

API CPU peaked at 0.69 cores and PostgreSQL at 0.32 while all of this happened,
so raising the cap did not turn the generator's own phase polling into a load on
the service — the other thing this probe was for. The *executors*, which no rule
was looking at yet, were pinned at 0.96 of a core each throughout.

### 2026-08-24 — the executor autoscaler asks for three pods against a three-thousand-job queue

Run `20260824T074332Z-b4fa9d64-keda`, D2, all seven autoscaling scenarios
against the repository's own ScaledObject, unmodified: trigger
`max(tap_oldest_queued_job_seconds)`, threshold 60, min 1, max 8, polling every
5 s. Offered rates from 0.5x to 6x async-C1 (6 jobs/s).

**Peak replicas was 3 in every scenario that scaled at all, out of a maximum of
8, across a twelve-fold spread of offered rate:**

| | profile | max queued | max oldest | max desired | peak ready | p95 |
| --- | --- | --- | --- | --- | --- | --- |
| K1 | idle to 0.5xC1 | 8 | 1 s | 1 | 1 | 1.3 s |
| K2 | 0.5x step to 3.5xC1 | 2,536 | 135 s | 3 | 3 | 257 s |
| K3 | 0 to 6xC1 spike | 2,968 | 155 s | 3 | 3 | 305 s |
| K4 | ramp to 6xC1 | 2,737 | 145 s | 3 | 3 | 289 s |
| K5 | alternating 0.5x/4xC1 | 1,039 | 79 s | 3 | 3 | 83 s |
| K6 | sustained 6xC1 | 2,855 | 167 s | 3 | 3 | 320 s |
| K7 | 4xC1 to 0.2xC1 | 2,904 | 144 s | 3 | 3 | 240 s |

K1 is the one scenario that behaved: the metric never crossed 60, nothing
scaled, and p95 stayed at 1.3 s inside the 2 s SLO. Every other scenario missed
the SLO for its entire window while **five of the eight executors it was
allowed were never asked for**. The API and the database were idle throughout —
API CPU p95 0.25 to 0.35 cores, PostgreSQL 0.08 to 0.22.

**Corrected on 2026-08-24.** This entry originally said "nothing was busy",
which was wrong and made the finding weaker than it is. Nothing *instrumented
by a rule* was busy: no rule looked at the executors, and the executors were
pinned. Each pod sat at 0.95 to 0.97 of a core with a two-core cgroup and no
CFS throttling — the chart's own GIL ceiling, since "one executor runs one query
at a time". So the three pods the autoscaler did provide were **full**, the
queue was growing, five more pods were permitted, and none was requested. See
[a pinned executor had no rule looking at it](#2026-08-24-a-pinned-executor-had-no-rule-looking-at-it-fixed).

The cause is the signal, not the autoscaler. `desired = ceil(oldest_queued_age /
60)`, and the age of the *head* of the queue is not proportional to the depth
of it. Depth grows at (arrivals − drain); the head's age grows far more slowly,
because the head is being served the whole time. At t+80 s in K3 there were
**1,713 jobs queued and the oldest had waited 54 s**, so the scaler read 54,
which is under the threshold, and asked for **one** pod. The signal saturates
around 150-170 s of head-age — three replicas — no matter how deep the queue
goes. It undershoots hardest exactly when arrivals most exceed capacity, which
is when the pods are most needed.

The chart's own comment describes the intent — "300s of backlog against 60 here
asks for 5 replicas" — and head-of-queue age does not deliver it. What would:
scale on queue *depth* against a jobs-per-replica figure, or on depth divided by
the measured drain rate, which is an estimated wait rather than an observed one.
Either is a change to `deploy/helm/skao-tap` values and the ScaledObject
template, and neither is made here: this entry is the measurement.

Two secondary observations. The scaler flaps — K3 scaled up three times and
down three times inside one window and ended at one replica with the queue
still draining; K7 reversed twice. And scale-out is slow before it is
insufficient: detection alone took 92 to 254 s, and total scale-out where it
could be established was 221 to 356 s.

**Four of the seven scenarios are invalid** and the guard says why: K3, K4, K6
and K7 abandoned 39%, 28%, 41% and 7% of their arrivals at the generator's
in-flight cap. For this family that is worse than a mislabelled rate — the
arrivals it dropped are the backlog the autoscaler was being measured on, so
the scenario shrank the thing under test. The numbers above survive it because
they are all *lower* bounds on the queue and on the mismatch: a fuller offered
rate makes the queue deeper and the undershoot worse, not better. The cap is
now configurable with a much larger value for async work, and the family is
worth re-running under it before anything is asserted about the stage timings.

### 2026-08-24 — the KEDA family's own analysis had been reporting less than it measured (fixed)

Reading the seven scenarios above turned up five defects in how they were
analysed. All are fixed, and all were correctable from the artefacts the run had
already written.

**Every autoscaling dashboard was skipped.** The plot looked for
`metrics/K3.parquet`; the series are written as `metrics/keda-K3.parquet`. Seven
of seven timelines — the whole visual output of the family the run exists for —
were absent from the report, under the message "no metrics parquet for this
scenario", which was not true.

**A scenario could not be marked invalid.** `measure()` computes the guards and
the `invalid` flag, and the scenario entry was then rebuilt from a list of
fields to keep, which dropped them. The guards ran on all seven and were
discarded before anything read them.

**`reclassify` skipped the family.** The scenarios live under `keda` rather than
`runs`, so every correction to the analysis rules reached every family except
this one. It now walks both, re-derives the stage timings from the stored stamps,
and will not clear a guard failure the run recorded — a rerun of the analysis
must not be able to accept a rejected measurement by forgetting why it was
rejected.

**A recovery scenario was timed as a scale-out.** `timings()` takes a
`scale_up` flag and nothing ever passed it, so K7 — 4xC1 falling to 0.2xC1 —
looked for the metric *crossing* the threshold and found it 2 s in, left over
from the phase before the transition. Read from the profile now: K7's detection
is 156 s, its HPA decision 265 s, and its recovery 186 s.

**A stage duration of -1.5 s was publishable.** T1 and T2 come from a Prometheus
range query quantised to its 2 s step; a Pod's lifecycle is stamped to the whole
second. A stage shorter than either comes out ordered backwards. Inside what the
two clocks can resolve — measured from the series rather than assumed — the
stage now reads 0 with a note; beyond it, absent. And where the pods that served
a scale-out were deleted before the run ended, which is what happens to every
flapping scenario, T3 and T6 fall back to the state watcher that saw them live.

### 2026-08-24 — the replica sweep cannot measure replica scaling, and almost published a number claiming it had (fixed)

Run `20260824T014320Z-a5058118-fixed-scaling`, D2, autoscalers off, offered
rates at 0.5, 1, 2, 4, 6 and 8 times C1 against 1, 2, 4 and 8 API replicas.
**Seventeen of the 24 measurements are invalid**, and the seven that are left
do not bracket a ceiling at any replica count.

What the valid points do establish:

| replicas | offered | served | p95 | errors |
| --- | --- | --- | --- | --- |
| 1 | 115.3 | 114.7 | 52 ms | 0% |
| 1 | 230.7 | 1.4 | — | 98.7% |
| 2 | 230.7 | 229.3 | 33 ms | 0% |
| 4 | 230.7 | 229.2 | 17 ms | 0% |
| 8 | 230.7 | 229.3 | 16 ms | 0% |

So: one replica cannot serve C1 itself — at exactly 230.7 rps the pool was
full for 99.9% of the window and refused 1,576 requests — and two, four and
eight replicas all serve it with a p95 between 16 and 33 ms and no errors. The
p95 falling from 33 ms to 16 ms as replicas double while throughput stays at
229 is the shape of a service with headroom to spare, not of one scaling.

Every rung that would have found where four and eight replicas stop is
unmeasurable as offered. The generator holds at most 4,096 requests in flight;
past 1×C1 it hits that cap and **abandons 71% to 99% of its arrivals**. What is
left is a closed-loop measurement at concurrency 4,096 wearing an open-loop
label — which is exactly why 2×, 4×, 6× and 8× C1 all report the same ~120
requests/s at the same ~92-second p95 on both four and eight replicas. Four
different offered rates, two different replica counts, one experiment.

The [existing lateness guard](#2026-08-24-a-guard-was-missing-for-the-generators-own-schedule-fixed)
could not see this: at the cap the generator does not fall behind its schedule,
it drops the arrival, so p95 lateness stayed under four seconds on points that
issued 7% of what they claimed. There is now a second guard on the same
principle — more than 1% of arrivals abandoned and the run is marked — and the
count is recorded per measurement so a finished run can be re-judged.

The suite was about to publish **"replica scaling efficiency at 8: 0.25"** from
this, with the evidence "229.3 rps on 8 replicas against 114.7 on one". Both of
those are rates the service met in full; neither is a limit; and the report's
per-replica efficiency column (1.000, 1.000, 0.500, 0.250) and its scaling
efficiency plot said the same thing with the same authority. A ratio of two
rates nobody raised is not an efficiency, and the caption attributing the
shortfall to "one PostgreSQL" attributed it to the wrong party — the shortfall
was the ladder's. The headline, the table and the plot now require each figure
to be a *bracketed* capacity: a rate the service was pushed past by a valid
higher rung. Where it was not, the efficiency reads "—" and says why, and C1 is
labelled a lower bound.

To actually measure replica scaling, the family needs bounded-concurrency runs
per replica count — the shape the size sweep already uses — or rungs between 1×
and 2× C1, which is where the interesting region for two replicas and up
turned out to be.

### 2026-08-24 — the classifier was comparing a fleet's total against one pod's ceiling (fixed)

Every API series the bottleneck rules read is a Prometheus `sum()` over the
pods. Three of the ceilings they were compared against were per-pod or
per-process:

- `tap_db_connections_in_use` (fleet) against `dbPoolMax` = 8 (one worker
  process). An eight-replica deployment holding 12 of its 64 connections was
  called pool-full at 19% utilisation.
- `tap_api_memory_bytes` (fleet) against 1 GiB (one pod). 1.77 GB across eight
  pods is 220 MiB each; it was reported as memory pressure.
- API CPU was already multiplied by the *configured* replica count, which is
  the right idea and the wrong number under an autoscaler, where the
  configuration is not what served.

Reclassifying the fixed-replica run moved **9 of 24 measurements** off
`MEMORY_BOUND`, which they had been assigned at a fifth of the real limit. All
three ceilings now scale by the peak *ready* replica count taken from the run
itself, so an autoscaled window is judged against the fleet that served it.
`CONNECTION_POOL_BOUND` still fires where the pool genuinely timed out — the
503s are real — but `fraction_of_window_pool_full` now means what it says.

Two related artefacts are recorded rather than fixed, because fixing either
means changing the service image and losing comparability with the runs already
measured: `peak_pool_wait_p95_s` reads ~9.7 s against a 5 s pool timeout
because the histogram's last finite bucket is 10 s, so every timed-out acquire
interpolates to the middle of (5, 10]; and `CONNECTION_POOL_BOUND` takes
confidence `min(1.0, pool_wait)`, so any wait over a second is full confidence
and the class outranks everything else wherever the pool waited at all.

### 2026-08-24 — a third of the shed load is a reset, not a refusal, wherever the pool tips

Same run. At every point where the connection pool tipped over, a large
fraction of the load was dropped at the socket rather than refused with an
answer:

| measurement | requests | 503 | ReadError | ReadTimeout |
| --- | --- | --- | --- | --- |
| 1 replica, 1×C1 | 4,194 | 1,330 | 893 | 586 |
| 2 replicas, 2×C1 | 15,211 | 2,594 | 11,968 | 540 |
| 4 replicas, 6×C1 | 12,421 | 7,294 | 4,012 | 753 |

The 503s are the pool-timeout path working as designed. The `ReadError`s are
not: a reset connection is indistinguishable from a crash to the client that
receives it, and a client cannot retry it safely. This is not specific to one
point — it appears at one replica, at two and at four — so the earlier note
asking for the four-replica 6×C1 point to be repeated is superseded: the
behaviour reproduces across the family, and that point is in any case
generator-capped and now marked invalid.

What cannot be said from this family is the offered rate at which resets start,
because every measurement that shows them abandoned most of its arrivals. Four
replicas at 8×C1 ran clean (197 refusals in 32,590 requests) while the same
replicas at 6×C1 collapsed, which looks like rate-independence but is really
two incomparable experiments at the in-flight cap. Pinning the reset path needs
a bounded-concurrency run held just past pool saturation.

### 2026-08-24 — `kubectl scale` then `helm upgrade` is a hard stop under server-side apply (fixed)

The KEDA family would not start. `helm upgrade` failed with `conflict with
"kubectl" with subresource "scale" using apps/v1: .spec.replicas` — the
fixed-replica family had set replica counts through the scale subresource,
which made `kubectl` the field's owner, and Helm 4 applies server-side. The
first upgrade that switches an autoscaler on then aborts. `--force-conflicts`
on the chart install takes the field back: the chart is the authority on every
field it renders. Cost the run about 75 minutes of wall clock, entirely
avoidably, because the failure was instant and unattended.

### 2026-08-23 — the liveness probe turned overload into an outage (fixed)

Both Kubernetes probes pointed at `/tap/availability`, which reports on the
database and therefore queues for a pooled connection. Neither probe set
`timeoutSeconds`, so both used the one-second default. Under an offered rate
*inside* the service's own measured capacity the pool saturated, the endpoint
could not answer in a second, and the kubelet **SIGKILLed the API twice** —
restarting a process that was busy rather than broken, dropping every in-flight
request, and making the overload worse on each restart. Readiness had the same
dependency, so a loaded pod also left the Service and pushed its share onto
pods in the same state.

Fixed by asking the two questions separately, since their remedies differ:
`/health/live` touches nothing outside the process (a wedged process is what a
restart fixes), and `/health/ready` distinguishes a full pool — still ready,
because that is a healthy service under load — from an unreachable database.
Both are exempt from authentication, because a kubelet has no token and gating
them would have meant that enabling auth killed every pod.

### 2026-08-24 — overload sheds mostly cleanly, but not entirely

Fixed-replica scaling on D2, four replicas, six times C1 offered (about 1,380
requests/s against a service that sustains ~230 per replica): of 12,421
requests, **7,294 were refused with 503** — the pool-timeout path working as
designed, a fast refusal rather than a held connection — but **4,005 failed
with a transport-level `ReadError` and 753 with `ReadTimeout`**. A third of the
load was dropped at the socket rather than refused with an answer.

A 503 with `Retry-After` is a client's cue to back off; a reset connection is
not, and it is indistinguishable from the service having crashed. Worth
tracing: most likely the accept queue overflowing, since the pods neither
restarted nor were OOM-killed. Graceful shedding is the difference between an
overloaded service and an unavailable one.

Note the non-monotonicity: eight times C1 on the same four replicas ran clean
(32,391 of 32,590 successful, 197 refusals). So this is not a simple function
of offered rate, and the 6x point needs repeating before its cause is asserted.

**Superseded on 2026-08-24.** Two corrections. The parenthetical "a service
that sustains ~230 per replica" is wrong: one replica does not sustain C1, it
sheds 98.7% of it. And the request to repeat the 6× point is answered by the
family as a whole — resets appear at one replica, at two and at four, wherever
the pool tips — while the point itself abandoned 96% of its arrivals at the
generator's in-flight cap and is now marked invalid, so the comparison against
8×C1 was never between two comparable experiments. See
[a third of the shed load is a reset](#2026-08-24-a-third-of-the-shed-load-is-a-reset-not-a-refusal-wherever-the-pool-tips).

### 2026-08-24 — a guard was missing for the generator's own schedule (fixed)

One measurement reported 84 requests/s with a 100-second p95 and was published
as a service result. It was not one: the service answered all 29,407 requests
successfully, and the **generator was 88 seconds behind its own arrival
schedule**, so every latency was measured from an issue time it had missed.
Textbook coordinated omission, in the direction that makes the service look
bad.

The lateness was already computed and stored per measurement — nothing checked
it. There is now a guard: if arrivals are more than five seconds late at p95,
the run is marked, on the same principle as the load-generator CPU guard. A
number that describes the client must not be presentable as a number about the
service.

### 2026-08-24 — the aggregate query is the case for admission control

Q13 (`GROUP BY` over the whole ObsCore table) across the three sizes, four
concurrent clients:

| | p95 | throughput |
| --- | --- | --- |
| D1, 2 GiB | 393 ms | 11.2 requests/s |
| D2, 10 GiB | 3,128 ms | 1.7 requests/s |
| **D3, 25 GiB** | **17,753 ms** | **0.2 requests/s** |

Forty-five times the latency for twelve times the data, and at D3 it is
`DATABASE_IO_BOUND` with a plan that discards 616,550 rows after reading them.
Nothing here is a bad plan — a full aggregate over 7.4 million wide rows is
proportional work — but a user can issue this *synchronously* today, and one
such request occupies a connection for eighteen seconds. That is what makes
package 11's `EXPLAIN`-based admission decision worth building: the service
should be able to recognise this shape and route it to `/async` rather than
hold a synchronous connection for it.

### 2026-08-24 — the liveness fix holds under the load that broke it

The open-loop family that previously killed the API twice — pool saturation
starving a database-dependent liveness probe, 32% errors and a 17-minute p95 —
now runs clean: at the same offered rate, 114.7 requests/s served against 115
offered, zero errors, p95 52 ms, no restarts. The failure was the probe, not
the service's capacity.

### 2026-08-24 — the size sweep was comparing different workloads (fixed)

The corpus was rebuilt for each dataset inside the sweep, sized to the
observation count of the tier being measured. So D1, D2 and D3 were each
measured with a *different* set of queries, and the corpus hash recorded in the
provenance described only the last one built — the field that exists to prove
two results are comparable was quietly asserting something false.

It shows up in the numbers: at four concurrent clients D3 (25 GiB) served ~209
requests/s against D2's (10 GiB) 196, which is not a thing 2.5x the data does.

Now built once per run and sized to the *smallest* tier. That works because
generation is a prefix — a database grown to 25 GiB contains every row the
2 GiB one had, at the same identifiers and coordinates — so every lookup and
cone centre in the corpus exists at every size, and what changes between tiers
is only how much other data surrounds it. Sizing to the largest instead would
leave most identifiers absent from the smaller tiers and turn the sweep into a
comparison of miss rates.

**What this qualifies.** Cross-*size* throughput comparisons taken before this
fix — including the published "throughput versus database size" — compare
different workloads and should not be read as a size effect. Everything
measured *within* one dataset stands: saturation points, bottleneck
classifications, cache hit ratios, per-class latencies and the before/after of
the translation fix, which was measured on D1 both times.

### 2026-08-24 — D3 is where the working set stops fitting

D3 (25.28 GiB, 7.4M ObsCore rows) against a 6 GiB PostgreSQL is the first size
that touches the disk in earnest. Per 180-second measurement window, on client
backends only:

| | buffer hit ratio | blocks read | read wait |
| --- | --- | --- | --- |
| D1 (2 GiB), warm | 100.00% | 0 | 0 s |
| D2 (10 GiB), warm | 100.00% | 0–7 | 0 s |
| **D3 (25 GiB)** | **68–70%** | **1.6–2.2M** | **14–52 s** |

Throughput at one client falls from 145 requests/s on D2 to 115–142 on D3 —
less than a 70% hit ratio might suggest, because the reads are NVMe-fast and a
single client cannot queue behind itself. What this regime costs under
concurrency is the question the rest of the D3 sweep answers.

The first repetition at each size is measurably colder than the rest (D1's
first window read 280 MiB, D2's first read 1,091 MiB, and both were at 100%
thereafter), so the 60-second warmup does not fully warm a working set of this
size. Worth widening the warmup for the larger datasets rather than reading the
first repetition as a result.

### 2026-08-24 — the database summary was reading the wrong row (fixed)

`pg_stat_database` carries one row per database *plus* a shared-objects row
whose `datname` is NULL, and that row sorts first. The summary took the first
row, so every cache hit ratio and block-read figure the suite produced —
including the ones already published — described an empty accounting entry with
a few hundred block accesses rather than the workload. The bottleneck classifier
was fed the same row, which is why an I/O-bound D3 measurement classified as
`UNKNOWN`.

Fixed by selecting the row by database name, recorded in the snapshot. The
`pg_stat_io` figures the summary reports are now client-backend only as well:
after a bulk load the checkpointer and autovacuum dwarf the query workload, and
counting them attributed generation I/O to the measurement that followed it.

Two things follow. The conclusion that D1 and D2 fit in memory *survives* —
re-derived from the stored deltas, they really are 100% with zero reads — but
it was not evidence when it was written, because the number quoted came from a
row that reads 100% whatever the workload does. And a `reclassify` command now
re-derives a finished run's database summary and bottleneck verdicts from its
stored artefacts, so an analysis mistake can be corrected without re-measuring:
run against the published D1+D2 results, nothing changed, which is what
confirms those conclusions rather than assuming them.

### 2026-08-23 — a full aggregate scales with the table, as it must

Q13 (`GROUP BY` over all of ObsCore) went from a p95 of 393 ms on D1 to
**3,128 ms on D2** — roughly 8x for 5x the rows — and is the only class that
classifies `DATABASE_CPU_BOUND`. Nothing is wrong with the plan; a full
aggregate is proportional work. It is the class that will make an
`EXPLAIN`-based admission decision worth having, because a user can issue it
synchronously today (see package 11).

### 2026-08-23 — ADQL translation was the service (fixed)

Translation cost **41.5 ms of a ~50 ms synchronous request**, against under
1 ms in PostgreSQL for most query shapes. The single-core ceiling was 24
requests/s and it was set in the parser, not the database. Cause: the parser
library parsed twice per translation and discarded the first tree, and ANTLR
ran full-context prediction (71% of the profile). Fixed by parsing once and
trying SLL prediction first with a fallback — **41.55 ms → 1.18 ms**, verified
identical on all 12,000 corpus queries. End to end on D1, one replica and one
worker: **20.1 → 230.7 requests/s sustainable (11.5x)**, p95 at four clients
77 ms → 28 ms.

### 2026-08-23 — the API is still the constraint, at both sizes

After that fix the normal mix remains `TAP_CPU_BOUND` on D1 (2 GiB) and D2
(10 GiB) alike: PostgreSQL sits near idle for it. Saturation moved from four
concurrent clients on D1 to eight on D2. So replicas and `tapApi.workers`
remain the throughput lever — and each worker is now worth roughly 200
requests/s rather than 20.

### 2026-08-23 — five times the data costs about a fifth of the throughput

D1 (2.06 GiB, 600k ObsCore rows) served 177 requests/s at one client; D2
(10.26 GiB, 3.0M rows) served 145 — **-18% for 5x the data**, with p95
unchanged at 13 ms. Index-assisted queries are close to flat against size, as
they should be; the size sweep continues to D3 and D4, where the working set
stops fitting in memory and this is expected to change character.

### 2026-08-23 — the remaining costs have separated

With parsing no longer dominating, the expensive classes are individually
attributable rather than uniformly slow:

| | p95 on D1 | classified |
| --- | --- | --- |
| normal mix (Q01–Q08, Q10) | 22–43 ms | `TAP_CPU_BOUND` |
| Q11, 10,000-row result | 352 ms | `SERIALIZATION_BOUND` |
| Q13, full aggregate | 393 ms | `DATABASE_CPU_BOUND` |

Those two are the next optimisation targets, and they are unrelated to each
other — see packages 10 and 11.

### 2026-08-23 — cone search needs an index on the *expression*

The translator emits `spoint(RADIANS(s_ra), RADIANS(s_dec)) @ scircle(...)`, a
function applied to the columns, so a GiST index on a stored `spoint` column is
never considered and every cone search becomes a sequential scan. The index has
to be on that expression; `spoint`, `scircle` and `radians` are all
`IMMUTABLE`, so it is legal, and the planner does use it once present. This is
deployment guidance the project did not previously state.

### 2026-08-23 — the planner misjudges join fan-out

Ten plan nodes costing 5 ms or more carry cardinality estimates 50x to 477x
out, all in the join-heavy classes (Q09, Q11, Q14) and none in the normal mix.
Extended statistics on the CAOM parent/child key pairs is the obvious
candidate; the impact is currently confined to the stress classes.

## Package 10 — Cheaper large results

Q11 (10,000 wide ObsCore rows) has a p95 of 352 ms and classifies as
`SERIALIZATION_BOUND`: a busy API and an idle database, so the time is going
into producing bytes rather than finding rows. Worth investigating in order:
where the per-row cost actually is (the typed serialisation path builds a
Python value per cell), whether the VOTable writer or the DSV writer dominates,
and whether Arrow/Parquet — which already stream in record batches — are
materially cheaper per row and should be recommended for bulk transfer.

## Package 11 — Aggregate and scan-bound queries

Q13 (`GROUP BY` over the whole ObsCore table) has a p95 of 393 ms and is the
only class that makes PostgreSQL the constraint. A full aggregate over a large
table is honest work, so the question is not how to make the scan free but
what a service should do about it: parallel query settings for the in-chart
database, whether `EXPLAIN`-based rejection should refuse the most expensive
synchronous queries and steer them to `/async`, and whether summary tables or
materialised views belong in the metadata domains that need them.

## Package 7 — Queryable region footprints (`s_region`)

Unblocked: `ska-src-mm-notification` 0.1.8 is released and ships the STC-S
validator this depends on, so the work can start. **First step is the version
bump** — `services/tap-api/pyproject.toml` still pins `>=0.1.7`, so nothing
sees the validator until that becomes `>=0.1.8` and `uv.lock` is refreshed.

The notification model carries `s_region` (STC-S/pgsphere-style strings,
e.g. `CIRCLE 3.5867 -30.4 0.25`) on data products and artifacts, but the
generated schema stores it as plain text — so ObsCore-style footprint
queries (`INTERSECTS(s_region, CIRCLE('ICRS', ...))`) do not work on the
ingested metadata.

- **Parse at ingestion**: convert the STC-S string into a companion
  pgsphere geometry column (`s_region_geom spoly`; circles converted to
  polygon approximations), register it in TAP_SCHEMA, and index it with
  GiST so ADQL `INTERSECTS`/`CONTAINS` over footprints are fast.
- **Take the 0.1.8 validator** *(upstream, done — needs the pin bump)*: the
  region type mismatch is fixed. `0.1.7` declared `s_region: str | None`
  with no format validation, so any string (e.g. `"NOT A REGION"`) validated,
  unlike the numeric fields with their Ge/Le constraints. `0.1.8` adds
  `models/regions.py` with `validate_s_region()` — the `CIRCLE`, `POLYGON`
  and `POSITION` STC-S subset in ICRS with coordinate-range checks — wired
  as a model validator on `BaseNotificationModel`, so malformed regions are
  now rejected at the producer, before they reach any archive.
- **Reject malformed regions at the API boundary**: keep validating region
  syntax at the ingestion endpoint as defence in depth, since a producer can
  always be running an older model package than the service.
- **Amendments follow**: `PATCH` updates to `s_region` re-derive the
  geometry column.

## Package 12 — ObsCore 1.1 compliance (`ivoa.obscore` view)

The service is not ObsCore 1.1 compliant today, and the gap was measured
against the REC (REC-ObsCore-v1.1-20170509) rather than assumed: there is no
`ivoa.ObsCore` table or view in the deployed service (the only literal
`ivoa.obscore` in the repo is the synthetic benchmark table in
`benchmarks/tap-performance/tapbench/dataset/schema.sql`, which is not part of
the service and misses the `*_xel` columns); `/tap/capabilities` and the
VOResource record carry no
`<dataModel ivo-id="ivo://ivoa.net/std/ObsCore#core-1.1">` element
(`services/tap-api/tap_api/endpoints/vosi.py::_capability_elements()`); and
utype/xtype metadata is absent end-to-end — never written by
`tapcore/metadata/schema_gen.py::registration_statements()` (which also
hard-codes `std = 0`), never emitted by `tables_xml()` or the JSON `/tables`
mirror — even though the TAP_SCHEMA DDL already has the columns for it.

The distance is short because the SRCNet ODP model is itself
ObsCore-1.1-derived: `srcnet.data_products` already carries ~18 of the
mandatory columns under their exact ObsCore names (including all five 1.1
axis-length additions `s_xel1/s_xel2/t_xel/em_xel/pol_xel`),
`srcnet.artifacts` has `access_url/access_format/access_estsize`, and
`srcnet.observations` has `collection/facility_name/instrument_name`. The
publication gate (`queries/query.py::_published_tables()`) is a name lookup
in `tap_schema.tables`, so a view passes with no query-path change.

The shape of the fix is a registered **view**, created by the odp plugin's
bootstrap so it exists exactly when its source tables do:

- **`services/tap-api/tap_api/plugins/obscore.py` (new)** — the 29 mandatory
  columns as data (name, datatype, arraysize, xtype, unit, ucd, utype
  transcribed from REC Table 6, `std = 1`, `principal = 1`), the view DDL,
  and `ensure_obscore(conn)` which runs `CREATE SCHEMA IF NOT EXISTS ivoa`,
  `DROP VIEW IF EXISTS` + `CREATE VIEW ivoa.obscore` (drop-and-create so a
  mapping change migrates forward; atomic inside the bootstrap transaction),
  upserts the TAP_SCHEMA registration (schema/table/columns, table utype
  `ivo://ivoa.net/std/ObsCore#core-1.1`), and grants
  `USAGE`/`SELECT` on schema `ivoa` to the query role at runtime — the same
  pattern `srcnet` uses; `db/init/05_roles.sql` stays untouched.
- **View mapping** — one row per data product (the product already aggregates
  its artifacts' axes), joining `srcnet.data_products` to
  `srcnet.observations` and picking one representative artifact for the
  access columns via `LEFT JOIN LATERAL ... WHERE semantics = 'science'
  ORDER BY artifact_id LIMIT 1` (NULL `access_url` is spec-legal — the
  DataLink pattern). Decided mappings: `dataproduct_type = 'table'` (in the
  srcnet CHECK but not the ObsCore vocabulary) maps to `'measurements'` via
  `CASE`; `obs_collection = COALESCE(observations.collection,
  'unclassified')` to honour NOT NULL; `obs_publisher_did` is a configurable
  prefix plus the PK chain
  `<prefix><project_id>/<obs_id>/<sbd_id>/<eb_id>/<product_id>` — new
  setting `TAP_OBSCORE_DID_PREFIX` (Helm `obscore.didPrefix`, default
  `ivo://skao.int/~?`, prefix validated before SQL-literal interpolation; the
  authority must match the registry `authorityId` in a real deployment since
  a PublisherDID is a permanent promise); `access_estsize =
  round(bytes/1000.0)::bigint` (kbyte); `s_resolution` from `beam_size`;
  `t_resolution` and `em_res_power` NULL (permitted); `s_region` is
  registered with `xtype = 'adql:REGION'` now even though the stored value
  is a plain STC-S string — region *functions* over it arrive with
  Package 7, whose geometry column this view inherits.
- **Bootstrap wiring** — an optional `post_ensure` hook on `MetadataPlugin`
  (`libs/tapcore/tapcore/metadata/plugins.py`), invoked by
  `ingest.ensure_schema()` after the plugin's tables and registration are
  ensured, same connection and transaction, advisory xact lock still held
  (concurrent pods safe); `odp.py` wires
  `post_ensure=obscore.ensure_obscore`. `main.py::lifespan` already calls
  `forget_published_tables()` after bootstrap, so the view is immediately
  queryable. The `software` plugin is unaffected — the hook lives only on
  odp.
- **Capability declaration** — `_capability_elements()` gains an
  `_obscore_active()` flag keyed off the active plugins and, when true,
  emits `<dataModel ivo-id="ivo://ivoa.net/std/ObsCore#core-1.1">
  ObsCore-1.1</dataModel>` after `</interface>` and before `<language>`
  (TAPRegExt element order). `voresource_xml()` reuses the same block, so
  the registry record inherits it — and the REC's rule that the identifier
  may only be declared once the table with all mandatory columns exists
  holds structurally, because the flag and the view key off the same plugin.
- **utype/xtype plumbing** — `tables_xml()` additionally selects and emits
  table `utype`, column `utype`, and column `xtype` as the `extendedType`
  attribute on `<dataType>` (VODataService order); the JSON `/tables` in
  `endpoints/json_api.py` adds the same fields. Optional follow-up:
  `tapcore/query/results.py::tap_schema_metadata()` carrying utype/xtype
  into result VOTable FIELDs.
- **Tests** — unit: the column list is exactly the 29 mandatory names with
  spot-checked units/ucds/utypes and the view SQL contains the CASE,
  COALESCE and lateral-pick clauses; capabilities contain the `dataModel`
  element with odp active and not otherwise. Component: `/tables` lists
  `ivoa.obscore` and pyvo sees the metadata; ingest a notification through
  `/api/v1/notifications`, then `SELECT * FROM ivoa.ObsCore` over TAP sync
  returns one row per product with the constructed DID (ADQL case folding
  makes the REC's case-insensitive `ivoa.ObsCore` work unquoted). Optional
  external check: stilts `taplint` against the composed stack.
- **Extras** — an ObsCore cone-search entry in `/tap/examples` gated on the
  same flag, and a `docs/obscore.md` recording the column mapping and the
  DID scheme.

