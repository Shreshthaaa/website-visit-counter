# Decisions

What was chosen, what it cost, and what would change it. Written while
building, so some entries record a decision that was wrong the first time.

---

## The API is a Deployment, each Redis shard is a StatefulSet

The API holds nothing a replacement pod needs. Any replica can answer any
request, pods are interchangeable, and the only in-memory state is a write
buffer that gets flushed on shutdown. That is a Deployment.

Redis holds the counts. Each shard needs a stable network name because the
consistent hash ring is built from those names when the application starts, and
it needs its own volume that survives a restart. That is a StatefulSet with a
headless Service.

Cost: a StatefulSet is slower to roll and more fields on it are immutable. One
of those immutable fields cost a deployment during development (see below).

## One StatefulSet per shard, not one StatefulSet with two replicas

A single StatefulSet scaled to 2 would give `redis-0` and `redis-1`, which
looks like two shards and is cheaper to write. It was rejected because the two
shards are not interchangeable. They listen on different ports (7070 and 7071),
they hold disjoint key ranges, and adding a third means rebalancing the ring
rather than bumping a replica count.

Templating the shards from a list in `values.yaml` keeps that honest:

```yaml
redis:
  shards:
    - name: shard-a
      port: 7070
    - name: shard-b
      port: 7071
```

Cost: more rendered objects, and `helm template` output gets long. Adding a
shard is still a values edit, and the comment above that list says plainly that
it changes the ring and existing keys need rebalancing.

## Writes are buffered in memory and flushed on a timer

Inherited from the baseline application and kept deliberately. Every visit goes
into a per-process dictionary and reaches Redis every 30 seconds, which turns
a write per request into one pipeline per page per interval.

The cost is stated in the metric name. `visit_counter_buffer_depth` is
documented as "the amount of data at risk if the process dies right now", and
`FLUSH_INTERVAL_SECONDS` is the upper bound on how much traffic a hard crash
loses. Graceful shutdown closes the ordinary case of a rolling update or a
node drain. A `kill -9` or a node power loss still loses the buffer.

Lower the interval and the window shrinks at the cost of Redis writes. The
right number depends on how much a single visit is worth, which is a product
question rather than a technical one.

## The buffer is swapped atomically instead of locked

The flush used to take a lock, write to Redis, and release it, which blocks
writers for the length of a network round trip. It now does:

```python
pending, self._buffer = self._buffer, defaultdict(int)
```

There is no `await` between those two operations, so in asyncio nothing else
runs in between and the swap is atomic without a lock. Writers coming in during
the Redis call land in the fresh dictionary and are never blocked.

A running total is kept alongside the buffer as an integer, because the metric
used to call `sum()` over every buffered page on the hot path.

## Reads check the local buffer first, so consistency is per replica

A read returns the Redis value plus whatever that replica has buffered for the
page. Within one replica a visit is visible immediately. Across replicas the
count converges within the flush interval, which `docs/evidence/convergence.txt`
measures at 10 seconds for a 30 second interval.

This was a choice between read-your-writes everywhere and not paying for a
round trip on every write. Sticky sessions at the ingress would give
read-your-writes per client, and they would also concentrate traffic on
whichever replica a hot page landed on. For a visit counter, a count that is
briefly low is cheaper than that.

There is a five second read cache in front of this, which is why `served_via`
sometimes reports `in-memory`. It bounds staleness explicitly rather than
letting it depend on load.

## Helm, not raw manifests or kustomize

Three things pushed this way. The Redis shards need a loop. The release
needs to be rollback-able, and `helm rollback` to a revision moves running code
with one command, which `docs/evidence/rollback.txt` demonstrates. And the
image tag changes on every deploy, which wants a parameter rather than a
`sed`.

Cost: template errors that only appear at render time, and Go templating in
YAML is easy to get subtly wrong. The Grafana dashboard JSON is loaded with
`.Files.Get` rather than inlined, because Grafana legends use `{{label}}` and
Helm would try to evaluate them.

## The image is tagged with the git SHA, never `latest`

`make deploy` tags the image `visit-counter:$(git rev-parse --short=12 HEAD)`
and passes the same string in as a build argument, so `/version` reports it
from inside the running pod.

The practical consequence is that uncommitted code cannot be deployed. The tag
does not change, so Helm sees no difference and does nothing. That is annoying
once and correct forever: anything running can be pointed at an exact commit.

`latest` would make every rollback a guess.

