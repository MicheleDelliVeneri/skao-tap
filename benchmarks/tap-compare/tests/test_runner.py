"""The workload draw: deterministic and faithful to its weights."""

import typing

import pytest
from tap_compare import corpus
from tap_compare import runner as load_runner


def test_the_mix_is_drawn_deterministically(cfg):
    entries = corpus.build(cfg["scenarios"], cfg["datasets"], projects=3906, portable_only=True)
    mix = {k: float(v) for k, v in cfg["scenarios"]["mix"].items()}
    first = load_runner.Workload(entries, mix, seed=7)
    second = load_runner.Workload(entries, mix, seed=7)
    assert [first.next().query_id for _ in range(500)] == [
        second.next().query_id for _ in range(500)
    ]


def test_the_mix_follows_its_weights(cfg):
    entries = corpus.build(cfg["scenarios"], cfg["datasets"], projects=3906, portable_only=True)
    mix = {k: float(v) for k, v in cfg["scenarios"]["mix"].items()}
    total = sum(mix.values())
    workload = load_runner.Workload(entries, mix, seed=11)
    counts: dict[str, int] = {}
    for _ in range(20_000):
        cls = workload.next().query_class
        counts[cls] = counts.get(cls, 0) + 1
    for cls, weight in mix.items():
        assert abs(counts.get(cls, 0) / 20_000 - weight / total) < 0.02, cls


def test_a_mix_naming_an_absent_class_is_refused(cfg):
    entries = corpus.build(cfg["scenarios"], cfg["datasets"], projects=100, portable_only=True)
    with pytest.raises(ValueError):
        load_runner.Workload(entries, {"Q99": 1.0}, seed=1)


def test_maxrec_is_sent_explicitly():
    """The compared servers have different MAXREC defaults; every request
    must pin it or the comparison silently compares different row counts."""
    import asyncio

    sent = {}

    class FakeResponse:
        status_code = 200
        headers: typing.ClassVar[dict] = {}

        async def aiter_bytes(self):
            yield b"a,b\n"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeClient:
        def stream(self, method, url, data=None):
            sent.update(data)
            return FakeResponse()

    entry = corpus.CorpusEntry(query_id="x", query_class="Q01", adql="SELECT 1")
    recorder = load_runner.Recorder()
    asyncio.run(
        load_runner._issue_sync(FakeClient(), "http://x/tap", entry, 0.0, recorder, "csv", 12345)
    )
    assert sent["MAXREC"] == "12345"
    assert sent["RESPONSEFORMAT"] == "csv"
    assert recorder.samples[0].status == 200
