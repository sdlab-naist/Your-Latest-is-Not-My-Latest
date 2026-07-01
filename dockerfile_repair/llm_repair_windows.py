import json
import os
import shutil
import re
import docker
import argparse
import logging
import subprocess
import gc
import git
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

docker_client = docker.from_env(timeout=600)

# Default model per provider, used when --model is not passed. Shared by both
# --mode api (SDK call) and --mode agent (coding CLI).
MODELS = {"gpt": "gpt-5.5", "claude": "claude-opus-4-8", "gemini": "gemini-3.1-pro-preview"}

# Max wall-clock seconds for a single agentic CLI invocation before it is killed.
AGENT_TIMEOUT = 900

# Map provider -> agentic coding CLI used in --mode agent.
#   claude -> Claude Code, gpt -> Codex, gemini -> Gemini CLI
# On Windows these ship two ways: a native standalone .exe (e.g. the Claude Code installer
# drops claude.exe in ~/.local/bin) and an npm .cmd wrapper (e.g. %APPDATA%\npm\claude.cmd).
# subprocess cannot launch a bare ".cmd"/".exe" name without a full path, and PATH may not
# include either install dir in the launching shell, so resolve to an absolute path here.
# Prefer the native .exe: it has no cmd.exe shim, which is more reliable when we pipe a
# large prompt through stdin and capture stdout (see run_agent_repair).
def _resolve_cli(name):
    # Build candidate dirs from the HOME directory, not from APPDATA/LOCALAPPDATA env vars.
    # Some launch contexts (services, schedulers, stripped-env shells) drop those vars while
    # still resolving the home dir, which is why claude (found in ~/.local/bin) could resolve
    # while codex (which lived only under %APPDATA%\npm) silently fell back to the bare name.
    home = os.path.expanduser("~")
    native_dirs = [
        os.path.join(home, ".local", "bin"),                               # native installer
        os.path.join(home, "AppData", "Local", "Programs", "claude"),      # home-derived
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "claude"),
    ]
    npm_dirs = [
        os.path.join(home, "AppData", "Roaming", "npm"),                   # npm -g, home-derived
        os.path.join(home, "AppData", "Local", "npm"),
        os.path.join(os.environ.get("APPDATA", ""), "npm"),                # env-var fallbacks
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "npm"),
        os.path.join(os.environ.get("ProgramFiles", ""), "nodejs"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "nodejs"),
    ]
    # 1) native .exe in a known install dir (most reliable to launch)
    for d in native_dirs:
        cand = os.path.join(d, name + ".exe")
        if d and os.path.isfile(cand):
            return cand
    # 2) anything on PATH (.exe preferred over the .cmd shim)
    found = shutil.which(name) or shutil.which(name + ".cmd")
    if found:
        return found
    # 3) probe the install dirs directly in case PATH omits them
    for d in native_dirs + npm_dirs:
        for ext in (".exe", ".cmd", ""):
            cand = os.path.join(d, name + ext)
            if d and os.path.isfile(cand):
                return cand
    return name  # unresolved: run_agent_repair surfaces a clear "install it with ..." error

# Hard-coded absolute paths for THIS machine (SDLab01). These avoid any PATH/APPDATA
# resolution at runtime — see the WinError 2 history. If a path is ever missing (e.g. the
# CLI is reinstalled elsewhere), _resolve_cli() is used as a fallback for that provider.
_HARDCODED_CLI = {
    "claude": r"C:\Users\SDLab01\.local\bin\claude.exe",          # native exe (no .cmd shim)
    "gpt":    r"C:\Users\SDLab01\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\npm\codex.cmd",  # npm global
    "gemini": r"C:\Users\SDLab01\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\npm\gemini.cmd", # npm global
}
_FALLBACK_NAME = {"claude": "claude", "gpt": "codex", "gemini": "gemini"}
AGENT_CLI = {
    k: (path if os.path.isfile(path) else _resolve_cli(_FALLBACK_NAME[k]))
    for k, path in _HARDCODED_CLI.items()
}

