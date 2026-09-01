"""The corpus must be deterministic, distinct, and aimed at the data."""

from tap_compare import corpus


def test_the_corpus_is_identical_across_builds(cfg):
    """Two runs — and two servers — have to be asked the same questions."""
    first = corpus.build(cfg["scenarios"], cfg["datasets"], projects=3906)
    second = corpus.build(cfg["scenarios"], cfg["datasets"], projects=3906)
    assert corpus.corpus_hash(first) == corpus.corpus_hash(second)
    assert [e.adql for e in first] == [e.adql for e in second]


def test_the_corpus_hash_changes_when_the_corpus_does(cfg):
    """Otherwise the hash in the provenance would not be evidence of anything."""
    base = corpus.build(cfg["scenarios"], cfg["datasets"], projects=3906)
    altered = dict(cfg["scenarios"])
    altered["corpus"] = {**cfg["scenarios"]["corpus"], "seed": 999}
    other = corpus.build(altered, cfg["datasets"], projects=3906)
    assert corpus.corpus_hash(base) != corpus.corpus_hash(other)


def test_combinations_are_distinct(cfg):
    entries = corpus.build(cfg["scenarios"], cfg["datasets"], projects=3906)
    assert len({e.adql for e in entries}) == len(entries)
    assert len(entries) >= cfg["scenarios"]["corpus"]["combinations"] * 0.9


def test_portable_corpus_never_touches_egernia_tables(cfg):
    """The cross-server corpus may reference only what every ObsTAP server
    carries: ivoa.obscore and TAP_SCHEMA."""
    entries = corpus.build(cfg["scenarios"], cfg["datasets"], projects=3906, portable_only=True)
    classes = {e.query_class for e in entries}
    assert classes == set(corpus.PORTABLE)
    assert not any("srcnet." in e.adql for e in entries)


def test_every_selected_class_is_represented(cfg):
    grouped = corpus.by_class(corpus.build(cfg["scenarios"], cfg["datasets"], projects=3906))
    assert set(grouped) == set(corpus.CLASSES)


def test_cone_searches_do_not_repeat_one_coordinate(cfg):
    """A corpus that queries one position measures the page cache."""
    grouped = corpus.by_class(corpus.build(cfg["scenarios"], cfg["datasets"], projects=3906))
    centres = {e.adql.split("CIRCLE")[1][:40] for e in grouped["Q05"]}
    assert len(centres) > 20


def test_the_python_prng_matches_the_generators_contract():
    """corpus.rnd reimplements bench.rnd from dataset/egernia_dataset; if it
    drifts, the corpus stops aiming at the data. The property pinned here is
    the one the SQL relies on: a masked 32-bit slice of the md5, scaled into
    [0, 1)."""
    import hashlib

    digest = hashlib.md5(b"7:42:ra").hexdigest()
    expected = (int(digest[:8], 16) & 0x7FFFFFFF) / 2147483648.0
    assert corpus.rnd("7", 42, "ra") == expected
    assert 0.0 <= corpus.rnd("7", 42, "ra") < 1.0


def test_declination_is_uniform_over_the_sphere():
    """Uniform in degrees would crowd the poles, and then a cone search's
    yield would depend mostly on where the corpus happened to point."""
    decs = [corpus.object_position("1", i)[1] for i in range(1, 20_000)]
    northern = sum(1 for d in decs if d > 0)
    assert 0.45 < northern / len(decs) < 0.55
    # Half the sphere's area lies within +/-30 degrees of the equator.
    equatorial = sum(1 for d in decs if abs(d) <= 30)
    assert 0.45 < equatorial / len(decs) < 0.55
