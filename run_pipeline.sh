#!/usr/bin/env bash
# Rebuild everything from raw/ (geocoding upload is a separate, manual step).
set -euo pipefail
cd "$(dirname "$0")"
python3 -m pipeline.clean
python3 -m pipeline.geocode prepare   # writes data/geocode/batches/ for new addresses
python3 -m pipeline.geocode import    # loads any Census results in data/geocode/results/
python3 -m pipeline.export
python3 -m pipeline.build_site
