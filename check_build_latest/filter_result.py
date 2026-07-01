import json
import argparse
import re
import os


def iter_repo_records(year, base="output/build_check"):
    """Yield each repo record one at a time.

    Reads the per-repo folder repo_build_details_{year}_items/ if it exists
    (memory-light: one small file at a time, so it works even on huge datasets);
    otherwise falls back to the legacy single repo_build_details_{year}.json array.
    """
    items_dir = os.path.join(base, f"repo_build_details_{year}_items")
    if os.path.isdir(items_dir):
        for name in sorted(os.listdir(items_dir)):
            if name.endswith(".json") and not name.startswith("_"):
                with open(os.path.join(items_dir, name), "r", encoding="utf-8") as f:
                    yield json.load(f)
        return
    legacy = os.path.join(base, f"repo_build_details_{year}.json")
    if os.path.exists(legacy):
        with open(legacy, "r", encoding="utf-8") as f:
            for rec in json.load(f):
                yield rec

def _find_keyword(keywords, log_lower):
    """Return the first keyword from the list that appears in the log, else None."""
    for kw in keywords:
        if kw in log_lower:
            return kw
    return None

def _kw_rx(keywords):
    return re.compile("|".join(re.escape(k) for k in keywords))

_kw_arch = _kw_rx(["$targetarch", "$archtag", "$targetplatform",
                   "does not match system", "operating system is not supported",
                   "segmentation fault", "${targetarch}", "${targetplatform}"])
_kw_buildkit     = _kw_rx(["requires buildkit", "buildkit enabled"])
_kw_license     = _kw_rx(["this system is not registered",
                           "lease does not exist: not found", "not licensed"])
_kw_cloner       = _kw_rx(["fail commit"])
_kw_context      = _kw_rx(["file not found in build context", "no such user",
                           "file does not exist", "no source files",
                           "no such file or directory", "required file not found",
                           "failed to calculate checksum of ref"])
_kw_secrets      = _kw_rx(["secret", "credentials", "unauthorized", "forbidden",
                           "unauthenticated", "permission denied"])
_kw_base         = _kw_rx(["should not be blank", "invalid reference format",
                           "golang:${go_version} as builder"])
_kw_network      = _kw_rx(["network error", "no such host", "connection refused",
                           "network is unreachable", "timed out",
                           "connection closed prematurely", "connection reset by peer"])
_kw_invalid_arg  = _kw_rx(["invalid argument", "bad parameter",
                           "unknown flag", "argument is required"])
_kw_syntax       = _kw_rx(["syntax error",
                           "When using COPY with more than one source file, "
                           "the destination must be a directory and end with a /"])

# Step headers in classic `docker build` output: "Step 2/8 : RUN ..."
_kw_step = re.compile(r"step\s+\d+/\d+\s*:")


def should_filter_out(error_category, full_log_text, count_filter_types={}):
    """
    Returns True if the error matches the conditions to be removed from the JSON.
    """
    if not full_log_text:
        full_log_text = ""
    log_lower = re.sub(r"\s+", " ", full_log_text.lower())

    # ----- KEPT AS-IS (run on the whole log) -----
    if error_category == "No Dockerfile found" or "is your Dockerfile empty" in log_lower:
        count_filter_types["No Dockerfile found"] = count_filter_types.get("No Dockerfile found", 0) + 1
        return True, count_filter_types

    from_match = re.search(r"(?:step \d+/\d+ : )?from\s+(?:--platform=\S+\s+)?(\S+)", log_lower)
    if from_match:
        base_image = from_match.group(1)
        is_pinned = (":" in base_image and ":latest" not in base_image) or ("@sha256" in base_image)
        if is_pinned:
            count_filter_types["Pinned Version (Not Latest)"] = count_filter_types.get("Pinned Version (Not Latest)", 0) + 1
            return True, count_filter_types

    local_kw = _find_keyword(["from base", "from image"], log_lower)
    if local_kw:
        count_filter_types["Local Base Image"] = count_filter_types.get("Local Base Image", 0) + 1
        return True, count_filter_types

    if "repository does not exist or may require" in log_lower:
        return False, count_filter_types

    # ----- LAST-STEP REGION (regex finds steps; keyword match runs only here) -----
    # Docker prints steps in order and stops at the failing one, so the last
    # "Step N/M :" header marks where the crash happened. Slice from there to the
    # end so keyword detection looks at the failing step, not the whole log.
    step_matches = list(_kw_step.finditer(log_lower))
    last_step = log_lower[step_matches[-1].start():] if step_matches else log_lower

    if error_category is not None:
        # 1. Architecture Mismatch
        if error_category == "Architecture Mismatch Error" or _kw_arch.search(last_step):
            count_filter_types["Architecture Mismatch Error"] = count_filter_types.get("Architecture Mismatch Error", 0) + 1
            return True, count_filter_types

        # 2. BuildKit errors
        if _kw_buildkit.search(last_step):
            count_filter_types["BuildKit errors"] = count_filter_types.get("BuildKit errors", 0) + 1
            return True, count_filter_types

        # License issues
        if _kw_license.search(last_step):
            count_filter_types["License Issues"] = count_filter_types.get("License Issues", 0) + 1
            return True, count_filter_types

        # Cloner issues
        if _kw_cloner.search(last_step):
            count_filter_types["Cloner Issues"] = count_filter_types.get("Cloner Issues", 0) + 1
            return True, count_filter_types

        # Missing file in context
        if _kw_context.search(last_step):
            count_filter_types["Context/Missing File Error"] = count_filter_types.get("Context/Missing File Error", 0) + 1
            return True, count_filter_types

        # 4. Secrets / Missing Keys
        if _kw_secrets.search(last_step):
            count_filter_types["Secrets / Missing Keys"] = count_filter_types.get("Secrets / Missing Keys", 0) + 1
            return True, count_filter_types

        # 5. User must specify base image (e.g., ${BASE_IMAGE})
        if _kw_base.search(last_step): # or (error_category == "Other" or "Other" in error_category) and "$" in last_step:
            count_filter_types["User must specify base image"] = count_filter_types.get("User must specify base image", 0) + 1
            return True, count_filter_types

        # 6. No Dockerfile found
        if error_category == "No Dockerfile found":
            count_filter_types["No Dockerfile found"] = count_filter_types.get("No Dockerfile found", 0) + 1
            return True, count_filter_types

        # 8. Network and Connection Issues
        if _kw_network.search(last_step):
            count_filter_types["Network and Connection Issues"] = count_filter_types.get("Network and Connection Issues", 0) + 1
            return True, count_filter_types

        # 9. Invalid Argument Error
        if error_category == "Invalid Argument Error" or _kw_invalid_arg.search(last_step):
            count_filter_types["Invalid Argument Error"] = count_filter_types.get("Invalid Argument Error", 0) + 1
            return True, count_filter_types

        # Syntax Error
        if error_category == "Syntax Error" or _kw_syntax.search(last_step):
            count_filter_types["Syntax Error"] = count_filter_types.get("Syntax Error", 0) + 1
            return True, count_filter_types

    return False, count_filter_types

