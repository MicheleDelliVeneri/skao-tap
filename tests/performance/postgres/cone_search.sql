\set lon random(0, 359)
\set lat random(-80, 80)
\set radius_mdeg random(50, 1000)
SELECT count(*)
FROM perf.sources
WHERE spoint(radians(ra), radians(dec)) <@ scircle(
    spoint(radians(:lon::double precision), radians(:lat::double precision)),
    radians(:radius_mdeg::double precision / 1000.0)
);
