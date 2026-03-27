import json
import argparse
import re


def should_filter_out(error_category, full_log_text, count_filter_types={}):
    """
    Returns True if the error matches the conditions to be removed from the JSON.
    """
    if not full_log_text:
        full_log_text = ""
    log_lower = full_log_text.lower()

    if error_category == "No Dockerfile found" or "is your Dockerfile empty" in log_lower:
            count_filter_types["No Dockerfile found"] = count_filter_types.get("No Dockerfile found", 0) + 1
            return True, count_filter_types
    
    from_match = re.search(r"(?:step \d+/\d+ : )?from\s+(?:--platform=\S+\s+)?(\S+)", log_lower)
    if from_match:
        base_image = from_match.group(1)
        # It is NOT 'latest' if it has a specific tag (e.g., :3.12, :bookworm) 
        # or a SHA digest (@sha256:...)
        is_pinned = (":" in base_image and ":latest" not in base_image) or ("@sha256" in base_image)
        
        if is_pinned:
            count_filter_types["Pinned Version (Not Latest)"] = count_filter_types.get("Pinned Version (Not Latest)", 0) + 1
            return True, count_filter_types

    if error_category is not None:
        # Architecture Mismatch
        if error_category == "Architecture Mismatch Error" or any(kw in log_lower for kw in ["$targetarch", "$archtag", "does not match system", "operating system is not supported", "segmentation fault", f"${{targetarch}}", "unsupported platform"]):
            count_filter_types["Architecture Mismatch Error"] = count_filter_types.get("Architecture Mismatch Error", 0) + 1
            return True, count_filter_types

        # BuildKit errors
        if "requires buildkit" in log_lower or "buildkit enabled" in log_lower:
            count_filter_types["BuildKit errors"] = count_filter_types.get("BuildKit errors", 0) + 1
            return True, count_filter_types
        
        # Licence issues 
        if any(kw in log_lower for kw in ["lease does not exist: not found", "not licensed"]):
            count_filter_types["License Issues"] = count_filter_types.get("License Issues", 0) + 1
            return True, count_filter_types
        
        # cloner issues 
        if any(kw in log_lower for kw in ["pull access denied"]):
            count_filter_types["Cloner Issues"] = count_filter_types.get("Cloner Issues", 0) + 1
            return True, count_filter_types

        # Missing file in context 
        if any(x in log_lower for x in ["file not found in build context"]):
            count_filter_types["Context/Missing File Error"] = count_filter_types.get("Context/Missing File Error", 0) + 1
            return True, count_filter_types

        # Secrets / Missing Keys
        if (any(kw in log_lower for kw in ["secret", "credentials", "unauthorized", "forbidden", "unauthenticated"]) or error_category == "Permission Error"):
            count_filter_types["Secrets / Missing Keys"] = count_filter_types.get("Secrets / Missing Keys", 0) + 1
            return True, count_filter_types

        #  User have to put base image by themselves (e.g., ${BASE_IMAGE})
        if (any(kw in log_lower for kw in ["should not be blank", "invalid reference format"]) or \
            "from $" in log_lower or \
            ("$" in log_lower and (error_category == "Other" or "Other" in error_category or error_category == "Missing Base Image Error"))):
            count_filter_types["User must specify base image"] = count_filter_types.get("User must specify base image", 0) + 1
            return True, count_filter_types
        
        # No Dockerfile found
        if error_category == "No Dockerfile found":
            count_filter_types["No Dockerfile found"] = count_filter_types.get("No Dockerfile found", 0) + 1
            return True, count_filter_types

        # No meaning error (Other: )
        if error_category == "Other: ":
            count_filter_types["No Meaningful Error"] = count_filter_types.get("No Meaningful Error", 0) + 1
            return True, count_filter_types

        # Network and Connection Issues
        if any(kw.lower() in log_lower for kw in ["network error", "no such host", "connection refused", "network is unreachable", "timed out"]) and error_category is not None:
            count_filter_types["Network and Connection Issues"] = count_filter_types.get("Network and Connection Issues", 0) + 1
            return True, count_filter_types
        
        if any(kw.lower() in log_lower for kw in ["add failed: no source files were specified", "copy failed: no source files were specified", "invalid from flag value"]):
            count_filter_types["No source files"] = count_filter_types.get("No source files", 0) + 1
            return True, count_filter_types
        
        # if any(kw.lower() in log_lower for kw in ["failed to export layer: creatediff: mount callback failed"]):
        #     count_filter_types["Failed to export layer"] = count_filter_types.get("Failed to export layer", 0) + 1
        #     return True, count_filter_types
    

    # Keep everything else
    return False, count_filter_types

def main(year):
    input_json = f"output/build_check/repo_build_details_{year}.json"  
    output_json = f"output/build_check/filtered_latest_details_{year}.json" 
    stat_output = f"output/build_check/build_stats_summary_{year}.txt" 

    category_counts = {}

    try:
        with open(input_json, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Could not find {input_json}")
        return

    filtered_data = []
    removed_dockerfiles_count = 0

    for repo in data:
        if not repo.get("full_log"):
            continue

        # reconstruct the full_log array with only the kept Dockerfiles
        filtered_full_log = []
        has_any_dockerfiles_left = False

        for attempt in repo["full_log"]:
            filtered_details = []
            
            for df in attempt.get("details", []):

                error_cat = df.get("error", "Unknown Error")
                log_text = df.get("full_log", "")
                
                should_filter, category_counts = should_filter_out(error_cat, log_text, category_counts)
                
                if should_filter:
                    removed_dockerfiles_count += 1
                    continue # Skip appending this Dockerfile
                
                filtered_details.append(df)
            
            if filtered_details:
                attempt["details"] = filtered_details
                filtered_full_log.append(attempt)
                has_any_dockerfiles_left = True
        
        # If the repository still has at least one valid Dockerfile left, keep the whole repo
        if has_any_dockerfiles_left:
            repo["full_log"] = filtered_full_log
            
            latest_details = repo["full_log"][0].get("details", [])
            repo["latest_success"] = any(d.get("status") == "success" for d in latest_details)
            
            filtered_data.append(repo)

    # Save the filtered data to a new JSON file
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(filtered_data, f, indent=4)

    print_stats_lines = []
    print_stats_lines.append("="*50)
    print_stats_lines.append(f"FILTERING SUMMARY (Year: {year})")
    print_stats_lines.append("="*50)
    print_stats_lines.append(f"Original Repositories : {len(data)}")
    print_stats_lines.append(f"Filtered Repositories : {len(filtered_data)} (Dropped {len(data) - len(filtered_data)} completely empty repos)")
    print_stats_lines.append(f"Specific Failed Dockerfiles Removed: {removed_dockerfiles_count}")
    print_stats_lines.append("\nRemoved Dockerfiles are those that failed due to:")
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
    args = parser.parse_args()
    main(args.year)