import csv
import git
import os
import shutil
import glob
import time
import argparse
import re
import fcntl
import requests

GITHUB_TOKEN = "YOUR_GITHUB_TOKEN_HERE"

def extract_from_lines(path):
    """Return a list of FROM lines with base + tag"""
    result = []
    if not os.path.exists(path):
        return result

    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("FROM "):
                result.append(line)
    return result

def parse_base_tag(from_line):
    """
    Extract image:tag from FROM lines.
    Return: (image, tag) example: ("python", "3.9")
    """
    m = re.search(r"FROM\s+([^\s:]+)(?::([^\s@]+))?", from_line)
    if not m:
        return None, None
    image = m.group(1)
    tag = m.group(2) if m.group(2) else None
    return image, tag

def is_release_tag(tag_name):
    """
    Accept tags like v1.2.3, 1.0.0, v2.5, 3.4, v1.0.0-1, release-2.0 etc.
    Reject non-release tags (nightly, dev, rc*, beta*, etc)
    """
    unstable_keywords = ['beta', 'alpha', 'rc', 'dev', 'develop', 'nightly', 'snapshot', 'pre']
    if any(keyword in tag_name.lower() for keyword in unstable_keywords):
        return False
    return bool(re.match(r"^[a-zA-Z\-_]*\d+([\.\-]\d+)+.*$", tag_name))

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

    return release_tags, non_release_tags

def clear_directory(target_dir):
    if not os.path.exists(target_dir):
        #print(f"directory '{target_dir}' does not exist.")
        return

    if not os.path.isdir(target_dir):
        #print(f"specified path  '{target_dir}' have no directory.")
        return

    # Delete all files and subdirectories in a directory
    for item in os.listdir(target_dir):
        item_path = os.path.join(target_dir, item)
        try:
            if os.path.isfile(item_path) or os.path.islink(item_path):
                os.remove(item_path)  # Delete files and symbolic links
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)  # Delete directories recursively
            #print(f"Delete completed: {item_path}")
        except Exception as e:
            print(f"Failed to delete: {item_path}, Error: {e}")


# Clone the repository
def clone_repo(reponame, repo_dir, output_dir):
    dir_name = reponame.replace('/','_')
    try:
        if os.path.exists(repo_dir + '/' + dir_name):
            clear_directory(repo_dir + '/' + dir_name)
            os.rmdir(repo_dir + '/' + dir_name)
        repo = git.Repo.clone_from('git@github.com:' + reponame + '.git', repo_dir + '/' + dir_name)
        print("Clone completed: {0}".format(reponame))
    except git.GitCommandError as e:
        print(f"Error cloning repository: {e}")
        error_text = str(e)
        if "Disk quota exceeded" in error_text:
            with open(os.path.join(output_dir, 'disk_quota_exceeded.csv'), 'a') as f:
                f.write(reponame + '\n')
        repo = ''
    except Exception as e:
        print(e)
        error_text = str(e)
        if "Too many open files" in error_text:
            with open(os.path.join(output_dir, 'retry_repo.csv'), 'a') as f:
                f.write(reponame + '\n')
        print("Delete completed: {0}".format(reponame))
        repo = ''
    return repo, dir_name

def get_headers():
    if "YOUR_GITHUB_TOKEN_HERE" in GITHUB_TOKEN:
        print("WARNING: No GitHub Token provided. API checks will likely fail or hit rate limits.")
        return {}
    return {"Authorization": f"token {GITHUB_TOKEN}"}

