#!/usr/bin/env bash
# Export the canonical benchmark corpus: egernia's ivoa.obscore view — the 30
# mandatory ObsCore 1.1 columns, every server's common denominator — as one
# deterministic CSV plus its sha256 and row count. The *view output* is the
# corpus, not the srcnet.* base tables: the other TAP servers ingest a flat
# ObsCore table. Ordered by obs_publisher_did so the same database always
# produces byte-identical output.
#
# Usage: TAP_DATABASE_URL=postgresql://tap:tap@localhost:5432/tap \
#          scripts/export_obscore_snapshot.sh [output-dir]
set -euo pipefail

url="${TAP_DATABASE_URL:?set TAP_DATABASE_URL to the seeded egernia database}"
out_dir="${1:-benchmarks/tap-compare/corpus}"
mkdir -p "$out_dir"
csv="$out_dir/obscore.csv"

COLUMNS="dataproduct_type, calib_level, obs_collection, obs_id,
obs_publisher_did, access_url, access_format, access_estsize, target_name,
s_ra, s_dec, s_fov, s_region, s_resolution, s_xel1, s_xel2, t_min, t_max,
t_exptime, t_resolution, t_xel, em_min, em_max, em_res_power, em_xel, o_ucd,
pol_states, pol_xel, facility_name, instrument_name"

# TO STDOUT rather than a file path, so it works when psql itself runs
# somewhere else (e.g. inside the database container).
psql "$url" -v ON_ERROR_STOP=1 \
    -c "\\copy (SELECT ${COLUMNS} FROM ivoa.obscore ORDER BY obs_publisher_did) TO STDOUT WITH (FORMAT csv, HEADER)" \
    > "$csv"

rows=$(($(wc -l < "$csv") - 1))
sha=$(shasum -a 256 "$csv" | cut -d' ' -f1)
cat > "$out_dir/dataset.json" <<EOF
{
  "source": "egernia ivoa.obscore view",
  "rows": ${rows},
  "sha256": "${sha}",
  "columns": "obscore-1.1 mandatory (30)",
  "exported_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
echo "wrote ${csv}: ${rows} rows, sha256 ${sha}"
