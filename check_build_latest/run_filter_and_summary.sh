#!/bin/bash

YEAR=(2024 2025 2025_1 2026)
OS=(linux windows mac)
for y in "${YEAR[@]}"; do
    for os in "${OS[@]}"; do
        # python check_build_latest/filter_result.py --year "$y" --os "$os"
    done
    # python check_build_latest/check_error_categorize.py --year "$y"
    python check_build_latest/count_stats.py --year "$y"
    # python check_build_latest/error_categorize.py --year "$y"
    # python check_build_latest/pick_filter.py --year "$y" --os "$OS"
    # python check_build_latest/compare_multiple_result_os.py --year "$y"
    # python check_build_latest/merge_filter_result.py --year "$y"
done