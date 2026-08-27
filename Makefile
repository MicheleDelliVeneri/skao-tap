SHELL := /bin/bash
PYTHON ?= $(CURDIR)/.venv/bin/python

# ---------------------------------------------------------------------------
# Notebooks
#
#   make notebook                         marimo scaling notebook, against any
#                                         deployment (BASE_URL=..., INSECURE_TLS=1)
#
# The deployment itself is no longer driven from here: egernia is deployed as
# a service of ska-src-api-deployment-stack, whose overlays own the values,
# the ingress and the secrets.

MARIMO := $(CURDIR)/demo/scaling_demo.py

.PHONY: notebook

notebook:
	EGERNIA_BASE_URL=$(or $(BASE_URL),http://localhost:8080) \
	  EGERNIA_INSECURE_TLS=$(or $(INSECURE_TLS),0) \
	  EGERNIA_PROMETHEUS_URL=$(or $(PROMETHEUS_URL),) \
	  $(PYTHON) -m marimo edit $(MARIMO)
