import os
import glob
import csv
import re
import argparse
from collections import defaultdict

def extract_year_from_filename(filename):
    """Extracts the year specifically from 'merge_build_stats_summary_{year}'."""
    match = re.search(r'merge_build_stats_summary_(\d{4})', filename)
    if match:
        return match.group(1)
    return "Unknown"

def process_stats(input_dir):
    output_csv = "output/build_check/dockerfile_build_stats.csv"

    # data[yyyy_mm][file_year] = {'success': 0, 'failure': 0}
    data = defaultdict(lambda: defaultdict(lambda: {'success': 0, 'failure': 0}))
    
    all_yyyy_mm = set()
    all_file_years = set()
    
    pattern = re.compile(r"^\s*(\d{4}-\d{2})\s*:\s*(\d+)")
    
    files = glob.glob(os.path.join(input_dir, "merge_build_stats_summary_*.txt"))
    
    if not files:
        print(f"[-] No 'merge_build_stats_summary_*.txt' files found in '{input_dir}'")
        return

    for filepath in files:
        filename = os.path.basename(filepath)
        file_year = extract_year_from_filename(filename)
        
        if file_year == "Unknown":
            print(f"[!] Skipping {filename} - couldn't find a valid 4-digit year.")
            continue
            
        all_file_years.add(file_year)
        current_mode = None
        
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                if "SUCCESS BY YEAR/MONTH" in line:
                    current_mode = "success"
                elif "FAILURES BY YEAR/MONTH" in line:
                    current_mode = "failure"
                else:
                    match = pattern.match(line)
                    if match and current_mode:
                        yyyy_mm = match.group(1)
                        count = int(match.group(2))
                        
                        data[yyyy_mm][file_year][current_mode] += count
                        all_yyyy_mm.add(yyyy_mm)

    with open(output_csv, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        
        sorted_file_years = sorted(list(all_file_years))
        sorted_yyyy_mm = sorted(list(all_yyyy_mm), reverse=True) 
        
        header = ['YYYY-MM']
        for fy in sorted_file_years:
            header.append(f"{fy}_Success")
            header.append(f"{fy}_Failure")
        header.append("Overall_Success")
        header.append("Overall_Failure")
        writer.writerow(header)

        for yyyy_mm in sorted_yyyy_mm:
            row = [yyyy_mm]
            overall_success = 0
            overall_failure = 0
            for fy in sorted_file_years:
                success_count = data[yyyy_mm][fy]['success']
                failure_count = data[yyyy_mm][fy]['failure']
                row.append(success_count)
                row.append(failure_count)
                overall_success += success_count
                overall_failure += failure_count
            row.append(overall_success)
            row.append(overall_failure)
            writer.writerow(row)
                
    print("="*50)
    print("CSV CONVERSION COMPLETE")
    print("="*50)
    print(f"Files Processed  : {len(files)}")
    print(f"Unique File Years: {sorted_file_years}")
    print(f"Data saved to    : {output_csv}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_dir", type=str, default="./", help="Directory containing the txt files")
    args = parser.parse_args()
    
    process_stats(args.input_dir)