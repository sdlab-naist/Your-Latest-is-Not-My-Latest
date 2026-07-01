import docker
import os
import shutil
import argparse
import re
import git
from datetime import datetime
import logging
import json
import select
import subprocess
import sys
import time

client = docker.from_env()

TEMP_DIR = "TEMP_REPOS"  # Temporary directory for cloning repositories
BUILD_MEMORY_LIMIT = None # Max RAM+swap per docker build; set to None to disable

def setup_logging(output_dir, year):
    """Sets up logging to both file and console."""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    log_file = os.path.join(output_dir, f"build_log_{year}.log")
    
    # Configure logging format
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file), # Saves to file
            logging.StreamHandler()        # Prints to screen
        ]
    )
    # Silence noise from 3rd party libs
    logging.getLogger("git").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

def docker_login(docker_user=None, docker_pass=None):
    if docker_user and docker_pass:
        try:
            result = subprocess.run(
                ["docker", "login", "-u", docker_user, "--password-stdin"],
                input=docker_pass,
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                logging.error(f"Docker login failed: {result.stderr.strip()}")
                exit(1)
            logging.info("Docker login successful.")
        except Exception as e:
            logging.error(f"Docker login failed: {e}")
            exit(1)
    else:
        logging.info("No Docker credentials provided, skipping login.")

def docker_prune():
    try:
        logging.info("Cleaning up Docker images and volumes...")
        prune_result = subprocess.run(["docker", "system", "prune", "-a", "-f", "--volumes"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logging.info("Docker images and volumes pruned successfully.")
    except subprocess.CalledProcessError as e:
        logging.warning(f"Docker prune failed: {e}. Continuing without cleanup.")

def check_dockerfile(path):
    # Re-scan the directory as files change after git checkout
    dfile_list = []
    for pathname, dirnames, filenames in os.walk(path):
        for filename in filenames:
            if filename == "Dockerfile" or filename == "dockerfile":
                dfile_list.append(pathname)
    return set(dfile_list)

def get_dockerfile_last_modified(repo_path, df_dir):
    """Returns the last git commit date for the Dockerfile in df_dir."""
    try:
        dockerfile_path = os.path.join(df_dir, "Dockerfile")
        if not os.path.exists(dockerfile_path):
            dockerfile_path = os.path.join(df_dir, "dockerfile")
        
        rel_path = os.path.relpath(dockerfile_path, repo_path)
        
        result = subprocess.run(
            ["git", "-C", repo_path, "log", "-1", "--format=%ci", "--", rel_path],
            capture_output=True, text=True
        )
        date_str = result.stdout.strip()
        return date_str if date_str else "Unknown"
    except Exception as e:
        return f"Error: {e}"

def is_release_tag(tag_name):
    """
    Accept tags like v1.2.3, 1.0.0, v2.5, 3.4, v1.0.0-1, release-2.0 etc.
    Reject non-release tags (nightly, dev, rc*, beta*, etc)
    """
    unstable_keywords = ['beta', 'alpha', 'rc', 'dev', 'develop', 'nightly', 'snapshot', 'pre', 'debug']
    if any(keyword in tag_name.lower() for keyword in unstable_keywords):
        return False
    return bool(re.match(r"^[a-zA-Z\-_/]*\d+([\.\-]\d+)+.*$", tag_name))

def get_release_tags(repo):
    release_tags = []
    non_release_tags = []

    for t in repo.tags:
        if not is_release_tag(t.name):
            non_release_tags.append(t)
            continue

        try:
            obj = t.object  # may be Commit, TagObject, Tree, Blob

            # Dereference annotated tags
            if isinstance(obj, git.TagObject):
                obj = obj.object

            # Only accept commit-backed tags
            if isinstance(obj, git.Commit):
                release_tags.append(t)
            else:
                # tag points to tree/blob → skip safely
                non_release_tags.append(t)

        except Exception:
            non_release_tags.append(t)

    # sort tags by commit time
    release_tags.sort(
        key=lambda t: (
            t.object.object.committed_datetime
            if isinstance(t.object, git.TagObject)
            else t.object.committed_datetime
        )
    )
    logging.info(f"Sorted release tags: {release_tags}")
    return release_tags, non_release_tags

def pick_version_before_cutoff(git_repo, repo_path, cutoff_date):
    """(label, hexsha) for the version to use: the latest RELEASE tag whose commit is
    on/before cutoff_date; else the latest COMMIT on/before cutoff_date; else HEAD."""
    cutoff = datetime.fromisoformat(cutoff_date).date()

    # get_release_tags() is sorted ascending by commit date -> last eligible == newest <= cutoff
    release_tags, _ = get_release_tags(git_repo)
    eligible = [t for t in release_tags if t.commit.committed_datetime.date() <= cutoff]
    if eligible:
        tag = eligible[-1]
        return f"latest-release-before-{cutoff_date}-{tag.name}", tag.commit.hexsha

    # Fallback: latest commit before the cutoff (previous behavior)
    result = subprocess.run(
        ["git", "-C", repo_path, "log", f"--until={cutoff_date}", "-1", "--format=%H"],
        capture_output=True, text=True
    )
    hexsha = result.stdout.strip()
    if hexsha:
        return f"latest-before-{cutoff_date}-{hexsha[:7]}", hexsha

    return "HEAD", git_repo.head.commit.hexsha

import re

def get_error_category(error_message, full_log=""):
    msg = str(error_message).lower()
    log_str = str(full_log).lower()

    if any(x in msg for x in ["500 internal server error"]):
        exit(1) # This indicates a problem with the Docker daemon or registry, so we should stop the entire process to avoid false failures.

    if "build stalled (no output for" in msg:
        return "Build Stalled Error"

    if any(x in msg for x in ["exec format error", "manifest for", "wrong architecture", "architecture not supported", "failed to parse platform", "no match for platform"]):
        return "Architecture Mismatch Error"

    if any(x in msg for x in ["unknown instruction", "parse error", "syntax error", "dockerfile parse error", "bad request"]):
        return "Syntax Error"
    
    if any(x in msg for x in ["invalid argument", "bad parameter", "unknown flag"]):
        return "Invalid Argument Error"

    # Base image cannot be pulled (private/nonexistent repo) -> treat as missing base image,
    # not a permission problem. Must run before the access-denied check below since the
    # message contains "access denied".
    if "pull access denied" in msg or "repository does not exist or may require" in msg:
        return "Missing Base Image Error"

    if any(x in msg for x in ["permission denied", "access denied", "eacces"]):
        return "Permission Error"

    if "should not be blank" in msg or ("base name" in msg and "blank" in msg) or "invalid reference format" in msg:
        return "Missing Base Image Error"

    last_step_cmd = None

    # BuildKit: find the step number that errored (e.g. "#9 error:"), then look up
    # its instruction (e.g. "#9 [runtime 4/4] COPY ticker /ticker").
    # This handles multi-stage builds where [stagename N/M] steps may appear out of
    # order in the log and a canceled step could otherwise become the last match.
    error_step = re.search(r"#(\d+)\s+error:", msg, re.IGNORECASE)
    if error_step:
        step_num = error_step.group(1)
        step_cmd = re.search(rf"#{step_num}\s+\[[^\]]*\d+/\d+\]\s+(.*)", msg, re.IGNORECASE)
        if step_cmd:
            last_step_cmd = step_cmd.group(1).strip()
        elif re.search(rf"#{step_num}\s+\[internal\]\s+load metadata for\s+", msg, re.IGNORECASE):
            # BuildKit reports base image failures as [internal] load metadata steps
            return "Missing Base Image Error"

    # BuildKit fallback: last [stagename N/M] step (single-stage or no explicit error line).
    # Pattern covers both "[1/3]" and "[runtime 4/4]" style labels; excludes "[internal]".
    if not last_step_cmd:
        buildkit_steps = re.findall(r"#\d+\s+\[[^\]]*\d+/\d+\]\s+(.*)", msg, re.IGNORECASE)
        if buildkit_steps:
            last_step_cmd = buildkit_steps[-1].strip()

    # Classic Docker fallback: "Step 2/3 : RUN ..."
    if not last_step_cmd:
        classic_steps = re.findall(r"step\s+\d+/\d+\s*:\s*(.*)", msg, re.IGNORECASE)
        if classic_steps:
            last_step_cmd = classic_steps[-1].strip()

    if last_step_cmd:
        if last_step_cmd.startswith("from"):
            if "no longer supported" in msg:
                return "No Longer Supported Error"
            return "Missing Base Image Error"

        elif last_step_cmd.startswith("copy") or last_step_cmd.startswith("add"):
            if "no longer supported" in msg:
                return "No Longer Supported Error"
            return "Context/Missing File Error"

        elif last_step_cmd.startswith("run"):
            dep_keywords = [
                "apt-get", "apt ", "apk ", "yum ", "dnf ",
                "pip ", "pip3 ", "npm ", "yarn ", "go get ",
                "gem install", "composer ", "wget ", "curl "
            ]
            network_errors = [
                "connection refused", "404 not found", "failed to fetch", "timeout", "no such host", "network is unreachable"
            ]
            if "no longer supported" in msg:
                return "No Longer Supported Error"
            elif any(k in last_step_cmd for k in dep_keywords) or any(n in msg for n in network_errors):
                return "Dependency Error"
            else:
                return "Command Execution Error"

    if any(x in msg for x in ["killed", "out of memory", "oom", "memory limit exceeded", "cannot allocate memory"]):
        return "OOM Kill Error"

    return f"Other: {msg}"

def build_commit(repo_path, identifier):
    """
    Builds all Dockerfiles in the current checked-out state.
    identifier: string name of the tag or commit hash (for logging)
    """
    dockerfile_dirs = check_dockerfile(repo_path)

    commit_results = {
        "identifier": identifier,
        "any_success": False,
        "details": []
    }

    if not dockerfile_dirs:
        commit_results["details"].append({
            "path": "root",
            "status": "fail",
            "error": "No Dockerfile found",
            "dockerfile_last_modified": "N/A"
        })
        return False, commit_results

    for df_dir in dockerfile_dirs:
        last_modified = get_dockerfile_last_modified(repo_path, df_dir)
        result_entry = {
            "path": df_dir,
            "status": "fail",
            "error": None,
            "full_log": None,
            "dockerfile_last_modified": last_modified
        }

        dockerfile_path = os.path.join(df_dir, "Dockerfile")
        if not os.path.exists(dockerfile_path):
            dockerfile_path = os.path.join(df_dir, "dockerfile")

        safe_id = str(identifier).replace('/', '-').replace(':', '-').lower()
        folder_name = os.path.basename(df_dir).lower() or "root"
        tag_name = f"research_test_build:{safe_id}-{folder_name}"

        cmd = ["docker", "build", "-f", dockerfile_path, "--rm", "--force-rm", "--no-cache", "-t", tag_name]
        if BUILD_MEMORY_LIMIT:
            cmd += ["--memory", BUILD_MEMORY_LIMIT, "--memory-swap", BUILD_MEMORY_LIMIT]
        cmd += [df_dir]
        env = os.environ.copy()
        env["DOCKER_BUILDKIT"] = "0"  # Classic builder enforces --memory via cgroup v1; BuildKit ignores it

        try:
            logging.info(f"    Building Dockerfile in {df_dir}...")
            time_start = time.time()
            time_limit = 1800 # 30 minutes limit per build to prevent hanging indefinitely
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env
            )

            log_lines = []
            printed_count = 0
            stream_to_tty = sys.stdout.isatty()
            timed_out = False
            stalled = False
            stall_limit = 1800  # 30 minutes of no output -> assume hung (e.g. interactive prompt)
            last_output = time.time()
            while True:
                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                if ready:
                    line = process.stdout.readline()
                    if not line:
                        break
                    line = line.rstrip()
                    log_lines.append(line)
                    last_output = time.time()
                    if stream_to_tty:
                        print(line, flush=True)
                        printed_count += 1
                if time.time() - last_output > stall_limit:
                    process.kill()
                    log_lines.append(f"Build stalled (no output for {stall_limit}s) and was killed.")
                    stalled = True
                    timed_out = True
                    break
                if time.time() - time_start > time_limit:
                    process.kill()
                    log_lines.append("Build timed out and was killed.")
                    timed_out = True
                    break
                if process.poll() is not None and not ready:
                    break

            process.wait()

            if stream_to_tty and printed_count:
                sys.stdout.write(f"\x1b[{printed_count}F\x1b[0J")
                sys.stdout.flush()

            log_output = "\n".join(log_lines)

            if process.returncode == 0:
                subprocess.run(["docker", "rmi", "-f", tag_name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                commit_results["any_success"] = True
                result_entry["status"] = "success"
                result_entry["full_log"] = log_output
            elif stalled:
                result_entry["error"] = "Build Stalled Error"
                result_entry["full_log"] = log_output
                logging.warning(f"    Build stalled (no output for {stall_limit}s) for {df_dir}. Likely an interactive prompt or hung step.")
            elif not timed_out and process.returncode in (-9, 137):
                result_entry["error"] = "OOM Kill Error"
                result_entry["full_log"] = log_output
                logging.warning(f"    OOM Kill detected (exit {process.returncode}) for {df_dir}. Consider raising BUILD_MEMORY_LIMIT or checking host memory.")
            else:
                result_entry["error"] = get_error_category(log_output)
                result_entry["full_log"] = log_output

        except Exception as e:
            result_entry["error"] = get_error_category(str(e))
            result_entry["full_log"] = str(e)

        commit_results["details"].append(result_entry)

    return commit_results["any_success"], commit_results

def process_repository(repo_name):
    dir_name = repo_name.replace('/','_')
    repo_path = os.path.join(TEMP_DIR, dir_name)

    repo_data = {
        "repo_name": repo_name,
        "dockerfile_last_modified": {},
        "latest_success": False,
        "buildable_history": [], 
        "breaking_point": None, 
        "error_summary": [],
        "full_log": []
    }

    if not os.path.exists(repo_path):
        try:
            logging.info(f"Cloning repository: {repo_name}...")
            repo = git.Repo.clone_from('git@github.com:' + repo_name + '.git', repo_path)
            logging.info("Clone completed: {0}".format(repo_name))
        except git.GitCommandError as e:
            logging.error(f"Error cloning repository: {e}")
            repo_data["error_summary"].append("Repo path not found")
            return repo_data
    try:
        git_repo = git.Repo(repo_path)
        try:
            git_repo.git.fetch('--all')
            # Check if HEAD is valid
            _ = git_repo.head.commit
            git_repo.git.reset('--hard', 'HEAD')
            # try:
            #     git_repo.git.checkout('origin/main', force=True)
            # except:
            #     git_repo.git.checkout('origin/master', force=True)
            
            logging.info("  [+] Recovery successful.")
        except Exception as e:
            # If we still can't fix it, we can't analyze this repo
            raise Exception(f"Repository HEAD is broken and could not be recovered: {e}")

        # --- Latest release before cutoff date (falls back to latest commit before cutoff) ---
        CUTOFF_DATE = "2026-03-21"
        label_cutoff, hexsha_cutoff = pick_version_before_cutoff(git_repo, repo_path, CUTOFF_DATE)
        checkpoints = [(label_cutoff, git_repo.commit(hexsha_cutoff))]

        logging.info(f"Scanning {repo_name} (checking: {checkpoints[0][0]})")
        # --- Latest release before cutoff date approach end here ---
        previous_commit_hash = None

        for i, (label, commit_obj) in enumerate(checkpoints):
            # just check the latest release before cutoff (or latest commit / HEAD if none)
            if i != 0:
                break

            docker_prune()
            hexsha = commit_obj.hexsha
            
            if hexsha == previous_commit_hash:
                logging.info(f"  Skipping {label} (same commit as previous)...")
                continue

            logging.info(f"  Checking {label} [{hexsha[:7]}]...")
            
            # Checkout
            git_repo.git.checkout(hexsha, force=True)
            
            # Build
            is_success, build_details = build_commit(repo_path, label)

            dockerfile_dates = {}
            for detail in build_details["details"]:
                path = detail["path"]
                last_modified = detail.get("dockerfile_last_modified", "Unknown")
                dockerfile_dates[path] = last_modified

            repo_data["dockerfile_last_modified"] = dockerfile_dates
            
            if is_success:
                logging.info(" SUCCESS")
                repo_data["buildable_history"].append(label)
                repo_data["full_log"].append(build_details)
                if i == 0: # If the first check (HEAD or latest tag) works
                    repo_data["latest_success"] = True
                
                previous_commit_hash = hexsha # Update tracking
            else:
                logging.info(" FAIL")
                repo_data["breaking_point"] = build_details
                repo_data["full_log"].append(build_details)
                
                # Collect errors
                for detail in build_details["details"]:
                    if detail["status"] == "fail":
                        repo_data["error_summary"].append(detail["error"])
                
                break # STOP scanning on first failure

    except Exception as e:
        logging.info(" FAIL")
        logging.error(f"\nGit/System Error: {e}")
        repo_data["error_summary"].append(f"System Error: {str(e)}")

    docker_prune() # Clean up after each repo to save space

    return repo_data

def output_summary(year, stats, last_repo, output_dir):
    with open(os.path.join(output_dir, f'summary_check_build_dockerfile_{year}.txt'), 'w') as summary:
        summary.write(f"Process year: {year}\n")
        summary.write(f"Projects with LATEST version Buildable: {stats['Projects']['Latest_Success']}\n")
        summary.write(f"Projects with LATEST version Failing: {stats['Projects']['Latest_Fail']}\n")
        
        sorted_errors = sorted(stats["Errors"].items(), key=lambda x: x[1], reverse=True)
        summary.write("\nTop Errors (from failing versions):\n")
        for err_type, count in sorted_errors:
            summary.write(f"{err_type}: {count}\n")
            
        summary.write("="*40 + "\n")
        summary.write(f"Last Processed Repo: {last_repo}\n")
        summary.write("="*40 + "\n")

def append_detailed_history(year, data, output_dir):
    file_path = os.path.join(output_dir, f'history_analysis_{year}.txt')
    
    with open(file_path, 'a') as f:
        f.write(f"Repository: {data['repo_name']}\n")
        f.write(f"Latest Version Buildable: {data['latest_success']}\n")
        f.write(f"Buildable Versions (Newest->Oldest): {data['buildable_history']}\n")
        
        if data['breaking_point']:
            f.write(f"Breaking Version: {data['breaking_point']['identifier']}\n")
            f.write("Failures:\n")
            for det in data['breaking_point']['details']:
                    if det['status'] == 'fail':
                        f.write(f"  - {det['path']}: {det['error']}\n")
        elif data['error_summary']:
             f.write(f"Errors: {data['error_summary']}\n")
        else:
            f.write("Status: All checked versions successful\n")
        
        f.write("-" * 40 + "\n")

def append_json_result(year, data, output_dir):
    """Write this repo's result as its own JSON file in a per-year folder.

    Replaces the old single-growing-array file, which ballooned to 100+ GB and got
    slower to append to as it grew. Each repo is written to
        output_dir/repo_build_details_{year}_items/<repo_name with '/' -> '_'>.json
    via a temp file + atomic rename, so an interrupted write never corrupts data.
    """
    items_dir = os.path.join(output_dir, f'repo_build_details_{year}_items')
    os.makedirs(items_dir, exist_ok=True)
    stem = data['repo_name'].replace('/', '_')
    out_path = os.path.join(items_dir, f'{stem}.json')
    tmp_path = out_path + '.part'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        os.replace(tmp_path, out_path)
    except Exception as e:
        logging.error(f"Failed to write JSON for {data.get('repo_name')}: {e}")

def load_checkpoint(output_dir, year):
    stats = {
        "Projects": {"Latest_Success": 0, "Latest_Fail": 0},
        "Errors": {}
    }
    processed_repos = set()
    
    # 1. Load processed repos from the per-repo folder (or the legacy big JSON as fallback).
    #    Stored as sanitized names (repo_name with '/' -> '_') to match the per-repo
    #    filenames; main() compares candidates the same way.
    items_dir = os.path.join(output_dir, f'repo_build_details_{year}_items')
    legacy_path = os.path.join(output_dir, f'repo_build_details_{year}.json')
    if os.path.isdir(items_dir):
        for name in os.listdir(items_dir):
            if name.endswith('.json') and not name.startswith('_'):
                processed_repos.add(name[:-len('.json')])
        logging.info(f"Loaded {len(processed_repos)} processed repositories from {items_dir}.")
    elif os.path.exists(legacy_path):
        logging.info("Found legacy JSON data. Loading processed repositories...")
        try:
            with open(legacy_path, 'r') as f:
                data_list = json.load(f) # Standard load
                for item in data_list:
                    processed_repos.add(item['repo_name'].replace('/', '_'))
            logging.info(f"Loaded {len(processed_repos)} processed repositories.")
        except json.JSONDecodeError:
            logging.warning("JSON file is empty or corrupted. Starting with empty repo list.")
        except Exception as e:
            logging.error(f"Error loading JSON checkpoint: {e}")

    # 2. Load Counters from Summary File
    summary_path = os.path.join(output_dir, f'summary_check_build_dockerfile_{year}.txt')
    if os.path.exists(summary_path):
        logging.info("Found existing summary file. Restoring statistics...")
        try:
            with open(summary_path, 'r') as f:
                lines = f.readlines()
                
            is_error_section = False
            for line in lines:
                line = line.strip()
                if "Projects with LATEST version Buildable:" in line:
                    stats["Projects"]["Latest_Success"] = int(line.split(":")[-1].strip())
                elif "Projects with LATEST version Failing:" in line:
                    stats["Projects"]["Latest_Fail"] = int(line.split(":")[-1].strip())
                elif "Top Errors" in line:
                    is_error_section = True
                    continue
                elif is_error_section and ":" in line:
                    parts = line.split(":")
                    if len(parts) > 2:
                        # Handle case where error type contains ":"
                        err_name = ":".join(parts[:-1]).strip()
                        try:
                            count = int(parts[-1].strip())
                            stats["Errors"][err_name] = count
                        except:
                            pass
                    if len(parts) == 2:
                        err_name = parts[0].strip()
                        try:
                            count = int(parts[1].strip())
                            stats["Errors"][err_name] = count
                        except:
                            pass
        except Exception as e:
            logging.error(f"Failed to parse summary file: {e}. Stats reset.")

    return stats, processed_repos

def main(input_dir, output_dir, year, docker_user=None, docker_pass=None, log_dir=None):
    setup_logging(log_dir, year)
    docker_login(docker_user, docker_pass)

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    logging.info(f"Starting analysis for year {year}")

    filename = os.path.join(input_dir, f"use_latest_repos_multiple_releases_{year}.txt")
    
    if not os.path.exists(filename):
        logging.info(f"Skipping {year} (File not found: {filename})")
        return

    stats, processed_repos = load_checkpoint(output_dir, year)

    with open(filename, 'r') as f:
        repos = [line.strip() for line in f if line.strip()]

    # Filter out already processed repos based on checkpoint
    repos_to_process = [r for r in repos if r.replace('/', '_') not in processed_repos]
    
    total_repos = len(repos)
    remaining_count = len(repos_to_process)
    skipped_count = total_repos - remaining_count

    logging.info(f"\nProcessing Year: {year}")
    logging.info(f"Total Repos: {total_repos}")
    logging.info(f"Already Done: {skipped_count}")
    logging.info(f"Remaining: {remaining_count}")
    logging.info("-" * 40)

    for i, repo_url in enumerate(repos_to_process):
        current_idx = skipped_count + i + 1
        logging.info(f"[{current_idx}/{total_repos}] Analyzing {repo_url}")
        
        result = process_repository(repo_url)
        
        if result["latest_success"]:
            stats["Projects"]["Latest_Success"] += 1
        else:
            stats["Projects"]["Latest_Fail"] += 1

        for err in result["error_summary"]:
            stats["Errors"][err] = stats["Errors"].get(err, 0) + 1
        
        append_detailed_history(year, result, output_dir)
        append_json_result(year, result, output_dir)
        output_summary(year, stats, repo_url, output_dir)
        
        logging.info('-'*40)

    logging.info(f"Completed analysis for year {year}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--docker_user", type=str, default=None)
    parser.add_argument("--docker_pass", type=str, default=None)
    parser.add_argument("--input_dir", type=str, default="./output")
    parser.add_argument("--output_dir", type=str, default="./output/build_check")
    parser.add_argument("--repo_dir", type=str, default=None)
    parser.add_argument("--log_dir", type=str, default="./output/log/build_check")
    parser.add_argument("--year", type=str, default="1970")
    args = parser.parse_args()
    args = vars(args)
    TEMP_DIR = args['repo_dir'] if args['repo_dir'] else TEMP_DIR
    main(args['input_dir'], args['output_dir'], args['year'], args['docker_user'], args['docker_pass'], args['log_dir'])