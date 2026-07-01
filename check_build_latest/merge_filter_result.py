import json
import csv
import argparse
import copy
import os

# Preference order for "other details": windows is the base reference, then
# linux, then mac for any repo/dockerfile that windows does not have.
OS_NAMES = ["windows", "linux", "mac"]

BASE = "output/build_check"


def normalize_path(path):
    """Return the portion of the Dockerfile path after '/repo/' so that paths
    from different OSes (and the compare csv 'dockerfile' column) match."""
    path = path.replace("\\", "/")
    idx = path.lower().find("/repo/")
    if idx != -1:
        return path[idx + len("/repo/"):]
    return path


def load_json(os_name, year):
    path = os.path.join(BASE, os_name, f"filtered_latest_details_fixed_{year}.json")
    if not os.path.exists(path):
        print(f"  [warn] missing {path}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_pass_map(csv_name, key_col, year):
    """Read a compare csv into {key: pass_int}."""
    path = os.path.join(BASE, f"{csv_name}_{year}.csv")
    result = {}
    if not os.path.exists(path):
        print(f"  [warn] missing {path}")
        return result
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            result[row[key_col]] = int(row["pass"])
    return result


def main(year):
    output_json = os.path.join(BASE, f"merge_filtered_latest_details_{year}.json")

    # Reference maps from the compare csvs (pass == 1 -> success)
    repo_pass = load_pass_map("compare_repo", "repo_name", year)
    df_pass = load_pass_map("compare_dockerfile", "dockerfile", year)

    # Per-OS records keyed by repo_name
    os_data = {os_name: {r["repo_name"]: r for r in load_json(os_name, year)} for os_name in OS_NAMES}

    # Build the union of repo_names, windows first, then linux, then mac
    repo_order = []
    seen = set()
    for os_name in OS_NAMES:
        for repo_name in os_data[os_name]:
            if repo_name not in seen:
                seen.add(repo_name)
                repo_order.append(repo_name)

    merged = []
    for repo_name in repo_order:
        # Base record = first available OS in preference order (windows -> linux -> mac)
        base = None
        for os_name in OS_NAMES:
            if repo_name in os_data[os_name]:
                base = copy.deepcopy(os_data[os_name][repo_name])
                break
        if base is None:
            continue

        # Merge the latest-attempt details across all OSes, preferring windows.
        merged_details = {}   # norm_path -> detail (kept from highest-preference OS)
        detail_order = []     # preserve order, windows-first
        for os_name in OS_NAMES:
            rec = os_data[os_name].get(repo_name)
            if not rec or not rec.get("full_log"):
                continue
            for detail in rec["full_log"][0].get("details", []):
                norm = normalize_path(detail.get("path", ""))
                if norm not in merged_details:
                    merged_details[norm] = copy.deepcopy(detail)
                    detail_order.append(norm)

        # Override status from compare_dockerfile (pass == 1 -> success)
        final_details = []
        for norm in detail_order:
            detail = merged_details[norm]
            if norm in df_pass:
                detail["status"] = "success" if df_pass[norm] == 1 else "fail"
            final_details.append(detail)

        # Rebuild the latest attempt on the base record with merged details
        if base.get("full_log"):
            attempt = base["full_log"][0]
        else:
            attempt = {"identifier": "merged", "any_success": False, "details": []}
            base["full_log"] = [attempt]
        attempt["details"] = final_details
        attempt["any_success"] = any(d.get("status") == "success" for d in final_details)

        # Override repo-level success from compare_repo (fallback to attempt result)
        if repo_name in repo_pass:
            base["latest_success"] = repo_pass[repo_name] == 1
        else:
            base["latest_success"] = attempt["any_success"]

        merged.append(base)

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=4)

    success_repos = sum(1 for r in merged if r.get("latest_success"))
    total_details = sum(len(r["full_log"][0]["details"]) for r in merged if r.get("full_log"))
    success_details = sum(
        1 for r in merged if r.get("full_log")
        for d in r["full_log"][0]["details"] if d.get("status") == "success"
    )

    print("=" * 50)
    print(f"MERGE SUMMARY (Year: {year})")
    print("=" * 50)
    print(f"Merged repositories : {len(merged)} ({success_repos} marked success)")
    print(f"Merged dockerfiles  : {total_details} ({success_details} marked success)")
    print(f"Saved to: {output_json}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=str, required=True, help="Year to merge (e.g. 2026)")
    args = parser.parse_args()
    main(args.year)
