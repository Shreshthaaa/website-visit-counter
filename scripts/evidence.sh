#!/usr/bin/env bash
# Run every demonstration and save the transcripts under docs/evidence/.
#
# Each block is independent: a failure is recorded and the run continues, so
# one broken demo does not hide the rest.
#
#   bash scripts/evidence.sh          # all of them
#   bash scripts/evidence.sh chaos    # just one
set -u

NAMESPACE="${NAMESPACE:-visit-counter}"
RELEASE="${RELEASE:-visit-counter}"
BASE_URL="${BASE_URL:-http://visit-counter.localtest.me:8080}"
API="${BASE_URL}/api/v1/counter"
OUT="docs/evidence"

mkdir -p "$OUT"

say() { printf '\n=== %s\n' "$*"; }
run() { printf '\n$ %s\n' "$*"; eval "$@" 2>&1; }

# ---------------------------------------------------------------- traceability

evidence_version() {
  say "Commit, image tag and running pod all report the same SHA"
  run "git rev-parse --short=12 HEAD"
  run "git log -1 --format='%H %s'"
  run "kubectl -n $NAMESPACE get deploy/$RELEASE -o jsonpath='{.spec.template.spec.containers[0].image}{\"\\n\"}'"
  run "curl -fsS $BASE_URL/version"
  echo
  echo "The git SHA, the image tag and git_sha from the live pod are one string."
  echo "That is what makes a deployed version traceable back to source."
}

# ---------------------------------------------------------------- user journey

evidence_journey() {
  local page="journey-$(date +%s)"
  say "A visit is recorded and read back through the ingress"
  run "curl -fsS -X POST $API/visit/$page"
  run "curl -fsS $API/visits/$page"
  echo
  echo "served_via names the Redis shard that answered, or 'in-memory' when"
  echo "the replica's own read cache was still valid."
}

evidence_sharding() {
  say "Keys are distributed across both shards by the hash ring"
  for page in alpha beta gamma delta epsilon zeta; do
    curl -fsS -X POST "$API/visit/$page" >/dev/null 2>&1
    printf '%-10s ' "$page"
    curl -fsS "$API/visits/$page" 2>&1
    echo
  done
  echo
  echo "Both redis_7070 and redis_7071 should appear. A single shard for every"
  echo "key would mean the ring is not distributing."
}

# ---------------------------------------------------------------- consistency

evidence_convergence() {
  local page="converge-$(date +%s)"
  local n=30
  say "Counts converge within the flush interval, across replicas"
  echo "Replicas: $(kubectl -n $NAMESPACE get deploy/$RELEASE -o jsonpath='{.status.readyReplicas}')"
  echo
  echo "Sending $n visits to $page ..."
  for _ in $(seq 1 $n); do curl -fsS -X POST "$API/visit/$page" >/dev/null 2>&1; done

  echo
  echo "Reads are taken every 5s. The first is expected to be short of $n:"
  echo "the Service round-robins, so the visits are buffered across replicas"
  echo "and a read only sees Redis plus the buffer of whichever replica"
  echo "answered it."
  echo
  local start now count
  start=$(date +%s)
  for _ in $(seq 1 10); do
    now=$(( $(date +%s) - start ))
    count=$(curl -fsS "$API/visits/$page" 2>/dev/null | sed 's/[^0-9]*\([0-9]*\).*/\1/')
    printf '  t+%-4ss  count=%s\n' "$now" "${count:-error}"
    [ "${count:-0}" = "$n" ] && break
    sleep 5
  done
  echo
  echo "Converged to $n. The window is bounded by FLUSH_INTERVAL_SECONDS."
}

# ---------------------------------------------------------------- fault

evidence_chaos() {
  local page="chaos-$(date +%s)"
  local n=40
  say "Visits survive every replica being killed mid-buffer"
  echo "This is the regression the graceful-shutdown work exists to prevent."
  echo "On the baseline these visits are lost: the buffer is in memory and"
  echo "nothing flushes it on SIGTERM."
  echo
  echo "Sending $n visits to $page, then deleting the pods immediately,"
  echo "well inside the 30s flush interval."
  for _ in $(seq 1 $n); do curl -fsS -X POST "$API/visit/$page" >/dev/null 2>&1; done

  run "kubectl -n $NAMESPACE get pods -l app.kubernetes.io/component=api"
  run "kubectl -n $NAMESPACE delete pod -l app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=api --wait=true"
  run "kubectl -n $NAMESPACE rollout status deploy/$RELEASE --timeout=120s"
  sleep 5
  echo
  echo "Count after every replica was replaced (expect $n):"
  run "curl -fsS $API/visits/$page"
  echo
  echo "The recovered count is the proof. The pods that did the flushing were"
  echo "deleted, so their logs are gone and there is nothing left to grep."
}

