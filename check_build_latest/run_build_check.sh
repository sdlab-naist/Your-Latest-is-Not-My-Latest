#!/bin/bash

if [ -z "$1" ]; then
    echo "Usage: bash run_build_check.sh <year>"
    echo "Example: bash run_build_check.sh 2026"
    exit 1
fi

YEAR="$1"
ROOT_DIR="LOCAL_PATH"

source "$ROOT_DIR/.venv/bin/activate"

ulimit -n 65536

pip install -r "$ROOT_DIR/requirements.txt" -q

python "$ROOT_DIR/check_build_latest/check_build_latest.py" \
    --docker_user "DOCKERUSER" \
    --docker_pass "DOCKERPASS" \
    --input_dir "$ROOT_DIR/output/latest_check" \
    --output_dir "$ROOT_DIR/output/build_check" \
    --year "$YEAR"