def main(year, tag_only=False, os=None):
    if tag_only:
        output_json = f"output/build_check/{os}/filtered_latest_details_{year}.json"
    else:
        output_json = f"output/build_check/{os}/filtered_latest_details_fixed_{year}.json"
    stat_output = f"output/build_check/{os}/build_stats_summary_{year}.txt"

    category_counts = {}

    filtered_data = []
    removed_dockerfiles_count = 0
    tagged_yes_count = 0
    original_count = 0

    def filter_detail_list(details):
        """Tag (tag_only) or drop (filter mode) each Dockerfile detail; return the kept list.

        Shared by every section that carries Dockerfile details so they are all filtered
        the same way.
        """
        nonlocal removed_dockerfiles_count, tagged_yes_count
        kept = []
        for df in details:
            error_cat = df.get("error", "Unknown Error")
            log_text = df.get("full_log", "")
            should_filter, _ = should_filter_out(error_cat, log_text, category_counts)
            if tag_only:
                df["should_filter"] = "Yes" if should_filter else "No"
                if should_filter:
                    tagged_yes_count += 1
                kept.append(df)
            else:
                if should_filter:
                    removed_dockerfiles_count += 1
                    continue
                kept.append(df)
        return kept

    for repo in iter_repo_records(year, base=f"output/build_check/{os}"):
        original_count += 1
        if not repo.get("full_log"):
            continue

        filtered_full_log = []
        has_any_dockerfiles_left = False

        for attempt in repo["full_log"]:
            filtered_details = filter_detail_list(attempt.get("details", []))

            if filtered_details:
                attempt["details"] = filtered_details
                filtered_full_log.append(attempt)
                has_any_dockerfiles_left = True

        if has_any_dockerfiles_left or tag_only:
            repo["full_log"] = filtered_full_log

            # breaking_point carries its own Dockerfile details and was previously passed
            # through unfiltered; filter/tag it with the same rules.
            bp = repo.get("breaking_point")
            if isinstance(bp, dict) and isinstance(bp.get("details"), list):
                bp["details"] = filter_detail_list(bp["details"])

            latest_details = repo["full_log"][0].get("details", []) if repo["full_log"] else []
            repo["latest_success"] = any(d.get("status") == "success" for d in latest_details)

            filtered_data.append(repo)

    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(filtered_data, f, indent=4)

    print_stats_lines = []
    print_stats_lines.append("="*50)
    mode_label = "TAG-ONLY" if tag_only else "FILTER"
    print_stats_lines.append(f"FILTERING SUMMARY [{mode_label} MODE] (Year: {year})")
    print_stats_lines.append("="*50)
    print_stats_lines.append(f"Original Repositories : {original_count}")
    if tag_only:
        print_stats_lines.append(f"Output Repositories   : {len(filtered_data)} (all kept, should_filter field added)")
        print_stats_lines.append(f"Dockerfiles tagged should_filter=Yes: {tagged_yes_count}")
    else:
        print_stats_lines.append(f"Filtered Repositories : {len(filtered_data)} (Dropped {original_count - len(filtered_data)} completely empty repos)")
        print_stats_lines.append(f"Specific Failed Dockerfiles Removed: {removed_dockerfiles_count}")
    print_stats_lines.append("\nDockerfiles matching filter conditions (by category):")
    for category, count in category_counts.items():
        print_stats_lines.append(f"  {category}: {count}")
    print_stats_lines.append("\n")
    print_stats_text = "\n".join(print_stats_lines)
    print(print_stats_text)
    print(f"Saved to: {output_json}")

    with open(stat_output, 'w', encoding='utf-8') as f:
        f.write(print_stats_text)
        print(f"Filtering summary also saved to: {stat_output}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=str, help="Year for which to filter results")
    parser.add_argument("--tag-only", action="store_true",
                        help="Instead of removing records, add should_filter=Yes/No field to each detail entry")
    parser.add_argument("--os", type=str, default=None, 
                        help="(mac, linux, windows) Only count errors for this OS (if specified)", 
                        choices=["mac", "linux", "windows"], 
                        required=True)
    args = parser.parse_args()
    main(args.year, tag_only=args.tag_only, os=args.os)