# Must match check_build_latest.py: repair runs on the latest RELEASE before this date
# (falling back to the latest commit before it), i.e. the same Dockerfile version that was
# originally built and failed.
CUTOFF_DATE = "2026-03-21"

# Few-shot examples injected into both --mode api and --mode agent prompts to steer the
# kind of fix expected. Example 1 is the lesson from activepieces issue #9568 (an unpinned
# :latest base image whose new major version broke things). Example 2 is a full
# before/after for a "No Longer Supported Error".
FEW_SHOT_EXAMPLES = r'''
--- Reference examples of the expected style of fix ---

Example 1 — an unpinned `:latest` base image whose new major version introduced a
breaking change (see activepieces issue #9568, where postgres:latest moved its data
directory in v18 and broke existing setups). Fix: pin the base image to the known-good
major version, or update the Dockerfile to satisfy the new version's requirements, instead
of leaving an unpinned `:latest`.

Example 2 — No Longer Supported Error (base image uses an obsolete manifest format).

Error Category: No Longer Supported Error

Failed Build Log:
    Step 1/3 : FROM jmferrer/apache2-reverse-proxy:latest
    not implemented: media type "application/vnd.docker.distribution.manifest.v1+prettyjws"
    is no longer supported since containerd v2.1, please rebuild the image as
    "application/vnd.docker.distribution.manifest.v2+json" or
    "application/vnd.oci.image.manifest.v1+json"

Original Dockerfile:
```dockerfile
FROM jmferrer/apache2-reverse-proxy:latest

COPY ports.conf /etc/apache2/sites-enabled
COPY proxy.conf /etc/apache2/sites-enable
```

Fixed Dockerfile:
```dockerfile
FROM ubuntu:24.04

RUN apt-get update && \
    apt-get install -y apache2 && \
    rm -rf /var/lib/apt/lists/*

# Modules a reverse proxy needs (same ones the old image enabled)
RUN a2enmod proxy proxy_http proxy_balancer lbmethod_byrequests rewrite headers

COPY ports.conf /etc/apache2/sites-enabled/
COPY proxy.conf /etc/apache2/sites-enabled/

EXPOSE 80

CMD ["apache2ctl", "-D", "FOREGROUND"]
```
The obsolete base image is replaced by rebuilding the same functionality (an apache2
reverse proxy) on a current, supported base image.

Example 3 — Dependency Error (`apt-get update` 404 on an end-of-life Debian base, whose
packages have moved to archive.debian.org; see e.g. docker/for-linux issue #167 and
docker-library/python issue #386).

Error Category: Dependency Error

Failed Build Log:
    Step 2/3 : RUN apt-get update && apt-get install -y curl ca-certificates
    W: Failed to fetch http://deb.debian.org/debian/dists/jessie/main/binary-amd64/Packages  404  Not Found
    E: Some index files failed to download. They have been ignored, or old ones used instead.

Original Dockerfile:
```dockerfile
FROM debian:jessie

RUN apt-get update && \
    apt-get install -y curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*
```

Fixed Dockerfile:
```dockerfile
FROM debian:jessie

# Jessie is end-of-life: its packages now live on archive.debian.org and the normal
# mirrors return 404. Point apt at the archive and skip the expired Valid-Until check.
RUN printf 'deb [check-valid-until=no] http://archive.debian.org/debian jessie main\n' \
        > /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*
```
The packages were unreachable because the release is archived; pointing apt at
archive.debian.org fixes the fetch. (The longer-term alternative is to migrate to a
supported base such as debian:bookworm.)

Example 4 — a `:latest` tag silently pulled a newer language runtime that broke the build
(`python:latest` moved to Python 3.12, which removed the stdlib `distutils` module that
many packages still import at install time; see docker-library/python issue #971 and
Pythagora-io/gpt-pilot issue #126). Fix: pin the image to the known-good version instead
of `:latest`.

Error Category: Dependency Error

Failed Build Log:
    Step 4/6 : RUN pip install -r requirements.txt
    Collecting ...
        File ".../setup.py", line 2, in <module>
            from distutils.core import setup
    ModuleNotFoundError: No module named 'distutils'

Original Dockerfile:
```dockerfile
FROM python:latest

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "app.py"]
```

Fixed Dockerfile:
```dockerfile
# python:latest now resolves to 3.12, which removed distutils and breaks packages that
# still import it. Pin to the last release that ships distutils.
FROM python:3.11

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "app.py"]
```
The unpinned `:latest` tag pulled an incompatible runtime; pinning to python:3.11 restores
a compatible environment. (Alternatively, stay on 3.12 and add `setuptools` so distutils
is available again.)
--- End of examples ---
'''

