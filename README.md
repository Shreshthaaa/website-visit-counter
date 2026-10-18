# Website Visit Counter

A FastAPI service that counts page visits, backed by two sharded Redis
instances. Visits are buffered in memory and flushed to Redis on a timer, keys
are placed across shards by a consistent hash ring, and reads are served from a
short-lived local cache when one is valid.

This repository holds the application and everything needed to run it on
Kubernetes: a Helm chart, a kind cluster definition, a CI pipeline that builds
and scans the image, Prometheus rules with an SLO, a Grafana dashboard, and a
set of scripts that demonstrate each of those working.

## What runs where

| | |
|---|---|
| API | Deployment, 3 replicas, HPA 3 to 6 on CPU |
| Redis | one StatefulSet per shard, ports 7070 and 7071, 1Gi volume each |
| Ingress | ingress-nginx at `visit-counter.localtest.me:8080` |
| Metrics | ServiceMonitor scraped by kube-prometheus-stack |
| Alerts | 5 rules, two of them multi-window burn rate on the latency SLO |

## Prerequisites

- Docker
- `kind`, `kubectl` and `helm`
- Python 3.11 or newer, for the test suite and the operator scripts
- `make`

Nothing needs a registry. The image is built locally and loaded straight into
the kind cluster.

## Run it without a cluster

```bash
cp .env.example .env
make compose-up
curl localhost:8000/version
```

This brings up the app and both Redis shards under Docker Compose. Tear it
down with `make compose-down`, which also removes the volumes.

## Run it on Kubernetes

From an empty machine to a working cluster:

```bash
make cluster-up          # kind cluster plus ingress-nginx
make metrics-server      # the HPA needs this to read CPU
make deploy              # build, load into kind, helm upgrade --install
make smoke               # check it actually works
```

`make cluster-up` takes a couple of minutes, most of it waiting for the
ingress controller to become ready. `make deploy` tags the image with the
current git SHA and passes the same string in as a build argument, so
`/version` reports the commit the running pod was built from.

The service is reachable at `http://visit-counter.localtest.me:8080`.
`*.localtest.me` resolves to 127.0.0.1 without touching `/etc/hosts`, and the
kind config maps port 8080 to the ingress controller.

`make status` shows what is running. `make cluster-down` deletes the whole
cluster.

### One thing that will catch you

`make deploy` uses the git SHA as the image tag. If you change code without
committing, the tag does not change, Helm sees no difference, and nothing rolls
out. Commit first, then deploy.

## API

Both endpoints live under the `/api/v1` prefix.

Record a visit:

```bash
curl -X POST http://visit-counter.localtest.me:8080/api/v1/counter/visit/homepage
{"status":"success","message":"Visit recorded for page homepage"}
```

Read a count:

```bash
curl http://visit-counter.localtest.me:8080/api/v1/counter/visits/homepage
{"count":123,"served_via":"redis_7070"}
```

`served_via` names the Redis shard that answered, or `in-memory` when the
replica's own read cache was still valid.

A count read immediately after a write can come back low. Writes sit in a
per-replica buffer for up to 30 seconds, and the Service round-robins, so a
read may land on a replica that has not seen the write yet. It converges within
the flush interval. `docs/EVIDENCE.md` has the measurement.

Operational endpoints sit at the root: `/healthz` for liveness, `/readyz` for
readiness and shard reachability, `/version` for build identity, and `/metrics`
for Prometheus.

## Seeing each piece work

```bash
make smoke        # deployed SHA, shard routing, buffer convergence
make converge     # poll a count until buffered visits reach Redis
make chaos        # kill every replica mid-buffer, show nothing is lost
make rollback     # roll back one revision
```

Observability needs Prometheus and Grafana installed first. The release has to
be called `monitoring`, because the chart labels its ServiceMonitor and rules
to match that release:

```bash
make monitoring   # kube-prometheus-stack into the monitoring namespace
make deploy       # again, so the ServiceMonitor and rules get created
make traffic      # 400 request pairs, so there is something to look at
make slo          # SLI, error budget burn, p95
make alerts       # which rules are loaded and whether any are firing
make grafana      # port-forward, then open the Visit Counter dashboard
```

To capture all of it to disk:

```bash
bash scripts/evidence.sh
```

That writes nine transcripts to `docs/evidence/`, each stamped with the time
and commit it was taken at. `docs/EVIDENCE.md` walks through what each one
shows.

## Development

```bash
make test         # pytest
make lint         # ruff
make chart-lint   # helm lint plus a full template render
```

CI runs the same checks on every push, plus a gitleaks scan over the full git
history, a kubeconform validation of every rendered manifest against the
Kubernetes API schemas, a Trivy scan of the built image, and an SBOM. It also
runs the built image and asserts that the `GIT_SHA` it reports matches the
commit under build, so a mislabelled image fails CI rather than reaching a
cluster.

## Configuration

Chart values are in `charts/visit-counter/values.yaml`, with the settings worth
knowing commented in place. The ones that change behaviour most:

| Value | Default | Effect |
|---|---|---|
| `app.flushIntervalSeconds` | 30 | How long a visit can sit in memory, and the upper bound on what a hard crash loses |
| `app.cacheTtlSeconds` | 5 | How stale a read can be |
| `redis.shards` | 2 | Adding one changes the hash ring, so existing keys need rebalancing |
| `terminationGracePeriodSeconds` | 45 | Must exceed `preStopSleepSeconds` plus the shutdown flush timeout |
| `monitoring.slo.objective` | 0.99 | Target for reads answered within `latencyBucket` |

For local runs, `.env.example` lists every environment variable the
application reads. In the cluster these come from a ConfigMap, and the Redis
password from a Secret.

## Layout

```
app/              FastAPI service
  core/           config, Redis manager, consistent hash ring, metrics, logging
  services/       the write buffer and read path
charts/           Helm chart, Prometheus rules, Grafana dashboard
kind/             cluster definition with the ingress port mapping
scripts/          observe.py (SLO and alerts), evidence.sh (demos)
tests/            pytest suite
docs/             EVIDENCE.md, DECISIONS.md, evidence/ transcripts
```

## Known limitations

NetworkPolicies are in the chart and are not enforced on this cluster. kind's
default CNI ignores them. On a cluster running Calico or Cilium they would
restrict ingress to the API pods and limit Redis to traffic from those pods.

Consistency across replicas is eventual, bounded by the flush interval. A read
is immediately consistent only against the replica that took the write.

A hard crash loses the buffer. Graceful shutdown covers rolling updates and
node drains, which is the common case; `kill -9` and node power loss are not
covered.

The hash ring is built once at startup with no key migration. Changing
`redis.shards` moves keys and the counts behind them need rebalancing by hand.

Deployment is a command someone runs, not a controller that reconciles. CI
stops at a scanned, published, traceable image. `docs/DECISIONS.md` explains
where that line was drawn and why.

## Further reading

- `docs/EVIDENCE.md` walks through what was demonstrated, with the captured
  output and the real numbers.
- `docs/DECISIONS.md` records what was chosen, what it cost, and what would
  change it, including the mistakes that are still visible in `helm history`.
