# Evidence

Every claim this project makes is backed by a transcript in `docs/evidence/`.
Regenerate all of them with:

```bash
bash scripts/evidence.sh            # all nine
bash scripts/evidence.sh chaos      # just one
```

The numbers below were captured on 2026-10-18 at commit `3ee811c3f552`,
against the kind cluster described in the README. Each transcript begins with
its own timestamp and commit, so a reader can tell when it was taken and
whether it matches the code they are looking at.

That stamp is the last commit that changed runtime behaviour, so it sits
behind `HEAD` by however many documentation commits came after it. Capturing
evidence of a commit before making it is not possible, and re-running the
demos after every README edit would be churn. If the transcripts ever name a
commit that changed application code, regenerate them.

Committing generated output is usually wrong, and it is deliberate here. On a
real repository these would be produced by a CI job and kept as build
artifacts. They are in git because the audience is a person reading the
repository, who should not need a Kubernetes cluster to see what the system
did.

---

## A deployed pod can be traced back to a commit

`docs/evidence/version.txt`

```
git rev-parse --short=12 HEAD      3ee811c3f552
deploy image                       visit-counter:3ee811c3f552
GET /version                       {"version":"0.1.0","git_sha":"3ee811c3f552"}
```

The same twelve characters appear in git, in the image tag, and in the
response from the running pod. The SHA is passed as a build argument and baked
into the image, so `/version` reports what was actually built rather than what
a config file claims.

A side effect worth knowing about: uncommitted work is undeployable. `make
deploy` tags the image with `git rev-parse HEAD`, so if you edit code without
committing, Helm sees the same image tag it already deployed and does nothing.
That cost about twenty minutes during development before the cause was obvious.

## A visit is recorded and read back through the ingress

`docs/evidence/journey.txt`

```
POST /api/v1/counter/visit/journey-1792327754
  {"status":"success","message":"Visit recorded for page journey-1792327754"}

GET  /api/v1/counter/visits/journey-1792327754
  {"count":0,"served_via":"redis_7071"}
```

The count is zero on purpose, and the reason is the whole design. Writes land
in an in-memory buffer and reach Redis on a 30 second timer. The Service
round-robins across three replicas, so the POST was buffered by one replica and
the GET was answered by a different one, which saw only what Redis held. The
next section measures how long that lasts.

`served_via` names the Redis shard that answered, or `in-memory` when the
replica's own read cache was still valid.

## Counts converge within the flush interval

`docs/evidence/convergence.txt`

Thirty visits sent to one page across three replicas, then read every five
seconds:

```
t+0s    count=7
t+5s    count=17
t+10s   count=30
```

Ten seconds to full consistency against a 30 second flush interval, because the
three replicas are on independent timers and their flushes landed at different
points in the cycle. The worst case is one full interval. Lowering
`app.flushIntervalSeconds` narrows the window and costs more Redis writes.

Reads inside a single replica are immediately consistent, because a read checks
that replica's own buffer before going to Redis. Across replicas the guarantee
is eventual.

## Keys are distributed across both shards

`docs/evidence/sharding.txt`

```
alpha      served_via redis_7071
beta       served_via redis_7070
gamma      served_via redis_7070
delta      served_via redis_7071
epsilon    served_via redis_7070
zeta       served_via redis_7071
```

Six keys, three to each shard, placed by the consistent hash ring rather than
by anything in the request. The counts in that transcript vary because these
pages carry visits from earlier demo runs; the shard assignment is the point.

If every key named the same shard, the ring would be collapsing, which is the
failure mode a plain `hash(key) % n` has when `n` changes.

## Buffered visits survive every replica being killed

`docs/evidence/chaos.txt`

This is the regression the graceful shutdown work exists to prevent. On the
baseline the buffer is in memory and nothing flushes it on SIGTERM, so a
rolling update silently drops whatever had not reached Redis yet.

```
40 visits sent to chaos-1792327765
kubectl delete pod -l component=api        3 pods deleted
kubectl rollout status                     successfully rolled out

GET /api/v1/counter/visits/chaos-1792327765
  {"count":40,"served_via":"redis_7071"}
```

All forty survived, having been deleted well inside the 30 second interval.
Three things have to line up for that to work: a `preStop` sleep so the pod
stops receiving traffic before it starts shutting down, a readiness probe that
fails first so the Service removes the pod, and a
`terminationGracePeriodSeconds` of 45 that leaves room for the 10 second
shutdown flush to finish before the kubelet sends SIGKILL.

The transcript ends with a count of pods that logged `shutdown flush complete`,
and it reads `0`. That check is worthless and has since been removed from the
script: it greps the logs of the pods that are running now, and the pods that
did the flushing were deleted. Their logs went with them. The recovered count
is the evidence.

## A release can be rolled back and the running version proves it

`docs/evidence/rollback.txt`

```
before rollback   {"version":"0.1.0","git_sha":"3ee811c3f552"}
helm rollback visit-counter 8                 Rollback was a success!
after rollback    {"version":"0.1.0","git_sha":"bc2173ff1977"}
helm rollback visit-counter 9                 Rollback was a success!
after roll forward {"version":"0.1.0","git_sha":"3ee811c3f552"}
```