def setup_logging(output_dir, llm):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    log_file = os.path.join(output_dir, f"repair_dockerfile_{llm}.log")
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    logging.getLogger("git").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

def load_completed(output_dir):
    """Returns a set of already-completed dockerfile paths by reading existing result files."""
    completed = {}
    if not os.path.exists(output_dir):
        return completed
    for filename in os.listdir(output_dir):
        if filename.endswith('.json'):
            filepath = os.path.join(output_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    item = json.load(f)
                completed[item['Dockerfile']] = filename
            except Exception:
                pass
    return completed

def save_result(output_dir, index, item):
    """Saves a single result to its own numbered JSON file."""
    os.makedirs(output_dir, exist_ok=True)
    filename = f"{index:03d}.json"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(item, f, indent=4)
    logging.info(f"Result saved to {filepath}")

# Build logs can be enormous (some exceed millions of tokens, far past any model's context
# window). Cap what we send to the model: keep the head (base image / early steps) and the
# tail (where the actual failure usually is), dropping the middle. ~4 chars/token, so this
# stays comfortably within limits.
MAX_LOG_CHARS = 60000

def truncate_log(text, max_chars=MAX_LOG_CHARS):
    if not text or len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars - head
    omitted = len(text) - max_chars
    return (
        text[:head]
        + f"\n\n... [truncated {omitted} characters of build log] ...\n\n"
        + text[-tail:]
    )

def generate_fix(provider, dockerfile_content, error_category, error_log, model):
    error_log = truncate_log(error_log)
    prompt = f"""
    You are an expert DevOps engineer. The following Dockerfile failed to build.

    The builds execute in a controlled environment running on Docker version 29.0.3, build 511dad6
{FEW_SHOT_EXAMPLES}
    Now fix the following Dockerfile.

    Error Category: {error_category}

    Failed Build Log:
    {error_log}

    Original Dockerfile:
```dockerfile
    {dockerfile_content}
```

    Please provide the corrected Dockerfile.
    IMPORTANT: Output ONLY the raw Dockerfile code inside a ```dockerfile block. Do not include any explanations or other text.
    """

    if provider == 'gpt':
        import openai
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        # temperature is not configurable on newer models (e.g. gpt-5.5); omit it.
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content

    elif provider == 'claude':
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        # temperature is deprecated on newer models (e.g. claude-opus-4-8); omit it.
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    elif provider == 'gemini':
        from google import genai
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        response = client.models.generate_content(
            model=model,
            contents=prompt
        )
        return response.text

    else:
        raise ValueError(f"Unknown provider: {provider}")

def build_agent_prompt(dockerfile_rel, error_category, error_log):
    """Prompt for --mode agent: the CLI runs inside the repo and edits the file in place."""
    error_log = truncate_log(error_log)
    return f"""You are an expert DevOps engineer working inside a real git repository.

A Dockerfile in this repository fails to build. Your job is to fix it.

The Dockerfile is at this path (relative to your current working directory):
    {dockerfile_rel}

The builds execute in a controlled environment running Docker version 29.0.3, build 511dad6.
{FEW_SHOT_EXAMPLES}
Error Category: {error_category}

Failed Build Log:
{error_log}

Instructions:
1. Read the Dockerfile at {dockerfile_rel}, and also read any neighbouring files in the
   build context (the files it COPYs/ADDs, package manifests, scripts, etc.) so you
   understand the real context before changing anything.
2. Edit the Dockerfile **in place** at {dockerfile_rel} so the build will succeed.
3. Do NOT run `docker build` or any docker command — a separate controlled build will
   verify your fix.
4. Do NOT modify any file other than {dockerfile_rel}.
The edited Dockerfile is the only deliverable; no explanation is needed."""

def build_agent_command(provider, model):
    """Returns the argv list for a non-interactive agentic CLI run.

    The prompt is NOT placed on the command line — on Windows the failed build log can be
    20KB+, which together with the few-shot examples easily exceeds the ~32,767-char
    CreateProcess command-line limit and makes the CLI exit immediately. Instead the prompt
    is fed via stdin (see run_agent_repair); all three CLIs support reading it that way.

    All three are configured to auto-approve file edits but NOT run builds or reach the
    network, so the script alone owns the single controlled `docker build`.
    """
    if provider == "claude":
        # Claude Code headless. `-p` with no inline value reads the prompt from stdin.
        # Omitting Bash prevents it from running docker build itself.
        return [
            AGENT_CLI[provider], "-p",
            "--model", model,
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read Edit Glob Grep",
        ]
    elif provider == "gpt":
        # Codex non-interactive. With no positional prompt it reads instructions from stdin.
        # On Windows codex has no working write-sandbox, so --full-auto / --sandbox
        # workspace-write both silently degrade to read-only and the agent can't edit the
        # Dockerfile. --dangerously-bypass-approvals-and-sandbox lets it write; safe enough
        # here because the prompt restricts it to editing the Dockerfile and this script owns
        # the controlled docker build. model_reasoning_effort=high for better fixes.
        return [
            AGENT_CLI[provider], "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c", "model_reasoning_effort=high",
            "-m", model,
        ]
    elif provider == "gemini":
        # Gemini CLI non-interactive. --yolo auto-approves file edits. The -p value is
        # appended to stdin input, so a short directive here + full prompt on stdin works.
        # --skip-trust: a freshly cloned repo is not a "trusted folder", and without it
        # gemini downgrades --yolo to default approval and exits 55 without editing.
        return [AGENT_CLI[provider], "-m", model, "--yolo", "--skip-trust", "-p", "Follow the instructions provided on standard input."]
    else:
        raise ValueError(f"Unknown provider: {provider}")

def run_agent_repair(provider, dockerfile_path, cwd, error_category, error_log, model):
    """Run the agentic CLI in `cwd`; it edits the Dockerfile in place.

    Returns the captured CLI stdout+stderr (truncated) for traceability. The file is read
    back by the caller regardless of the CLI's exit status.
    """
    dockerfile_rel = os.path.relpath(dockerfile_path, cwd)
    prompt = build_agent_prompt(dockerfile_rel, error_category, error_log)
    cmd = build_agent_command(provider, model)

    logging.info(f"Running agent CLI '{AGENT_CLI[provider]}' (model {model}) in {cwd} ...")
    try:
        # Prompt goes via stdin to avoid the Windows command-line length limit.
        result = subprocess.run(
            cmd,
            cwd=cwd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=AGENT_TIMEOUT,
            env=os.environ.copy(),
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            tail = output.strip()[-1500:] or "(no output captured)"
            logging.warning(f"Agent CLI exited with code {result.returncode}. Output tail:\n{tail}")
    except subprocess.TimeoutExpired as e:
        logging.warning(f"Agent CLI timed out after {AGENT_TIMEOUT}s and was killed.")
        output = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else "Agent CLI timed out."
    except FileNotFoundError:
        install_cmds = {"claude": "npm i -g @anthropic-ai/claude-code", "gpt": "npm i -g @openai/codex", "gemini": "npm i -g @google/gemini-cli"}
        logging.error(
            f"Agent CLI '{AGENT_CLI[provider]}' not found. "
            f"Install it with:  {install_cmds.get(provider, 'see provider docs')}  "
            f"then re-authenticate if needed."
        )
        raise

    return output

def find_repo_root(start_dir):
    """Walk upward from start_dir to the nearest directory containing .git; fall back to start_dir."""
    current = os.path.abspath(start_dir)
    while True:
        if os.path.isdir(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(start_dir)
        current = parent

def clear_directory(target_dir):
    if not os.path.exists(target_dir):
        return
    if not os.path.isdir(target_dir):
        return
    for item in os.listdir(target_dir):
        item_path = os.path.join(target_dir, item)
        try:
            logging.info(f"Deleting: {item_path}")
            if os.path.isfile(item_path) or os.path.islink(item_path):
                os.remove(item_path)
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)
        except Exception as e:
            logging.warning(f"Failed to delete: {item_path}, Error: {e}")

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

def clone_repo(reponame, repo_dir):
    dir_name = reponame.replace('/', '_')
    logging.info(f"Start cloning: {reponame}")
    try:
        target = os.path.join(repo_dir, dir_name)
        if not os.path.exists(target):
            logging.info(f"Cloning into {target}...")
            repo = git.Repo.clone_from(f'git@github.com:{reponame}.git', target)
        else:
            logging.info(f"Directory {target} already exists.")
            repo = git.Repo(target)
        logging.info(f"Clone completed: {reponame}")

        # Match check_build_latest.py: fetch + recover HEAD, then check out the latest
        # RELEASE before CUTOFF_DATE (falling back to the latest commit before it) so we
        # repair the exact Dockerfile version that was originally tested (not whatever the
        # default branch points at now).
        try:
            repo.git.fetch('--all')
            _ = repo.head.commit
            repo.git.reset('--hard', 'HEAD')
        except Exception as e:
            logging.warning(f"HEAD recovery issue for {reponame}: {e}")

        label, hexsha_cutoff = pick_version_before_cutoff(repo, target, CUTOFF_DATE)
        if hexsha_cutoff:
            repo.git.checkout(hexsha_cutoff, force=True)
            logging.info(f"Checked out {label}: {hexsha_cutoff[:7]}")
        else:
            logging.warning(f"No commit before {CUTOFF_DATE} for {reponame}; staying on current HEAD.")

    except Exception as e:
        logging.warning(e)
        repo = ''
    return repo, dir_name

def extract_dockerfile_code(llm_response):
    """Pull the raw Dockerfile out of an LLM response.

    Handles the common case where the model wraps the file in a Markdown code fence, e.g.
    ```dockerfile ... ```, a plain ``` ... ``` with no language tag, or a wrong tag like
    ```docker / ```bash. The language tag is matched case-insensitively and stripped so it
    never leaks into the returned code. When several fences are present, a dockerfile-tagged
    block is preferred; otherwise the first block is used. If the model returns no fence at
    all (or a truncated, unclosed one), the text is returned as-is, minus a stray leading
    language token.
    """
    if not llm_response:
        return ""

    # All fenced blocks: (language tag, body). Opening fence may carry a tag on its line.
    blocks = re.findall(r"```[ \t]*([\w.+-]*)[ \t]*\r?\n(.*?)```", llm_response, re.DOTALL)
    if blocks:
        for lang, body in blocks:
            if lang.lower() in ("dockerfile", "docker"):
                return body.strip()
        return blocks[0][1].strip()

    # Opening fence but no closing one (e.g. response truncated): take everything after it.
    open_only = re.search(r"```[ \t]*[\w.+-]*[ \t]*\r?\n(.*)", llm_response, re.DOTALL)
    if open_only:
        return open_only.group(1).strip()

    # No fence at all: strip a stray leading "dockerfile"/"docker" label if present.
    text = re.sub(r"^(dockerfile|docker)\s*\n", "", llm_response.strip(), flags=re.IGNORECASE)
    return text.strip()

def docker_login():
    logging.info("Start Docker login...")
    docker_user = os.getenv("DOCKER_USER")
    docker_pass = os.getenv("DOCKER_PASS")
    if docker_user and docker_pass:
        try:
            docker_client.login(username=docker_user, password=docker_pass)
            logging.info("Docker login successful.")
        except docker.errors.APIError as e:
            logging.error(f"Docker login failed: {e}")
            exit(1)
    else:
        logging.info("No Docker credentials provided, skipping login.")

def docker_prune():
    try:
        logging.info("Cleaning up Docker images and volumes...")
        subprocess.run(["docker", "system", "prune", "-a", "-f", "--volumes"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logging.info("Docker images and volumes pruned successfully.")
    except subprocess.CalledProcessError as e:
        logging.warning(f"Docker prune failed: {e}. Continuing without cleanup.")

def try_docker_build(dockerfile_path):
    build_context = os.path.dirname(dockerfile_path)
    cmd = ["docker", "build", "-f", dockerfile_path, "--rm", "--force-rm", "--no-cache", build_context]

    env = os.environ.copy()
    env["DOCKER_BUILDKIT"] = "0"

    timeout_seconds = 3600  # 1 hour timeout for builds, adjust as needed

    try:
        time_start = time.time()
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",   # Docker output is UTF-8; avoid Windows cp1252 decode crash
            errors="replace",
            bufsize=1,
            env=env
        )

        log_lines = []
        for line in process.stdout:
            line = line.rstrip()
            logging.info(f"[BUILD] {line}")
            log_lines.append(line)
            if time.time() - time_start > timeout_seconds:
                process.kill()
                logging.warning("Build timed out and was killed.")
                log_lines.append("Build timed out and was killed.")
                break

        process.wait()
        log_output = "\n".join(log_lines)

        if process.returncode == 0:
            return True, log_output
        else:
            return False, log_output

    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def get_error_category(error_message):
    msg = str(error_message).lower()

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

def resolve_dockerfile(directory):
    """Return the Dockerfile path inside `directory`, matching either 'Dockerfile' or
    'dockerfile' (Linux is case-sensitive, unlike macOS/Windows). Falls back to a
    case-insensitive scan, then to '<directory>/Dockerfile' if none exists so the caller's
    os.path.exists check can still decide to skip."""
    for name in ("Dockerfile", "dockerfile"):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    try:
        for entry in os.listdir(directory):
            if entry.lower() == "dockerfile":
                return os.path.join(directory, entry)
    except (FileNotFoundError, NotADirectoryError):
        pass
    return os.path.join(directory, "Dockerfile")

def process_repairs(input_json, output_dir, provider, mode, model):
    with open(input_json, 'r', encoding='utf-8') as f:
        data = json.load(f)

    completed = load_completed(output_dir)
    if completed:
        logging.info(f"Resuming: {len(completed)} already completed, {len(data) - len(completed)} remaining.")

    for i, item in enumerate(data):
        docker_prune()
        repo = item.get('Repository', 'Unknown')
        dockerfile_dir = item.get('Dockerfile')
        index = i + 1

        if dockerfile_dir in completed:
            logging.info(f"Skipping (already done): {repo}")
            continue

        dockerfile_path = resolve_dockerfile(dockerfile_dir)
        category = item.get('Category')
        old_log = item.get('Log')

        logging.info(f"--- [{index:03d}] Processing: {repo} ---")

        if not os.path.exists(dockerfile_path):
            # repo/ lives next to this script's parent dir, so this resolves correctly on
            # any machine instead of being pinned to one absolute path.
            temp_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "repo"
            )
            clone, repo_dir = clone_repo(repo, temp_dir)
            if not clone:
                logging.warning(f"Clone failed for {repo}. Skipping.")
                continue
            # Derive the Dockerfile's subpath inside the repo by anchoring on the repo
            # folder name, so it works regardless of the absolute path prefix recorded in
            # the input JSON (which may have been generated on another machine). Empty
            # subpath means the Dockerfile sits at the repo root. Normalise separators so
            # both Windows (\) and POSIX (/) recorded paths split correctly.
            parts = dockerfile_dir.replace('\\', '/').rstrip('/').split('/')
            sub_parts = parts[parts.index(repo_dir) + 1:] if repo_dir in parts else []
            dockerfile_path = resolve_dockerfile(os.path.join(temp_dir, repo_dir, *sub_parts))
            if not os.path.exists(dockerfile_path):
                logging.warning(f"File not found: {dockerfile_path}. Skipping.")
                continue

        with open(dockerfile_path, 'r', encoding='utf-8', errors='replace') as df_file:
            original_code = df_file.read()

        backup_path = f"{dockerfile_path}.backup"

        agent_output = None

        try:
            shutil.copy2(dockerfile_path, backup_path)

            if mode == "agent":
                # Run the coding CLI inside the repo once; it edits the Dockerfile in place.
                cwd = find_repo_root(os.path.dirname(dockerfile_path))
                logging.info(f"Requesting fix from {provider} agent ({AGENT_CLI[provider]})...")
                agent_output = run_agent_repair(provider, dockerfile_path, cwd, category, old_log, model)
                with open(dockerfile_path, 'r', encoding='utf-8', errors='replace') as df_file:
                    fixed_code = df_file.read()
                if fixed_code == original_code:
                    logging.warning("Agent did not change the Dockerfile.")
            else:
                logging.info(f"Requesting fix from {provider} (API)...")
                raw_response = generate_fix(provider, original_code, category, old_log, model)
                fixed_code = extract_dockerfile_code(raw_response)
                with open(dockerfile_path, 'w', encoding='utf-8') as df_file:
                    df_file.write(fixed_code)

            logging.info("Attempting to build repaired Dockerfile...")
            success, new_log = try_docker_build(dockerfile_path)
            if success:
                logging.info("Build SUCCESS!")
                new_category = 'Success'
            else:
                logging.warning("Build FAILED.")
                new_category = get_error_category(new_log)

            item['Repair_Provider'] = provider
            item['Repair_Mode'] = mode
            item['Repair_Model'] = model
            item['Repaired_Code'] = fixed_code
            item['Repair_Success'] = success
            item['Repair_Log'] = new_log
            item['Repair_Error'] = new_category
            if agent_output is not None:
                item['Repair_Agent_Output'] = agent_output

            save_result(output_dir, index, item)

        except Exception as e:
            logging.error(f"Unexpected error while processing {repo}: {e}", exc_info=True)

        finally:
            if os.path.exists(backup_path):
                shutil.move(backup_path, dockerfile_path)
                logging.info("Restored original Dockerfile.")

            old_log = None
            original_code = None
            raw_response = None
            fixed_code = None
            new_log = None
            agent_output = None
            gc.collect()
            docker_prune()

    logging.info(f"All done! Results saved to {output_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Input JSON file with broken logs")
    parser.add_argument("--output", default="repair_results", help="Directory to save individual result files")
    parser.add_argument("--provider", choices=['gpt', 'claude', 'gemini'], required=True, help="Which LLM to use")
    parser.add_argument("--mode", choices=['agent', 'api'], default='agent',
                        help="agent = use the coding CLI (claude/codex/gemini) with repo context; "
                             "api = direct SDK call (original behavior)")
    parser.add_argument("--model", default=None,
                        help="Model id to use. Defaults to the per-provider default in MODELS "
                             "(gpt=%s, claude=%s, gemini=%s)." % (MODELS['gpt'], MODELS['claude'], MODELS['gemini']))

    args = parser.parse_args()
    model = args.model or MODELS[args.provider]

    # Repo root = parent of this script's dir, so paths resolve on any machine.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    setup_logging(os.path.join(repo_root, "output", "log", "dockerfile_repair"), args.provider)
    logging.info(f"Provider={args.provider} | Mode={args.mode} | Model={model}")
    if args.mode == "agent":
        _cli = AGENT_CLI[args.provider]
        logging.info(f"Agent CLI for '{args.provider}' resolved to: {_cli} (exists={os.path.isfile(_cli)})")
    docker_login()
    process_repairs(args.input, args.output, args.provider, args.mode, model)
