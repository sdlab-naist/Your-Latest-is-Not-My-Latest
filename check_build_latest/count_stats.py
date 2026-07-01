import json
import argparse
from collections import defaultdict

def main(year):
    # json_file = f"output/build_check/filtered_latest_details_fixed_{year}.json"  # Replace with your actual JSON file name
    json_file = f"output/build_check/merge_all_error_filter/merge_filtered_latest_details_{year}.json"  # Replace with your actual JSON file name
    # output_file = f"output/build_check/build_stats_summary_{year}.txt"  # Output file for the summary (optional)
    output_file = f"output/build_check/merge_all_error_filter/merge_build_stats_summary_{year}.txt"  # Output file for the summary (optional)

    repo_success = 0
    repo_fail = 0
    df_success_total = 0
    df_fail_total = 0
    
    df_success_by_month = defaultdict(int)
    df_fail_by_month = defaultdict(int)

    try:
        with open(json_file, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Could not find {json_file}")
        return

    for repo in data:
        if repo.get("latest_success", False):
            repo_success += 1
        else:
            repo_fail += 1

        full_log = repo.get("full_log", [])
        if not full_log:
            continue
            
        latest_attempt = full_log[0] 
        details = latest_attempt.get("details", [])
        
        for df in details:
            status = df.get("status")
            mod_date_str = df.get("dockerfile_last_modified", "")
            
            year_month = mod_date_str[:7] if mod_date_str else "Unknown"

            if status == "success":
                df_success_total += 1
                df_success_by_month[year_month] += 1
            elif status == "fail":
                df_fail_total += 1
                df_fail_by_month[year_month] += 1

    print("="*50)
    print("REPOSITORY STATISTICS")
    print("="*50)
    print(f"Total Repos Built Successfully : {repo_success}")
    print(f"Total Repos Failed to Build    : {repo_fail}")
    
    print("\n" + "="*50)
    print("DOCKERFILE STATISTICS")
    print("="*50)
    print(f"Total Dockerfiles Built Successfully : {df_success_total}")
    print(f"Total Dockerfiles Failed to Build    : {df_fail_total}")
    
    print("\n" + "="*50)
    print("DOCKERFILE SUCCESS BY YEAR/MONTH")
    print("="*50)
    for ym in sorted(df_success_by_month.keys(), reverse=True):
        print(f"  {ym} : {df_success_by_month[ym]}")

    print("\n" + "="*50)
    print("DOCKERFILE FAILURES BY YEAR/MONTH")
    print("="*50)
    for ym in sorted(df_fail_by_month.keys(), reverse=True):
        print(f"  {ym} : {df_fail_by_month[ym]}")
    
    with open(output_file, 'a') as f:
        f.write("="*50 + "\n")
        f.write("REPOSITORY STATISTICS\n")
        f.write("="*50 + "\n")
        f.write(f"Total Repos Built Successfully : {repo_success}\n")
        f.write(f"Total Repos Failed to Build    : {repo_fail}\n")
        
        f.write("\n" + "="*50 + "\n")
        f.write("DOCKERFILE STATISTICS\n")
        f.write("="*50 + "\n")
        f.write(f"Total Dockerfiles Built Successfully : {df_success_total}\n")
        f.write(f"Total Dockerfiles Failed to Build    : {df_fail_total}\n")
        
        f.write("\n" + "="*50 + "\n")
        f.write("DOCKERFILE SUCCESS BY YEAR/MONTH\n")
        f.write("="*50 + "\n")
        for ym in sorted(df_success_by_month.keys(), reverse=True):
            f.write(f"  {ym} : {df_success_by_month[ym]}\n")

        f.write("\n" + "="*50 + "\n")
        f.write("DOCKERFILE FAILURES BY YEAR/MONTH\n")
        f.write("="*50 + "\n")
        for ym in sorted(df_fail_by_month.keys(), reverse=True):
            f.write(f"  {ym} : {df_fail_by_month[ym]}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=str, help="Year for which to generate statistics")
    args = parser.parse_args()
    main(args.year)