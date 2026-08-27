# Benchmark suite entry points (benchmarks/egernia-performance).
#
# Every target is resumable: pass RESUME=<results-dir-name> to continue an
# interrupted run instead of starting a new one. Nothing here ever overwrites
# an existing results directory.
#
#   make benchmark-smoke                  short end-to-end pass on D1
#   make benchmark-db-scaling             concurrency sweep per dataset size
#   make benchmark-fixed-scaling          replica scaling, autoscalers off
#   make benchmark-keda                   autoscaling scenarios K1-K7
#   make benchmark-result-formats         every result writer over the same rows
#   make benchmark-stress                 just the stress classes (Q09/Q11/Q13/Q14)
#   make benchmark-shedding               held overload: 503s versus socket drops
#   make benchmark-profile                per-request CPU by subsystem, and a token's cost
#   make benchmark-replicas               a bracketed capacity per replica count
#   make benchmark-workers                a bracketed capacity per (workers, replicas) point
#   make benchmark-serialize              writers only, in process, no cluster
#   make benchmark-full                   every family, every dataset
#   make benchmark-report                 redraw plots and HTML for a run
#   make benchmark-publish RUN=<dir>      publish graphs to the docs site
#
#   make benchmark-setup                  cluster + KEDA + monitoring + chart
#   make benchmark-teardown               delete the kind cluster

SHELL := /bin/bash
PYTHON ?= $(CURDIR)/.venv/bin/python
BENCH_DIR := $(CURDIR)/benchmarks/egernia-performance
BENCH := cd $(BENCH_DIR) && PYTHONPATH=$(BENCH_DIR):$(CURDIR) $(PYTHON) -m egernia_bench
RESUME_ARG := $(if $(RESUME),--resume $(RESUME),)
NO_BUILD_ARG := $(if $(NO_BUILD),--no-build,)

.PHONY: benchmark-smoke benchmark-db-scaling benchmark-fixed-scaling \
        benchmark-keda benchmark-result-formats benchmark-stress benchmark-shedding benchmark-replicas benchmark-workers benchmark-serialize \
        benchmark-profile \
        benchmark-full benchmark-report benchmark-setup \
        benchmark-teardown benchmark-publish benchmark-help

benchmark-help:
	@sed -n '1,22p' $(firstword $(MAKEFILE_LIST))

benchmark-setup:
	$(BENCH) setup $(NO_BUILD_ARG)

benchmark-smoke:
	$(BENCH) smoke $(RESUME_ARG) $(NO_BUILD_ARG)

benchmark-db-scaling:
	$(BENCH) db-scaling $(RESUME_ARG) $(NO_BUILD_ARG) $(if $(DATASETS),--datasets $(DATASETS),)

benchmark-fixed-scaling:
	$(BENCH) fixed-scaling $(RESUME_ARG) $(NO_BUILD_ARG) $(if $(DATASET),--dataset $(DATASET),) $(if $(C1),--c1 $(C1),)

benchmark-keda:
	$(BENCH) keda $(RESUME_ARG) $(NO_BUILD_ARG) $(if $(DATASET),--dataset $(DATASET),) $(if $(SCENARIOS),--scenarios $(SCENARIOS),) $(if $(ASYNC_C1),--async-c1 $(ASYNC_C1),)

benchmark-result-formats:
	$(BENCH) result-formats $(RESUME_ARG) $(NO_BUILD_ARG) $(if $(DATASET),--dataset $(DATASET),)

benchmark-stress:
	$(BENCH) stress $(RESUME_ARG) $(NO_BUILD_ARG) $(if $(DATASET),--dataset $(DATASET),)

benchmark-shedding:
	$(BENCH) shedding $(RESUME_ARG) $(NO_BUILD_ARG) $(if $(DATASET),--dataset $(DATASET),)

benchmark-replicas:
	$(BENCH) replicas $(RESUME_ARG) $(NO_BUILD_ARG) $(if $(DATASET),--dataset $(DATASET),)

benchmark-workers:
	$(BENCH) workers $(RESUME_ARG) $(NO_BUILD_ARG) $(if $(DATASET),--dataset $(DATASET),)

# Package 18. Needs py-spy on this interpreter and passwordless sudo: the
# worker runs in the node's namespaces, so reading its stacks is a root
# operation on the host.
#   uv pip install --python .venv/bin/python py-spy==0.4.2
# NO_AUTH=1 skips the authenticated rungs (no OIDC stub, no chart upgrades).
# BLOCKING=1 pauses the worker to sample it: accurate, and measured here at
# -74% throughput plus a liveness-probe restart. See config/scenarios.yaml.
benchmark-profile:
	$(BENCH) profile $(RESUME_ARG) $(NO_BUILD_ARG) $(if $(DATASET),--dataset $(DATASET),) $(if $(CONCURRENCY),--concurrency $(CONCURRENCY),) $(if $(NO_AUTH),--no-auth,) $(if $(BLOCKING),--blocking,)

# No cluster: the writers on their own, so a change to the serialisation path
# can be measured in seconds instead of an afternoon.
benchmark-serialize:
	$(BENCH) serialize $(if $(ROWS),--rows $(ROWS),) $(if $(OUT),--out $(OUT),)

benchmark-full:
	$(BENCH) full $(RESUME_ARG) $(NO_BUILD_ARG) $(if $(DATASETS),--datasets $(DATASETS),)