The SHA reported by the live pod changes and changes back, so the rollback
moved running code rather than just a Helm record.

`helm history` in that transcript also shows revisions 2, 4 and 5 as `failed`,
and those failures are real. Two of them were Kubernetes refusing to mutate an
immutable field: `spec.selector` on the Deployment, and `podManagementPolicy`
on both Redis StatefulSets. Both needed the object deleted and recreated. The
useful part is that the failed upgrades left the previous revision serving
traffic; Helm did not take the release down to apply a change the API server
had already rejected.

## Runtime security posture

`docs/evidence/security.txt`

Read from the live cluster rather than from the chart:

```
pod    runAsNonRoot: true, runAsUser/Group 10001, seccompProfile RuntimeDefault
container  allowPrivilegeEscalation: false, capabilities drop ALL,
           readOnlyRootFilesystem: true

id                             uid=10001(app) gid=10001(app)
echo test > /proof             Read-only file system
ls site-packages | grep pip    pip, setuptools and wheel are absent
ls /var/run/secrets/kubernetes.io/   no token mounted
configmap grep -ci password    0
secret keys                    ['redis-password']
```

Dropping pip, setuptools and wheel from the runtime image was not cosmetic. A
Trivy scan found four HIGH findings coming from `wheel` and `jaraco.context`
vendored inside setuptools, none of which the application imports. Removing
them from site-packages cleared all four, and the test suite still passes
without them.

The remaining three HIGH findings were real Starlette CVEs
(CVE-2024-47874, CVE-2026-48818, CVE-2026-54283). FastAPI only requires
`starlette>=0.46.0`, which still resolves to vulnerable versions, so
`requirements.txt` pins `starlette==1.8.0` explicitly on top of
`fastapi==0.143.3`.

Both NetworkPolicies exist and select the right pods. They are not enforced
here: kind's default CNI ignores NetworkPolicy entirely. The transcript says so
rather than implying the rules are doing something. On a cluster running
Calico or Cilium they would restrict ingress to the API pods and limit Redis to
traffic from those pods.

## Workload shape

`docs/evidence/workloads.txt`

```
deployment.apps/visit-counter                3/3
statefulset.apps/visit-counter-redis-shard-a 1/1
statefulset.apps/visit-counter-redis-shard-b 1/1
service/visit-counter-redis-shard-a          ClusterIP None (headless)
pdb/visit-counter                            minAvailable 2, allowed disruptions 1
pvc data-...-shard-a-0                       Bound, 1Gi
pvc data-...-shard-b-0                       Bound, 1Gi
```

The stateless API is a Deployment and each Redis shard is its own StatefulSet
with its own volume and a stable name. The headless Services give each shard a
DNS name that does not move when the pod restarts, which matters because the
hash ring is built from those names at startup.

The PDB reads `ALLOWED DISRUPTIONS 1` against `minAvailable 2`, which is
correct for three API pods. It read `3` earlier in development because the
selector matched every pod in the namespace, including the Redis pods, so the
budget thought it was protecting five. Adding `component: api` to the selector
fixed it, and the before and after numbers are the easiest way to show a PDB is
actually doing its job.

The HPA shows `cpu: <unknown>/70%` in this capture because metrics-server was
not installed on the cluster at the time. Run `make metrics-server` and the
target resolves to a real percentage. The autoscaler is configured either way,
min 3 and max 6.

## SLO and alerting

`docs/evidence/observability.txt`

```
SLO: 99% of count reads answered within 200ms

  reads under 200ms    100.0000%
  error budget burn    0.00x  (alerts above 14.4x)
  p95 latency          5.4 ms
  visits buffered      0  (lost if every pod died now)

5 alert rules loaded and evaluating

  VisitCounterFlushBacklog         warning   inactive
  VisitCounterReadLatencyBurnFast  critical  inactive
  VisitCounterReadLatencyBurnSlow  warning   inactive
  VisitCounterShardDown            critical  inactive
  VisitCounterVisitsLost           critical  inactive
```

`inactive` means the rule is loaded and evaluating and its condition is false,
which is what a healthy system looks like. A rule that was never loaded would
be absent from the list rather than inactive, so this output distinguishes the
two.

200ms is an explicit bucket edge in the latency histogram. If the SLO threshold
fell between two buckets, Prometheus would interpolate and the SLI would be an
estimate rather than a count.

The two latency alerts are multi-window burn rate rules rather than simple
threshold rules. The fast one needs 14.4x burn sustained over both 5 minutes
and 30 minutes before it fires, which is roughly "this will exhaust a 30 day
error budget in two days". The 30 minute window stops a brief spike from
paging anyone. The slow one catches 6x burn over 30 minutes and 2 hours, the
kind of steady degradation a threshold alert tends to miss.

## What is not demonstrated here

- NetworkPolicy enforcement, for the kind CNI reason above.
- HPA scaling under load. The policy is configured and visible, and no load
  test was run to make it scale.
- Continuous deployment. CI builds, scans and publishes the image; deploying it
  is a `make deploy` someone runs. `docs/DECISIONS.md` explains why that line
  was drawn there.
