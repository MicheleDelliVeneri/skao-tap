"""TAP / PostgreSQL / KEDA benchmark suite.

Deterministic, resumable, and append-only: a run never overwrites another
run's results, every measurement records the provenance needed to know whether
it is comparable with any other, and a run that the machine could not honestly
support is marked rather than quietly reported.
"""

__all__ = ["cluster", "corpus", "runs"]
