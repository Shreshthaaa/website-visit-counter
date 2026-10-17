.DEFAULT_GOAL := help
SHELL := /bin/bash

# Used as the image tag and baked into the image, so /version reports it.
GIT_SHA       ?= $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo local)
IMAGE_REPO    ?= visit-counter
IMAGE_TAG     ?= $(GIT_SHA)
IMAGE         := $(IMAGE_REPO):$(IMAGE_TAG)

CLUSTER       ?= visit-counter
NAMESPACE     ?= visit-counter
RELEASE       ?= visit-counter
CHART         := charts/visit-counter
HOST          ?= visit-counter.localtest.me
BASE_URL      ?= http://$(HOST):8080

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- local dev

.PHONY: test
test: ## Run the test suite
	pytest

.PHONY: lint
lint: ## Lint the application
	ruff check .

.PHONY: compose-up
compose-up: ## Run the stack with docker compose
	GIT_SHA=$(GIT_SHA) docker compose up --build -d

.PHONY: compose-down
compose-down: ## Stop the compose stack and delete its volumes
	docker compose down -v

# ---------------------------------------------------------------- cluster

.PHONY: cluster-up
cluster-up: ## Create the kind cluster and install the ingress controller
	kind create cluster --config kind/cluster.yaml
	kubectl --context kind-$(CLUSTER) apply -f \
		https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/kind/deploy.yaml
	kubectl --context kind-$(CLUSTER) -n ingress-nginx wait \
		--for=condition=ready pod --selector=app.kubernetes.io/component=controller --timeout=180s

# --kubelet-insecure-tls is needed because kind's kubelet uses a self-signed
# cert. Don't copy it to a real cluster.
.PHONY: metrics-server
metrics-server: ## Install metrics-server (needed by the HPA)
	kubectl --context kind-$(CLUSTER) apply -f \
		https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.1/components.yaml
	kubectl --context kind-$(CLUSTER) -n kube-system patch deployment metrics-server \
		--type=json -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
	kubectl --context kind-$(CLUSTER) -n kube-system rollout status deployment/metrics-server --timeout=120s

.PHONY: cluster-down
cluster-down: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)

# ---------------------------------------------------------------- build

.PHONY: build
build: ## Build the image, tagged with the git SHA
	docker build \
		--build-arg GIT_SHA=$(GIT_SHA) \
		--build-arg APP_VERSION=$(shell grep '^appVersion' $(CHART)/Chart.yaml | cut -d'"' -f2) \
		--build-arg BUILD_TIME=$(shell date -u +%Y-%m-%dT%H:%M:%SZ) \
		-t $(IMAGE) .

.PHONY: load
load: build ## Load the image into kind (no registry needed locally)
	kind load docker-image $(IMAGE) --name $(CLUSTER)

# ---------------------------------------------------------------- deploy

.PHONY: chart-lint
chart-lint: ## Lint and render the chart without applying anything
	helm lint $(CHART)
	helm template $(RELEASE) $(CHART) --namespace $(NAMESPACE) >/dev/null

.PHONY: deploy
deploy: load ## Install or upgrade the release at the current commit
	helm upgrade --install $(RELEASE) $(CHART) \
		--namespace $(NAMESPACE) --create-namespace \
		--set image.repository=$(IMAGE_REPO) \
		--set image.tag=$(IMAGE_TAG) \
		--set ingress.host=$(HOST) \
		--wait --timeout 5m
	@echo "deployed $(IMAGE)"

.PHONY: status
status: ## Show what is running
	kubectl -n $(NAMESPACE) get pods,svc,ingress,hpa,pdb,netpol,pvc

.PHONY: rollback
rollback: ## Roll back to the previous release revision
	helm rollback $(RELEASE) --namespace $(NAMESPACE) --wait
	helm history $(RELEASE) --namespace $(NAMESPACE)

.PHONY: uninstall
uninstall: ## Remove the release (PVCs survive on purpose)
	helm uninstall $(RELEASE) --namespace $(NAMESPACE)

# ---------------------------------------------------------------- verify

# Flush interval plus a few seconds, so every replica has flushed.
FLUSH_WAIT ?= 35
PAGE       ?= smoke-$(shell date +%s)

.PHONY: smoke
smoke: ## Verify the deployed SHA, shard routing, and buffer convergence
	@echo "--- deployed version (expect $(GIT_SHA))"
	@curl -fsS $(BASE_URL)/version 2>/dev/null || \
		kubectl -n $(NAMESPACE) exec deploy/$(RELEASE) -- \
			python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/version').read().decode())"
	@echo
	@echo "--- recording 6 visits to $(PAGE)"
	@for i in $$(seq 1 6); do curl -fsS -X POST $(BASE_URL)/api/v1/counter/visit/$(PAGE) >/dev/null; done
	@echo "--- immediate read (expect LESS than 6)"
	@curl -fsS $(BASE_URL)/api/v1/counter/visits/$(PAGE); echo
	@echo "    The Service round-robins, so those 6 writes are buffered across"
	@echo "    $$(kubectl -n $(NAMESPACE) get deploy/$(RELEASE) -o jsonpath='{.status.readyReplicas}') replicas. A read sees Redis plus the"
	@echo "    buffer of whichever replica answered - not the other replicas'."
	@echo "--- waiting $(FLUSH_WAIT)s for every replica to flush"
	@sleep $(FLUSH_WAIT)
	@echo "--- converged read (expect exactly 6)"
	@curl -fsS $(BASE_URL)/api/v1/counter/visits/$(PAGE); echo
	@echo
	@echo "--- shard routing: these must not all name the same shard"
	@for p in alpha beta gamma delta; do \
		curl -fsS -X POST $(BASE_URL)/api/v1/counter/visit/$$p >/dev/null; \
		printf '%-8s ' $$p; curl -fsS $(BASE_URL)/api/v1/counter/visits/$$p; echo; \
	done

.PHONY: converge
converge: ## Measure how long buffered visits take to reach Redis
	@echo "--- 20 visits to $(PAGE), then polling until the count settles"
	@for i in $$(seq 1 20); do curl -fsS -X POST $(BASE_URL)/api/v1/counter/visit/$(PAGE) >/dev/null; done
	@start=$$(date +%s); \
	for i in $$(seq 1 20); do \
		n=$$(curl -fsS $(BASE_URL)/api/v1/counter/visits/$(PAGE) | sed 's/[^0-9]*\([0-9]*\).*/\1/'); \
		printf 't+%-3ss count=%s\n' $$(( $$(date +%s) - start )) "$$n"; \
		[ "$$n" = "20" ] && break; \
		sleep 3; \
	done

.PHONY: chaos
chaos: ## Kill a pod mid-buffer and prove no visits are lost
	@echo "--- 40 visits, then delete the pod before the flush interval elapses"
	@for i in $$(seq 1 40); do curl -fsS -X POST $(BASE_URL)/api/v1/counter/visit/chaos >/dev/null; done
	@kubectl -n $(NAMESPACE) delete pod -l app.kubernetes.io/instance=$(RELEASE),app.kubernetes.io/component=api --wait=true
	@kubectl -n $(NAMESPACE) rollout status deploy/$(RELEASE) --timeout=120s
	@sleep 3
	@echo "--- count after the kill (expect 40)"
	@curl -fsS $(BASE_URL)/api/v1/counter/visits/chaos; echo