## CI builds and scans; a human deploys

The pipeline runs tests and lint, scans the full git history for secrets with
gitleaks, lints and renders the chart, then builds the image, generates an
SBOM, scans it with Trivy and pushes to GHCR. It stops there.

Continuous deployment was scoped out. The honest reason is time: Argo CD on a
local kind cluster is a day of work to set up and demonstrate properly, and
that day was better spent on the SLO and the evidence transcripts. The
boundary is drawn where the artifact becomes trustworthy, which is a defensible
place to draw it even with unlimited time, since the next step needs a cluster
to pull into.

What exists today is a `make deploy` that takes the current commit to the
cluster in one command, and a `make rollback` that undoes it.

## Trivy is pinned to an exact release, not a major tag

`aquasecurity/trivy-action@0.35.0`, not `@master` or `@0`.

In March 2026, 76 of that action's 77 tags were force-pushed to point at
credential-stealing malware (GHSA-69fq-xp46-6x23). Anyone pinned to a mutable
tag ran it. `0.35.0` is covered by immutable releases.

The general rule this encodes: a third-party action runs with access to the
workflow's secrets, so the version has to be something an attacker cannot
repoint.

## Python 3.11 is the floor

`requires-python = ">=3.11"`, matching the `python:3.11-slim` base image.

This was set after two bugs that only existed on older versions. `list[str] |
None` in a function signature is evaluated at runtime on 3.9 and raises, which
`from __future__ import annotations` now prevents throughout. And ruff's UP041
rewrote `asyncio.TimeoutError` to the builtin `TimeoutError`, which are
different classes before 3.11, so the flush loop stopped catching its own
timeout. Declaring the floor makes the first fix unnecessary and the second
rule correct.

## NetworkPolicies ship enabled and are not enforced here

Both policies are in the chart and both select the right pods. kind's default
CNI ignores NetworkPolicy, so nothing enforces them on this cluster.

They ship anyway because the chart is the thing that would be installed on a
real cluster, where Calico or Cilium would enforce them. The values file and
the evidence transcript both say they are unenforced here, which seemed better
than either shipping a policy that silently does nothing or shipping nothing at
all.

## Memory has a limit, CPU does not

A CPU limit throttles rather than kills, and a throttled replica fails its
latency SLO while looking healthy. Requests are set so the scheduler places
pods sensibly, and the HPA scales on CPU at 70% of request.

Memory is limited at 256Mi because a memory leak with no limit takes the node
down with it, and an OOMKill is a clear signal.

## The SLO is on read latency, and the alerts are burn rate

99% of count reads answered within 200ms, measured over 30 days.

200ms is an actual bucket edge in the histogram. A threshold between buckets
would make the SLI an interpolation rather than a count, which is a quiet way
to report a number that is not true.

Alerting is multi-window burn rate rather than a threshold on latency. A
threshold rule either pages on every brief spike or misses slow degradation.
The fast rule needs 14.4x burn on both a 5 minute and a 30 minute window,
roughly "a 30 day budget gone in two days, and still going". The slow rule
catches 6x over 30 minutes and 2 hours. Three other rules cover things a
latency SLO cannot see: visits actually lost, a shard down, and a flush backlog
building up.

Nothing is labelled by `page_id` anywhere in the metrics. It comes from the
caller, so it is unbounded, and a single crawler could create a series per URL
until Prometheus falls over. The HTTP metrics label the matched route template
instead.

## Redis password lives in a Secret, generated only for dev

The chart takes `redis.auth.existingSecret` for anything real. If that is
empty it generates a password on install and stores it in a Secret, which is a
convenience for a local cluster and nothing more.

The password is written to a `redis.conf` file rather than passed as
`--requirepass`, because command line arguments are visible in `ps` output
inside the container. Probes authenticate through `REDISCLI_AUTH` for the same
reason.

## Mistakes worth keeping on the record

Two immutable fields cost real time and are visible as `failed` revisions in
`helm history`.

`Deployment.spec.selector` is immutable. Adding `component: api` to the
selector to fix the PodDisruptionBudget required deleting the Deployment.

`StatefulSet.spec.podManagementPolicy` is immutable. It had been set to
`Parallel`, which is a no-op at one replica, and removing it as dead config
broke the next two upgrades. Both StatefulSets had to be deleted and
recreated; the PVCs survived, so no data was lost.

The lesson both times is that a field being pointless is not a reason to remove
it from a chart that is already installed somewhere.
