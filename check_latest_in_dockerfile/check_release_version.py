import argparse
import os
import csv
import re  # <--- Added re module
from github import Github, Auth
from collections import Counter, defaultdict
from datetime import datetime

# --- CONFIGURATION ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") 

def is_release_tag(tag_name):
    """
    Accept tags like v1.2.3, 1.0.0, v2.5, 3.4, v1.0.0-1, release-2.0 etc.
    Reject non-release tags (nightly, dev, rc*, beta*, etc)
    """
    unstable_keywords = ['beta', 'alpha', 'rc', 'dev', 'develop', 'nightly', 'snapshot', 'pre']
    if any(keyword in tag_name.lower() for keyword in unstable_keywords):
        return False
    # Regex checks for at least one digit sequence followed by separators
    return bool(re.match(r"^[a-zA-Z\-_]*\d+([\.\-]\d+)+.*$", tag_name))

def get_repo_stats(repo_name, g):
    try:
        clean_name = repo_name.replace("https://github.com/", "").strip("/")
        repo = g.get_repo(clean_name)

        # 1. Get Latest Commit Year
        try:
            branch = repo.default_branch
            latest_commit = repo.get_branch(branch).commit
            commit_date = latest_commit.commit.author.date
            latest_year = commit_date.year
        except Exception:
            latest_year = "Unknown"

        # 2. Check for Tags and Filter them
        try:
            tags = repo.get_tags()
            valid_release_count = 0
            
            # Iterate through tags and filter using your custom function
            # We iterate manually because .totalCount includes ALL tags (including nightly/beta)
            for tag in tags:
                if is_release_tag(tag.name):
                    valid_release_count += 1
            
            has_release = valid_release_count > 0
        except Exception:
            valid_release_count = 0
            has_release = False

        return {
            "repo": clean_name,
            "year": latest_year,
            "has_release": has_release,
            "release_count": valid_release_count
        }

    except Exception as e:
        print(f"  [!] Error accessing repo {repo_name}: {e}")
        return None

def main(input_file, token=None):
    # Authenticate
    if token:
        auth = Auth.Token(token)
        g = Github(auth=auth)
    else:
        print("Warning: No GitHub token provided. Rate limits will be strict.")
        g = Github()

    yearly_stats = defaultdict(lambda: {'total': 0, 'with_ver': 0, 'no_ver': 0})
    
    results = []

    if not os.path.exists(input_file):
        print(f"Error: Input file '{input_file}' not found.")
        return

    with open(input_file, 'r') as f:
        repo_list = [line.strip() for line in f if line.strip()]

    total_repos = len(repo_list)
    print(f"Found {total_repos} repositories. Starting scan...")
    print("-" * 60)

    for i, repo_name in enumerate(repo_list):
        print(f"[{i+1}/{total_repos}] Scanning {repo_name}...", end="", flush=True)
        
        data = get_repo_stats(repo_name, g)
        
        if data:
            results.append(data)
            year = data["year"]
            
            yearly_stats[year]['total'] += 1
            if data['has_release']:
                yearly_stats[year]['with_ver'] += 1
                print(f" Done ({year} | Valid Tags: {data['release_count']})")
            else:
                yearly_stats[year]['no_ver'] += 1
                print(f" Done ({year} | No Valid Tags)")
        else:
            print(" Failed")

    # --- PRINT YEARLY STATS ---
    print("\n" + "="*40)
    print("STATISTICS BY YEAR")
    print("="*40)

    sorted_years = sorted(yearly_stats.keys(), key=lambda x: (str(x) if x != "Unknown" else "0"), reverse=True)

    for year in sorted_years:
        stats = yearly_stats[year]
        print(f"{year}")
        print(f"  Total: {stats['total']}")
        print(f"  With multiple version: {stats['with_ver']}")
        print(f"  Without: {stats['no_ver']}")
        print("-" * 20)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--token", default=GITHUB_TOKEN)
    args = parser.parse_args()
    
    main(args.input, args.token)