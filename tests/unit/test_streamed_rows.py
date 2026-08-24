"""StreamedRows: plain streamed statements in place of named cursors.

The contract the call sites lean on: the query is sent at construction so
``cursor.description`` is readable before any row is consumed (empty results
included), rows come through unchanged, and ``close()`` reaches the psycopg
stream generator so an abandoned stream (MAXREC overflow, a dropped client)
deterministically cancels the statement instead of waiting for garbage
collection.
"""

import psycopg
import pytest
from tapcore.db import StreamedRows


class RecordingCursor:
    """stream() with psycopg's timing: nothing happens until first next()."""

    def __init__(self, rows):
        self._rows = rows
        self.description = None
        self.sent = None
        self.size = None
        self.cleaned_up = False

    def stream(self, sql, params=None, *, size=1):
        self.sent = sql
        self.size = size
        try:
            for row in self._rows:
                # psycopg populates description with the first delivered row
                # and, for a zero-row stream, never exposes it at all
                self.description = ["col"]
                yield row
        finally:
            self.cleaned_up = True

    def execute(self, sql, params=None):
        self.sent = sql
        self.description = ["col"]


def test_description_is_readable_before_any_row_is_consumed():
    cur = RecordingCursor([(1,), (2,)])
    rows = StreamedRows(cur, "SELECT 1", chunk_rows=100)
    assert cur.description == ["col"]  # populated by construction alone
    assert list(rows) == [(1,), (2,)]


def test_empty_result_recovers_description_with_a_limit_0_probe():
    # psycopg's stream generator finishes a zero-row query without ever
    # exposing the row description; an empty result still needs its columns
    # (an empty VOTable carries FIELDs), so StreamedRows recovers them.
    cur = RecordingCursor([])
    rows = StreamedRows(cur, "SELECT 1", chunk_rows=100)
    assert cur.description == ["col"]
    assert cur.sent == "SELECT * FROM (SELECT 1) AS empty_result LIMIT 0"
    assert list(rows) == []


def test_close_reaches_the_stream_generator():
    # Abandon the stream after one row, as a MAXREC overflow does: close()
    # must run the generator's cleanup (psycopg's cancel-and-drain) rather
    # than leaving it to the garbage collector.
    cur = RecordingCursor([(1,), (2,), (3,)])
    rows = StreamedRows(cur, "SELECT 1", chunk_rows=100)
    it = iter(rows)
    assert next(it) == (1,)
    assert not cur.cleaned_up
    rows.close()
    assert cur.cleaned_up


def test_close_after_exhaustion_is_harmless():
    cur = RecordingCursor([(1,)])
    rows = StreamedRows(cur, "SELECT 1", chunk_rows=100)
    assert list(rows) == [(1,)]
    assert cur.cleaned_up
    rows.close()


@pytest.mark.parametrize(("chunked", "expected"), [(True, 250), (False, 1)])
def test_chunk_size_follows_libpq_capability(monkeypatch, chunked, expected):
    monkeypatch.setattr(psycopg.capabilities, "has_stream_chunked", lambda: chunked)
    cur = RecordingCursor([(1,)])
    StreamedRows(cur, "SELECT 1", chunk_rows=250)
    assert cur.size == expected