benchmark-report:
	$(BENCH) report $(RUN)

# Copies the graphs, the per-measurement CSV and the provenance into
# docs/performance/, which the docs workflow already deploys to Pages.
benchmark-publish:
	$(BENCH) publish $(RUN)

benchmark-teardown:
	$(BENCH) teardown

# ---------------------------------------------------------------------------
# Multi-machine demo (deploy/demo): the service on a Kubernetes cluster you
# already have, the notebook on someone else's laptop.
#
#   make demo-help                        this list, and the current target
#   make demo-preflight                   what the cluster is and what it lacks
#   make demo-deploy                      deploy the chart via the current context
#   make demo-tunnel HOST=cluster-host    the ssh + /etc/hosts lines, and open it
#   make demo-tls                         a self-signed cert for egernia.test
#   make demo-dataset                     generate the ~100 GiB D5 dataset in-cluster
#   make demo-snapshot                    capture D5 so the next run is minutes
#   make demo-restore                     restore a captured D5
#   make demo-notebook                    marimo, against the deployed service
#   make demo-status                      URLs, replicas, dataset size
#   make demo-teardown                    remove the release (KEEP_DATA=1 keeps the PVC)
#
# The cluster is whatever `kubectl config current-context` points at. Every
# target that changes the cluster prints that context and refuses to guess:
# deploying 100 GiB into the wrong cluster is not an undo-able mistake.

DEMO_DIR := $(CURDIR)/deploy/demo
DEMO_NS ?= egernia-demo
DEMO_RELEASE ?= egernia
DEMO_VALUES ?= $(DEMO_DIR)/values-demo.yaml
DEMO_CONTEXT = $(shell kubectl config current-context 2>/dev/null)
DEMO_MARIMO := $(DEMO_DIR)/scaling_demo.py
# The name the notebook machine maps to 127.0.0.1. `.test` is reserved by
# RFC 6761, so it can never collide with a real domain.
DEMO_HOST ?= egernia.test

.PHONY: demo-help demo-preflight demo-deploy demo-tunnel demo-tls \
        demo-dataset demo-snapshot demo-restore demo-notebook demo-status demo-teardown

demo-help:
	@sed -n '/^# Multi-machine demo/,/^# deploying 100 GiB/p' $(firstword $(MAKEFILE_LIST))
	@echo
	@echo "current kubectl context: $(or $(DEMO_CONTEXT),<none>)"

demo-preflight:
	@$(DEMO_DIR)/preflight.sh

demo-deploy:
	@test -n "$(DEMO_CONTEXT)" || { echo "no current kubectl context" >&2; exit 2; }
	@echo "deploying to context '$(DEMO_CONTEXT)', namespace '$(DEMO_NS)', host '$(DEMO_HOST)'"
	helm upgrade --install $(DEMO_RELEASE) $(CURDIR)/deploy/helm/egernia \
	  --namespace $(DEMO_NS) --create-namespace \
	  --values $(DEMO_VALUES) \
	  --set ingress.host=$(DEMO_HOST) \
	  $(if $(INGRESS_CLASS),--set ingress.className=$(INGRESS_CLASS),) \
	  $(if $(STORAGE_CLASS),--set postgresql.storageClass=$(STORAGE_CLASS) --set results.storageClass=$(STORAGE_CLASS),) \
	  $(if $(TLS_SECRET),--set ingress.tls[0].secretName=$(TLS_SECRET) --set ingress.tls[0].hosts[0]=$(DEMO_HOST),) \
	  --wait --timeout 15m
	@$(MAKE) --no-print-directory demo-status

demo-tunnel:
	@$(DEMO_DIR)/tunnel.sh $(DEMO_NS) $(HOST)

demo-tls:
	@$(DEMO_DIR)/tls.sh $(DEMO_NS) $(DEMO_HOST)

demo-dataset:
	$(DEMO_DIR)/dataset.sh generate $(DEMO_NS) $(DEMO_RELEASE)

demo-snapshot:
	$(DEMO_DIR)/dataset.sh snapshot $(DEMO_NS) $(DEMO_RELEASE)

demo-restore:
	$(DEMO_DIR)/dataset.sh restore $(DEMO_NS) $(DEMO_RELEASE)

demo-notebook:
	EGERNIA_BASE_URL=$(or $(BASE_URL),http://$(DEMO_HOST):8080) \
	  EGERNIA_INSECURE_TLS=$(or $(INSECURE_TLS),0) \
	  $(PYTHON) -m marimo edit $(DEMO_MARIMO)

demo-status:
	@$(DEMO_DIR)/status.sh $(DEMO_NS) $(DEMO_RELEASE) $(DEMO_HOST)

demo-teardown:
	@echo "removing release '$(DEMO_RELEASE)' from context '$(DEMO_CONTEXT)'"
	-helm uninstall $(DEMO_RELEASE) --namespace $(DEMO_NS)
	@if [ -n "$(KEEP_DATA)" ]; then \
	  echo "keeping the PostgreSQL PVC (KEEP_DATA set); 'make demo-deploy' will reuse the dataset"; \
	else \
	  echo "deleting the namespace, dataset included"; \
	  kubectl delete namespace $(DEMO_NS) --ignore-not-found; \
	fi