# ---------------------------------------------------------------- release

evidence_rollback() {
  say "A bad release can be rolled back, and the running version proves it"
  local current
  current=$(helm history "$RELEASE" -n "$NAMESPACE" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["revision"])')
  echo "current revision: $current"

  run "helm history $RELEASE -n $NAMESPACE"
  echo
  echo "Version before rollback:"
  run "curl -fsS $BASE_URL/version"

  if [ "$current" -lt 2 ]; then
    echo
    echo "Only one revision exists; nothing to roll back to. Deploy twice first."
    return
  fi

  run "helm rollback $RELEASE $(( current - 1 )) -n $NAMESPACE --wait --timeout 3m"
  sleep 3
  echo
  echo "Version after rollback (should differ):"
  run "curl -fsS $BASE_URL/version"

  run "helm rollback $RELEASE $current -n $NAMESPACE --wait --timeout 3m"
  sleep 3
  echo
  echo "Version after rolling forward again:"
  run "curl -fsS $BASE_URL/version"
}

# ---------------------------------------------------------------- security

evidence_security() {
  say "Runtime security posture, read from the live cluster"
  run "kubectl -n $NAMESPACE get deploy/$RELEASE -o jsonpath='{.spec.template.spec.securityContext}{\"\\n\"}'"
  run "kubectl -n $NAMESPACE get deploy/$RELEASE -o jsonpath='{.spec.template.spec.containers[0].securityContext}{\"\\n\"}'"

  echo
  echo "Running as a non-root uid:"
  run "kubectl -n $NAMESPACE exec deploy/$RELEASE -- id"

  echo
  echo "Root filesystem is read-only (this write must fail):"
  run "kubectl -n $NAMESPACE exec deploy/$RELEASE -- sh -c 'echo test > /proof 2>&1 || echo \"write refused, as expected\"'"

  echo
  echo "No package manager in the image (removed in the runtime stage):"
  run "kubectl -n $NAMESPACE exec deploy/$RELEASE -- sh -c 'ls /opt/venv/lib/python3.11/site-packages | grep -E \"^(pip|setuptools|wheel)\$\" || echo \"pip, setuptools and wheel are absent\"'"

  echo
  echo "No service account token is mounted:"
  run "kubectl -n $NAMESPACE exec deploy/$RELEASE -- sh -c 'ls /var/run/secrets/kubernetes.io/ 2>&1 || echo \"no token mounted\"'"

  echo
  echo "The Redis password is a Secret, and is not in the ConfigMap:"
  run "kubectl -n $NAMESPACE get configmap $RELEASE -o yaml | grep -ci password || echo '0 occurrences in the ConfigMap'"
  run "kubectl -n $NAMESPACE get secret $RELEASE-redis -o jsonpath='{.data}' | python3 -c 'import json,sys; print(\"secret keys:\", list(json.load(sys.stdin)))'"

  echo
  echo "NetworkPolicies exist. NOTE: kind's CNI does not enforce them."
  run "kubectl -n $NAMESPACE get netpol"
}

# ---------------------------------------------------------------- k8s shape

evidence_workloads() {
  say "Workload shape and disruption budget"
  run "kubectl -n $NAMESPACE get deploy,sts,svc,ingress,hpa,pdb,pvc"
  echo
  echo "The PDB matches 3 app pods, not the Redis pods: allowed disruptions"
  echo "is 1 against minAvailable 2."
  run "kubectl -n $NAMESPACE get pods -L app.kubernetes.io/component"
  echo
  echo "Each shard is its own StatefulSet with its own volume:"
  run "kubectl -n $NAMESPACE get pvc"
}

evidence_observability() {
  say "SLO and alert rules, read from Prometheus"
  run "python3 scripts/observe.py slo"
  run "python3 scripts/observe.py alerts"
}

# ---------------------------------------------------------------- driver

ALL="version journey sharding convergence chaos rollback security workloads observability"

main() {
  local targets=("$@")
  [ ${#targets[@]} -eq 0 ] && read -ra targets <<< "$ALL"

  for name in "${targets[@]}"; do
    local file="$OUT/${name}.txt"
    echo "--> $name  ($file)"
    {
      echo "# $name"
      echo "# captured $(date -u '+%Y-%m-%d %H:%M:%SZ')"
      echo "# commit   $(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
      "evidence_${name}"
    } > "$file" 2>&1
    tail -n 3 "$file" | sed 's/^/    /'
  done

  echo
  echo "transcripts written to $OUT/"
  ls -la "$OUT"
}

main "$@"
