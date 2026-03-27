import os
from datetime import datetime

def parse_date(date_str):

    try:
        clean_date = date_str.strip().replace(' ', 'T')
        return datetime.fromisoformat(clean_date)
    except ValueError:
        return None

def analyze_tag_changes(file_path):
    repo_data = {}
    
    current_section = None
    current_repo = None

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        for line in lines:
            line = line.strip()
            
            if "Changes pinned to latest:" in line:
                current_section = "pinned_to_latest"
                continue
            elif "Changes latest to pinned:" in line:
                current_section = "latest_to_pinned"
                continue
            elif line.startswith("Process") and "change tag report" in line:
                current_section = None
                current_repo = None
                continue
            
            if line.startswith("Repository:"):
                current_repo = line.split("Repository:")[1].strip()
            
                if current_repo not in repo_data:
                    repo_data[current_repo] = {
                        "pinned_to_latest": [],
                        "latest_to_pinned": []
                    }
            
            if line.startswith("commit_date:") and current_section and current_repo:
                date_str = line.split("commit_date:")[1].strip()
                dt_obj = parse_date(date_str)
                
                if dt_obj:
                    repo_data[current_repo][current_section].append(dt_obj)

        print("="*60)
        print("TAG CHANGE ANALYSIS REPORT (WITH TIMELINE)")
        print("="*60)
        
        pinned_repos = {r for r, d in repo_data.items() if d["pinned_to_latest"]}
        latest_repos = {r for r, d in repo_data.items() if d["latest_to_pinned"]}
        both_repos = pinned_repos.intersection(latest_repos)

        print(f"Total Repos changing Pinned -> Latest: {len(pinned_repos)}")
        print(f"Total Repos changing Latest -> Pinned: {len(latest_repos)}")
        print("-" * 60)
        print(f"Repos with BOTH changes: {len(both_repos)}")
        
        if both_repos:
            print("\nAnalysis of Repos with BOTH changes:")
            print(f"{'Repository':<40} | {'Latest Action':<20} | {'Date'}")
            print("-" * 80)
            
            for repo in sorted(both_repos):
                dates_p2l = repo_data[repo]["pinned_to_latest"]
                dates_l2p = repo_data[repo]["latest_to_pinned"]
                
                # Get the most recent date for each category
                last_p2l = max(dates_p2l) if dates_p2l else datetime.min
                last_l2p = max(dates_l2p) if dates_l2p else datetime.min
                
                if last_p2l > last_l2p:
                    verdict = "Pinned -> Latest"
                    last_date = last_p2l
                else:
                    verdict = "Latest -> Pinned"
                    last_date = last_l2p
                
                print(f"{repo:<40} | {verdict:<20} | {last_date}")
        else:
            print("(None found)")
            
        print("="*60)

    except FileNotFoundError:
        print(f"Error: The file '{file_path}' was not found.")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    input_filename = "./output/change_tag_report.txt" 
    analyze_tag_changes(input_filename)