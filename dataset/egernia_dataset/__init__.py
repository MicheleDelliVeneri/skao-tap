"""Deterministic, resumable generation of ODP and software metadata.

Grows a deployed service's database to a requested number of rows, so there is
something to query. Used by the post-deploy seeding job; previously the dataset
subpackage of the benchmark suite, and unchanged apart from its name and where
it reads its config from.

Deterministic and idempotent by construction: every row is a pure function of
(seed, index), every statement is ON CONFLICT DO NOTHING, and generation
resumes from the first incomplete project, so a run that is killed continues
rather than duplicating.
"""