def is_repo_meaningful(repo, repo_name):
    """
    Check if repo is meaningful:
    1. Has at least one release tag OR
    2. Stars >= 5 OR
    3. Forks >= 1
    
    Includes retry logic for API Rate Limits (403).
    """
    print(f"Checking if repo {repo_name} is meaningful...")
    
    # 1. Check Tags first (Local check)
    release_tags, _ = get_release_tags(repo)
    if len(release_tags) > 0:
        print(f"  [Meaningful] Found {len(release_tags)} release tags.")
        return True

    # 2. Check Stars/Forks (API check)
    api_url = f"https://api.github.com/repos/{repo_name}"
    max_retries = 2
    
    for attempt in range(max_retries + 2):
        try:
            response = requests.get(api_url, headers=get_headers(), timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                stars = data.get('stargazers_count', 0)
                forks = data.get('forks_count', 0)
                
                if stars >= 5 or forks >= 1:
                    print(f"  [Meaningful] Stars: {stars}, Forks: {forks}")
                    return True
                else:
                    print(f"  [Skip] Not meaningful. Stars: {stars}, Forks: {forks}, Tags: 0")
                    return False
            
            elif response.status_code == 403:

                if attempt > max_retries:
                    print(f"  [Error] 403 Forbidden after {max_retries} retries. skipping.")
                    break 
                
                # Check specific Rate Limit headers
                remaining = int(response.headers.get("X-RateLimit-Remaining", 0))
                reset_time = int(response.headers.get("X-RateLimit-Reset", 0))
                current_time = int(time.time())
                
                # Rate Limit Exhausted (Remaining = 0)
                if remaining == 0 and reset_time > current_time:
                    sleep_seconds = reset_time - current_time + 2 # +2s buffer
                    print(f"  [Rate Limit] Limit exhausted. Sleeping {sleep_seconds}s until reset...")
                    time.sleep(sleep_seconds)
                    continue # Retry immediately after waking up
                
                else:
                    print(f"  [Warning] 403 Forbidden but rate limit remains. Waiting 10s and retrying...")
                    time.sleep(10)
                    continue

            # Other Errors
            else:
                print(f"  [Warning] API returned status {response.status_code}. Skipping.")
                return False

        except Exception as e:
            print(f"  [Error] Failed to check GitHub API: {e}")
            return False

    print("  [Fail] Could not verify meaningfulness via API.")
    return False

# Return a list of Dockerfiles included in the repository
def check_dockerfile(repo, dirname, repo_dir):
    print("Checking Dockerfile in repo/{0}".format(dirname))
    dfile_list = []
    path = repo_dir + '/' + dirname
    for pathname, dirnames, filenames in os.walk(path):
        for filename in filenames:
            if filename == "Dockerfile" or filename == "dockerfile":
                print("found Dockerfile: {}/{}".format(pathname, filename))
                dfile_list.append(pathname+'/'+filename)
    #print("{}:{}".format(dirname, dfile_list))
    return dfile_list

#Find "FROM base:latest" in the Dockerfile
def number_latest_in_dockerfile(repo, dfile_list): # I think this function have bugs
    count = 0
    error = 0
    latest_commit_year = 0
    
    dfile_count = len(dfile_list)
    for dfile_path in dfile_list:
        colon_flag = False
        latest_flag = False
        commit_year = 0
        if not os.path.isdir(dfile_path):
            try:
                extracted_lines = extract_from_lines(dfile_path)
                for line in extracted_lines:
                    image, tag = parse_base_tag(line)
                    if tag is None or tag == 'latest':
                        count += 1
                        print("Try to check the latest commit year of {}".format(dfile_path))
                        commit_year = int(list(repo.iter_commits(paths=dfile_path, max_count=1))[0].committed_datetime.year)
                        break
            except Exception as e:
                #print("dfile error")
                error += 1
        else:
            dfile_count -= 1
        if commit_year > latest_commit_year:
            latest_commit_year = commit_year
    print("latest commit year: {0}".format(latest_commit_year))
    return dfile_count, count, latest_commit_year, error

# Analyze changes in Dockerfiles between release tags and latest tag 
def analyze_dockerfile_changes(repo, dirname, dfile_list):
    tags, non_release_tags = get_release_tags(repo)
    if len(tags) < 2:
        print("Not enough release tags to compare.")
        return None  # no comparison possible

    # Save newest only:
    newest_pinned_to_latest = None
    newest_latest_to_pinned = None
    newest_p2l_commit_time = 0
    newest_l2p_commit_time = 0

    # For each Dockerfile, compare its content between tags
    for dfile in dfile_list:

        # Extract FROM lines for each release tag
        history = []
        for t in tags:
            try:
                repo.git.checkout(t.name)
                lines = extract_from_lines(dfile)
                parsed = [parse_base_tag(l) for l in lines]
                history.append((t, parsed))
            except Exception:
                continue

        # Compare tag[i] → tag[i+1]
        for i in range(len(history) - 1):
            tag_old, from_old = history[i]
            tag_new, from_new = history[i+1]

            # Find differences
            for (old_img, old_tag), (new_img, new_tag) in zip(from_old, from_new):
                if old_tag == new_tag:
                    continue  # no change

                commit_time = tag_new.commit.committed_datetime.timestamp()

                # pinned → latest
                if old_tag not in [None, "latest"] and new_tag == "latest":
                    if commit_time > newest_p2l_commit_time:
                        newest_p2l_commit_time = commit_time
                        newest_pinned_to_latest = {
                            "file": dfile,
                            "old": old_tag,
                            "new": new_tag,
                            "tag_from": tag_old.name,
                            "tag_to": tag_new.name,
                            "commit": str(tag_new.commit.hexsha),
                            "commit_date": str(tag_new.commit.committed_datetime),
                        }

                # latest → pinned
                if old_tag == "latest" and new_tag not in [None, "latest"]:
                    if commit_time > newest_l2p_commit_time:
                        newest_l2p_commit_time = commit_time
                        newest_latest_to_pinned = {
                            "file": dfile,
                            "old": old_tag,
                            "new": new_tag,
                            "tag_from": tag_old.name,
                            "tag_to": tag_new.name,
                            "commit": str(tag_new.commit.hexsha),
                            "commit_date": str(tag_new.commit.committed_datetime),
                        }

    return {
        "pinned_to_latest": newest_pinned_to_latest,
        "latest_to_pinned": newest_latest_to_pinned,
    }

def output_result(using, not_using, out, out_non, process_id):
    for file in using:
        with open(out + f'_{file[1]}.txt', 'a') as result:
            result.write(file[0])
            result.write('\n')
    
    for file in not_using:
        with open(out_non + f'.txt', 'a') as result_no:
            result_no.write(file)
            result_no.write('\n')

def tag_report(output_dir, repo_with_tags, process_id):
    with open(os.path.join(output_dir, f'tag_report.txt'), 'a') as report:
        report.write(f"Process {process_id} tag report:\n")
        for repo, tags in repo_with_tags.items():
            report.write(f"Repository: {repo}\n")
            if tags is None:
                report.write("  No tags found\n")
            else:
                report.write(f"  Has Dockerfile: {tags['has_dockerfile']}\n")
                report.write("  Release tags:\n")
                for tag in tags["release_tags"]:
                    report.write(f"    {tag.name}\n")
                report.write("  Non-release tags:\n")
                for tag in tags["non_release_tags"]:
                    report.write(f"    {tag.name}\n")
            report.write("\n")
        
        report.write("--------------------------------------------------\n")

def change_tag_report(output_dir, pinned_to_latest_repos, latest_to_pinned_repos, process_id):
    with open(os.path.join(output_dir, f'change_tag_report.txt'), 'a') as report:
        report.write(f"Process {process_id} change tag report:\n")
        report.write("Changes pinned to latest:\n")
        for repo, change in pinned_to_latest_repos.items():
            report.write(f"Repository: {repo}\n")
            for key, value in change.items():
                report.write(f"  {key}: {value}\n")
            report.write("\n")

        report.write("Changes latest to pinned:\n")
        for repo, change in latest_to_pinned_repos.items():
            report.write(f"Repository: {repo}\n")
            for key, value in change.items():
                report.write(f"  {key}: {value}\n")
            report.write("\n")
        report.write("--------------------------------------------------\n")

def output_summary(output_dir, total_project, latest_project, total_dfile, latest_dfile, error_count, repo_not_found, pinned_to_latest, latest_to_pinned, process_id):
    with open(os.path.join(output_dir, f'summary_check_latest_in_dockerfile.txt'), 'a') as summary:
        summary.write(f"Process {process_id} results:\n")
        summary.write('Total project: {0}\n'.format(total_project))
        summary.write('Project using latest: {0}\n'.format(latest_project))
        summary.write('Total Dockerfile: {0}\n'.format(total_dfile))
        summary.write('Dockerfile using latest: {0}\n'.format(latest_dfile))
        summary.write('Number of changes pinned to latest: {0}\n'.format(pinned_to_latest))
        summary.write('Number of changes latest to pinned: {0}\n'.format(latest_to_pinned))
        summary.write('File not found or error: {0}\n'.format(error_count))
        summary.write('Repo not found: {0}\n'.format(repo_not_found))
        summary.write("--------------------------------------------------\n")

def setup_index(input_file, index, total_array_size):
    start = 0
    stop = -1
    if index == -1:
        return start, stop
    else:
        total_repo = sum(1 for line in open(input_file))
        base = total_repo // total_array_size
        start = index * base + min(index, total_repo % total_array_size)
        stop = (index + 1) * base + min(index + 1, total_repo % total_array_size)
        if stop > total_repo:
            stop = total_repo
        print("process repo from {} to {}".format(start, stop))
        return start, stop

def main(input_file, output_dir, repo_dir, start=0, stop=-1, process_id=0):
    test_count = 0
    project_count = 0
    error_count = 0
    latest_project_count = 0
    total_dfile = 0
    latest_dfile = 0
    repo_not_found = 0

    use_latest_project = []
    not_use_latest_project = []

    total_number_change_pinned_to_latest = 0
    total_number_change_latest_to_pinned = 0
    pinned_to_latest_repos = {}
    latest_to_pinned_repos = {}

    repo_tags = {}

    ####config####
    target_directory = repo_dir  # Specify the directory to delete

    output_file_use = os.path.join(output_dir, 'use_latest_project')
    output_file_nonuse = os.path.join(output_dir, 'non_use_latest_project')

    #clear_directory(target_directory)
    time.sleep(1)

    with open(input_file, 'r') as input_file:
        for i, reponame in enumerate(input_file):
            #clear_directory(target_directory)
            if i < start:
                continue
            if stop != -1 and i >= stop:
                break
            reponame = reponame.rstrip("\n")
            print(reponame)

            repo, dirname = clone_repo(reponame, repo_dir, output_dir)
            if repo != '': 

                if not is_repo_meaningful(repo, reponame):
                    print("Repo is not meaningful (No tags, low stars/forks). Skipping.")
                    # Clean up immediately
                    clear_directory(repo_dir + "/" + dirname)
                    if os.path.exists(repo_dir + "/" + dirname):
                        shutil.rmtree(repo_dir + "/" + dirname)
                    print("--------------------------------------------------")
                    continue

                project_count += 1
                dfile_list = check_dockerfile(repo, dirname, repo_dir)
                release, non_release = get_release_tags(repo)
                if not release and not non_release:
                    repo_tags[reponame] = None
                else:
                    repo_tags[reponame] = {
                        "has_dockerfile": False,
                        "release_tags": release,
                        "non_release_tags": non_release,
                    }
                    if len(dfile_list) > 0:
                        repo_tags[reponame]["has_dockerfile"] = True
            else: 
                dfile_list = []
                repo_not_found += 1
            
            if len(dfile_list) > 0:
                number_dfile, number_latest, commit_year, error = number_latest_in_dockerfile(repo, dfile_list)
                latest_dfile += number_latest
                if number_latest > 0:
                    latest_project_count += 1
                    use_latest_project.append([reponame, commit_year])
                else:
                    not_use_latest_project.append(reponame)
                total_dfile += number_dfile
                error_count += error

                changes = analyze_dockerfile_changes(repo, dirname, dfile_list)
                if changes:
                    if changes["pinned_to_latest"]:
                        total_number_change_pinned_to_latest += 1
                        pinned_to_latest_repos[reponame] = changes["pinned_to_latest"]

                    if changes["latest_to_pinned"]:
                        total_number_change_latest_to_pinned += 1
                        latest_to_pinned_repos[reponame] = changes["latest_to_pinned"]
            
            if len(dfile_list) == 0 or number_latest == 0:
                #clear the cloned repository that do not have Dockerfile or do not use latest
                clear_directory(repo_dir + "/" + dirname)
                if os.path.exists(repo_dir + "/" + dirname):
                    shutil.rmtree(repo_dir + "/" + dirname)
        
            print("--------------------------------------------------")

            # For operation confirmation, small number of repositories
            #test_count += 1
            #if test_count == 100:
            #    break
    output_result(use_latest_project, 
                    not_use_latest_project, 
                    output_file_use,
                    output_file_nonuse,
                    process_id)
    change_tag_report(output_dir,
                        pinned_to_latest_repos, 
                        latest_to_pinned_repos, 
                        process_id)
    tag_report(output_dir, 
                repo_tags, 
                process_id)
    output_summary(output_dir, 
                    project_count, 
                    latest_project_count, 
                    total_dfile, 
                    latest_dfile, 
                    error_count, 
                    repo_not_found, 
                    total_number_change_pinned_to_latest, 
                    total_number_change_latest_to_pinned, 
                    process_id)
            
            
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_file", type=str, default="input/repo_list_test.csv")
    parser.add_argument("--index", type=int, default=-1)
    parser.add_argument("--total_array_size", type=int, default=1)
    parser.add_argument("--output_dir", type=str, default="/output")
    parser.add_argument("--clone_repo_dir", type=str, default="/repo")
    parser.add_argument("--github_token", type=str, default="YOUR_GITHUB_TOKEN_HERE")
    args = parser.parse_args()
    args = vars(args)
    start, stop = setup_index(args['input_file'], args['index'], args['total_array_size'])
    GITHUB_TOKEN = args['github_token']
    main(args['input_file'], args['output_dir'], args['clone_repo_dir'], start, stop, args['index'])