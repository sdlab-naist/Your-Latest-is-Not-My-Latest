import json
import datetime
import argparse
from collections import defaultdict

def main(year):
    # Adjust paths to match your folder structure
    input_json = f"output/build_check/filtered_latest_details_fixed_{year}.json" 
    output_txt = f"output/build_check/error_details_log_{year}.txt"
    
    categorized_counts = defaultdict(int)
    details_to_write = []

    try:
        with open(input_json, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Could not find {input_json}. Please run the filter script first.")
        return

    for repo in data:
        repo_name = repo.get("repo_name", "Unknown Repo")
        full_log = repo.get("full_log", [])
        
        if not full_log:
            continue
            
        # We look at the latest build attempt
        latest_attempt = full_log[0]
        details = latest_attempt.get("details", [])
        
        for df in details:
            # We only care about failures for this categorization step
            if df.get("status") == "fail":
                error_cat = df.get("error", "Unknown Error")
                log_text = df.get("full_log", "No log available")
                df_path = df.get("path", "Unknown Path")

                categorized_counts[error_cat] += 1
   
                detail_entry = (
                    f"Repository: {repo_name}\n"
                    f"Dockerfile: {df_path}\n"
                    f"Category  : {error_cat}\n"
                    f"--- LOG ---\n{log_text}\n"
                    f"{'='*60}\n"
                )
                details_to_write.append(detail_entry)

    summary_lines = []
    summary_lines.append("="*50)
    summary_lines.append(f"ERROR COUNTS (From Filtered Data - Year: {year})")
    summary_lines.append("="*50)
    
    total_errors = 0
    for cat, count in sorted(categorized_counts.items(), key=lambda x: x[1], reverse=True):
        summary_lines.append(f"{cat}: {count}")
        total_errors += count
        
    summary_lines.append("-" * 50)
    summary_lines.append(f"Total Errors Recorded: {total_errors}")
    summary_lines.append("="*50)
    
    summary_text = "\n".join(summary_lines)

    print(summary_text)

    if details_to_write:
        with open(output_txt, 'a', encoding='utf-8') as f:
            
            f.write(summary_text + "\n\n")
            
            f.write("--- DETAILED LOGS ---\n\n")
            
            for detail in details_to_write:
                f.write(detail)
                
        print(f"\n[+] Successfully appended summary and {len(details_to_write)} error details to '{output_txt}'")
    else:
        print("\n[-] No failing Dockerfiles found in the filtered JSON. Nothing written to file.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=str, required=True, help="Year for which to categorize results")
    args = parser.parse_args()
    main(args.year)