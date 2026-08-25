"""STC-S to pgsphere conversion (package 7).

The geometry column is only as trustworthy as this parse: a footprint that
converts to the wrong sky answers INTERSECTS with the wrong data products,
silently. The literals are pinned exactly — pgsphere semantics themselves
are exercised by the component suite against a real server.
"""

import math

import pytest
from egernia_core.metadata.regions import (
    CIRCLE_VERTICES,
    POSITION_RADIUS_DEG,
    stcs_to_spoly,
)


def _points(literal: str) -> list[tuple[float, float]]:
    body = literal.strip("{}")
    return [
        tuple(float(part.rstrip("d")) for part in point.strip("()").split(","))
        for point in body.split("),(")
    ]


def test_none_stays_none():
    assert stcs_to_spoly(None) is None


def test_polygon_uses_vertices_verbatim():
    literal = stcs_to_spoly("POLYGON ICRS 9.9 19.9 10.1 19.9 10.0 20.1")
    assert literal == (
        "{(9.900000000000d,19.900000000000d),(10.100000000000d,19.900000000000d),"
        "(10.000000000000d,20.100000000000d)}"
    )
    # and the frame token is optional
    assert stcs_to_spoly("POLYGON 9.9 19.9 10.1 19.9 10.0 20.1") == literal


def test_circle_becomes_a_regular_polygon_around_the_centre():
    points = _points(stcs_to_spoly("CIRCLE ICRS 10.0 -30.0 0.5"))
    assert len(points) == CIRCLE_VERTICES
    for ra, dec in points:
        # every vertex is 0.5 degrees from the centre (spherical distance)
        d = math.degrees(
            math.acos(
                math.sin(math.radians(-30.0)) * math.sin(math.radians(dec))
                + math.cos(math.radians(-30.0))
                * math.cos(math.radians(dec))
                * math.cos(math.radians(ra - 10.0))
            )
        )
        assert d == pytest.approx(0.5, abs=1e-9)


def test_circle_at_the_pole_wraps_instead_of_distorting():
    points = _points(stcs_to_spoly("CIRCLE 0 89.9 0.5"))
    ras = [ra for ra, _ in points]
    # a ring around the pole spans the whole RA range
    assert max(ras) - min(ras) > 180.0
    assert all(0.0 <= ra < 360.0 for ra in ras)


def test_position_becomes_a_tiny_polygon():
    points = _points(stcs_to_spoly("POSITION ICRS 187.5 12.25"))
    assert len(points) == 8
    for _, dec in points:
        assert dec == pytest.approx(12.25, abs=2 * POSITION_RADIUS_DEG)


def test_position_vertices_clear_the_pgsphere_epsilon():
    """The 8-gon's chord has to stay well above pgsphere's 1e-9 rad
    coordinate epsilon, or adjacent vertices compare equal and the spoly is
    rejected as degenerate — the failure the original half-milliarcsecond
    radius was 2x away from."""
    points = _points(stcs_to_spoly("POSITION ICRS 187.5 12.25"))
    chords = [
        math.acos(
            min(
                1.0,
                math.sin(math.radians(d1)) * math.sin(math.radians(d2))
                + math.cos(math.radians(d1))
                * math.cos(math.radians(d2))
                * math.cos(math.radians(r2 - r1)),
            )
        )
        for (r1, d1), (r2, d2) in zip(points, points[1:] + points[:1], strict=True)
    ]
    assert min(chords) > 1e-7  # ~186x the epsilon, at a 50 mas radius


def test_literals_never_use_exponent_notation():
    """A POSITION at RA=0 has vertices at 9.8e-06 degrees and one that is
    numerically 1.7e-21; float repr would render both in exponent notation
    and make the literal depend on pgsphere accepting it."""
    for region in ("POSITION ICRS 0 0", "POSITION 359.9999 -0.00001", "CIRCLE 0 0 0.0001"):
        literal = stcs_to_spoly(region)
        assert "e" not in literal.replace("d", ""), (region, literal)


@pytest.mark.parametrize(
    "bad",
    [
        "CIRCLE 10 20",  # missing radius
        "CIRCLE 10 20 0",  # zero radius
        "CIRCLE 400 20 1",  # RA out of range
        "CIRCLE 10 95 1",  # Dec out of range
        "POSITION 1",  # missing dec
        "POLYGON 1 2 3 4",  # two vertices
        "POLYGON 1 2 3 4 5 6 7",  # odd coordinate count
        "BOX 1 2 3 4",  # unsupported shape
        "CIRCLE ten twenty 1",  # not numbers
        "",  # empty
    ],
)
def test_malformed_regions_are_rejected(bad):
    with pytest.raises(ValueError):
        stcs_to_spoly(bad)
