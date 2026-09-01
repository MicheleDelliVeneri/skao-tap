"""STC-S footprints to pgsphere geometries.

SRCNet metadata carries observation footprints (``s_region``) as an STC-S
subset in the ICRS frame with coordinates in degrees — ``CIRCLE``,
``POLYGON`` or ``POSITION`` — validated at the producer by the data-model
package. Text is not queryable: for ADQL ``INTERSECTS``/``CONTAINS`` to
work over the ingested metadata, the string is parsed at ingestion into a
companion pgsphere column (see :mod:`egernia_core.metadata.schema_gen`), and this
module is that parse.

Everything becomes one type, ``spoly``, because a column has one type:

- a POLYGON's vertices are used as they are (pgsphere accepts either
  winding and encloses the smaller area, so producer winding is not
  normalised);
- a CIRCLE becomes a regular polygon inscribed in it — 32 vertices keeps
  the area error under 0.7% and any point of the circle within 0.2% of the
  radius, far below the positional uncertainty of a real footprint;
- a POSITION becomes a tiny polygon (a tenth of an arcsecond across, see
  ``POSITION_RADIUS_DEG``), so point-in-region and region-overlap queries
  treat it as the point it is.

Coordinates are emitted fixed-point rather than through ``repr``: the
vertices of a POSITION near RA=0 land below 1e-4 degrees, where Python's
float repr switches to exponent notation, and nothing here should depend
on pgsphere parsing ``9.8e-05d``.

The vertex-around-a-centre math is the standard spherical destination
formula, so a circle near a pole is a ring around the pole rather than a
distorted ellipse in RA/Dec space.
"""

import math

# 32 vertices: inscribed-polygon area deficit is (1 - sinc(2*pi/N)) ~ 0.64%,
# and the worst-case boundary error is r * (1 - cos(pi/N)) ~ 0.48% of r.
CIRCLE_VERTICES = 32

# A POSITION has no extent; give it just enough that pgsphere still sees
# eight distinct vertices. pgsphere compares coordinates with EPSILON =
# 1e-9 rad, and the chord between adjacent vertices of the 8-gon is
# 2*r*sin(pi/8) = 0.765*r — a radius too close to that epsilon lets pgsphere
# legitimately reject the spoly as degenerate. Fifty milliarcseconds gives
# r = 2.42e-7 rad and a 1.86e-7 rad chord, ~186x the epsilon. The margin is
# deliberately spent on precision rather than on safety headroom: a point
# 0.1 arcsecond across stays inside SKA astrometry, where a full arcsecond —
# a synthesised beam — would not, and 186x is already far outside the range
# where a coordinate comparison is in doubt.
POSITION_RADIUS_DEG = 0.05 / 3600

_SHAPES = ("CIRCLE", "POLYGON", "POSITION")


def _points_around(ra_deg: float, dec_deg: float, radius_deg: float, n: int) -> list[tuple]:
    """``n`` points at angular distance ``radius_deg`` around a centre."""
    ra0 = math.radians(ra_deg)
    dec0 = math.radians(dec_deg)
    r = math.radians(radius_deg)
    points = []
    for k in range(n):
        bearing = 2.0 * math.pi * k / n
        dec = math.asin(
            math.sin(dec0) * math.cos(r) + math.cos(dec0) * math.sin(r) * math.cos(bearing)
        )
        ra = ra0 + math.atan2(
            math.sin(bearing) * math.sin(r) * math.cos(dec0),
            math.cos(r) - math.sin(dec0) * math.sin(dec),
        )
        points.append((math.degrees(ra) % 360.0, math.degrees(dec)))
    return points


def _literal(points: list[tuple]) -> str:
    # fixed point, never repr(): float repr switches to exponent notation
    # below 1e-4, and a POSITION at RA=0 has vertices at 9.8e-06 degrees and
    # a numerically-zero one at 1.7e-21, so repr() would hand pgsphere
    # "(1.700898332149135e-21d,...)". 12 decimal places of degree is
    # 3.6e-9 arcsec (1.7e-14 rad) — five orders below the epsilon that keeps
    # two vertices apart, so the rounding costs nothing.
    return "{" + ",".join(f"({ra:.12f}d,{dec:.12f}d)" for ra, dec in points) + "}"


def stcs_to_spoly(region: str | None) -> str | None:
    """The pgsphere ``spoly`` literal for an STC-S region string.

    ``None`` stays ``None`` (an absent footprint has no geometry). Raises
    ``ValueError`` for anything outside the supported subset — the same
    subset the data model validates at the producer, revalidated here
    because an amendment can carry a region the model never saw.
    """
    if region is None:
        return None
    tokens = region.split()
    if not tokens or tokens[0].upper() not in _SHAPES:
        raise ValueError(f"unsupported region shape in {region!r}: expected one of {_SHAPES}")
    shape = tokens[0].upper()
    args = tokens[1:]
    if args and args[0].upper() == "ICRS":
        args = args[1:]
    try:
        values = [float(token) for token in args]
    except ValueError:
        raise ValueError(
            f"invalid s_region {region!r}: coordinates must be decimal degrees"
        ) from None

    if shape == "CIRCLE" and len(values) != 3:
        raise ValueError(f"invalid s_region {region!r}: CIRCLE requires <ra> <dec> <radius>")
    if shape == "POSITION" and len(values) != 2:
        raise ValueError(f"invalid s_region {region!r}: POSITION requires <ra> <dec>")
    if shape == "POLYGON" and (len(values) < 6 or len(values) % 2):
        raise ValueError(f"invalid s_region {region!r}: POLYGON requires >= 3 <ra> <dec> pairs")

    pair_count = 1 if shape in ("CIRCLE", "POSITION") else len(values) // 2
    for i in range(0, 2 * pair_count, 2):
        ra, dec = values[i], values[i + 1]
        if not 0.0 <= ra <= 360.0:
            raise ValueError(f"invalid s_region {region!r}: RA {ra} outside [0, 360]")
        if not -90.0 <= dec <= 90.0:
            raise ValueError(f"invalid s_region {region!r}: Dec {dec} outside [-90, 90]")

    if shape == "CIRCLE":
        radius = values[2]
        if not 0.0 < radius <= 180.0:
            raise ValueError(f"invalid s_region {region!r}: radius {radius} outside (0, 180]")
        return _literal(_points_around(values[0], values[1], radius, CIRCLE_VERTICES))
    if shape == "POSITION":
        return _literal(_points_around(values[0], values[1], POSITION_RADIUS_DEG, 8))
    return _literal(list(zip(values[0::2], values[1::2], strict=True)))
