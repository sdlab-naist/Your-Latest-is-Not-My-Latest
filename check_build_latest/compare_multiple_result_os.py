import json
import csv
import argparse
import os

OS_NAMES = ["linux", "mac", "windows"]


def normalize_path(path):
    path = path.replace("\\", "/")
    lower = path.lower()
    marker = "/repo/"
    idx = lower.find(marker)
    if idx != -1:
        return path[idx + len(marker):]
    return path


def load_os_data(year, os_name, base="output/build_check"):
    filepath = os.path.join(base, os_name, f"filtered_latest_details_fixed_{year}.json")
    if not os.path.exists(filepath):
        print(f"  [warn] not found: {filepath}")
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def main(year):
    base = "output/build_check"
    out_repo_csv = os.path.join(base, f"compare_repo_{year}.csv")
    out_df_csv = os.path.join(base, f"compare_dockerfile_{year}.csv")

    repo_map = {}
    df_map = {}

    for os_name in OS_NAMES:
        print(f"Loading {os_name} ...")
        records = load_os_data(year, os_name)

        for repo in records:
            repo_name = repo.get("repo_name", "")
            if not repo_name:
                continue

            if repo_name not in repo_map:
                repo_map[repo_name] = {n: 0 for n in OS_NAMES}

            full_log = repo.get("full_log", [])
            if not full_log:
                continue

            latest = full_log[0]

            if latest.get("any_success"):
                repo_map[repo_name][os_name] = 1

            for detail in latest.get("details", []):
                raw_path = detail.get("path", "")
                norm = normalize_path(raw_path)
                if not norm:
                    continue

                if norm not in df_map:
                    df_map[norm] = {n: 0 for n in OS_NAMES}

                if detail.get("status") == "success":
                    df_map[norm][os_name] = 1

    with open(out_repo_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["repo_name", "windows", "linux", "mac", "pass"])
        for repo_name, cols in sorted(repo_map.items()):
            w = cols["windows"]
            l = cols["linux"]
            m = cols["mac"]
            pass_col = 1 if (w or l or m) else 0
            writer.writerow([repo_name, w, l, m, pass_col])

    with open(out_df_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["dockerfile", "windows", "linux", "mac", "pass"])
        for norm_path, cols in sorted(df_map.items()):
            w = cols["windows"]
            l = cols["linux"]
            m = cols["mac"]
            pass_col = 1 if (w or l or m) else 0
            writer.writerow([norm_path, w, l, m, pass_col])

    repo_pass = sum(1 for c in repo_map.values() if c["windows"] or c["linux"] or c["mac"])
    df_pass = sum(1 for c in df_map.values() if c["windows"] or c["linux"] or c["mac"])

    print(f"\nYear: {year}")
    print(f"Repos   : {len(repo_map)} total, {repo_pass} with pass=1")
    print(f"Dockerfiles: {len(df_map)} total, {df_pass} with pass=1")
    print(f"Saved: {out_repo_csv}")
    print(f"Saved: {out_df_csv}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=str, required=True, help="Year to compare (e.g. 2026)")
    args = parser.parse_args()
    main(args.year)
