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
#   make benchmark-replicas               a bracketed capacity per replica count
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
        benchmark-keda benchmark-result-formats benchmark-stress benchmark-shedding benchmark-replicas benchmark-serialize \
        benchmark-full benchmark-report benchmark-setup \
        benchmark-teardown benchmark-publish benchmark-help

benchmark-help:
	@sed -n '1,21p' $(firstword $(MAKEFILE_LIST))

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
