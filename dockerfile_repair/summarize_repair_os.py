import json
import os
import csv
import argparse

# Output dir layout: <base>/<os>/<llm_folder>/NNN.json
# One CSV is produced per LLM. Folder name -> human label / CSV stem.
LLMS = {
    "claude_code": "Claude Code",   # provider=claude, mode=agent
    "gpt_codex":   "Codex",         # provider=gpt,    mode=agent
    "claude":      "Claude (API)",  # provider=claude, mode=api
    "gpt":         "GPT (API)",     # provider=gpt,    mode=api
}
OSES = ["linux", "mac", "windows"]

# Excel/Google Sheets cap a single cell at 32767 chars. A log longer than this is split
# across extra rows: the first row holds the metadata + the first chunk of each OS log;
# each continuation row has blank metadata and only the next chunk of each log.
MAX_LOG_CELL = 32767


def _norm_category(value):
    """'Other: <full log>' cells are huge and multi-line; collapse to 'Other' (same
    normalization convert_csv_file.py uses) so each CSV cell stays tidy."""
    if isinstance(value, str) and value.startswith("Other:"):
        return "Other"
    return value if value is not None else ""


def collect(base, llm_folder):
    """Return {dockerfile: {os: {cat, err, code, log, success}}} for one LLM.

    Joins on the 'Dockerfile' field, which is the repo-relative path and is identical
    across OSes (the absolute prefix is not stored). 'cat' is the original pre-repair
    error category (the 'Category' field); 'code' is the LLM's Repaired_Code.
    """
    data = {}
    for os_name in OSES:
        d = os.path.join(base, os_name, llm_folder)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    item = json.load(f)
            except Exception:
                continue
            key = item.get("Dockerfile")
            if key is None:
                continue
            data.setdefault(key, {})[os_name] = {
                "cat": _norm_category(item.get("Category", "")),
                "err": _norm_category(item.get("Repair_Error", "")),
                "code": item.get("Repaired_Code", ""),
                "log": item.get("Repair_Log", ""),
                "success": item.get("Repair_Success", None),
            }
    return data


def read_original_code(repo_root, dockerfile_rel):
    """Read the original Dockerfile from disk for a repo-relative dir key, matching either
    'Dockerfile' or 'dockerfile'. Returns '' if the repo isn't cloned / file is missing."""
    if not repo_root:
        return ""
    directory = os.path.join(repo_root, *dockerfile_rel.replace("\\", "/").split("/"))
    candidates = [os.path.join(directory, "Dockerfile"), os.path.join(directory, "dockerfile")]
    try:
        candidates += [
            os.path.join(directory, e) for e in os.listdir(directory)
            if e.lower() == "dockerfile"
        ]
    except (FileNotFoundError, NotADirectoryError):
        pass
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception:
                return ""
    return ""


def load_excludes(path):
    """Return a set of Dockerfile keys to omit from the CSVs (one per line; blanks ignored)."""
    if not path or not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def success_cell(success):
    """True -> 1, False -> 0, missing -> '' (blank)."""
    if success is True:
        return 1
    if success is False:
        return 0
    return ""


def chunk_log(text, limit=MAX_LOG_CELL):
    """Split a log into pieces no longer than `limit`. Always returns at least one piece
    (an empty string for empty/short logs), so every OS contributes one cell on row 0."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def write_csv(out_path, data, include_log, include_code, repo_root):
    header = ["Dockerfile"]
    for os_name in OSES:
        header.append(f"Old_Category({os_name})")
        header.append(f"Repair_Error({os_name})")
        if include_log:
            header.append(f"Repair_Log({os_name})")
        header.append(f"Repair_Success({os_name})")
    header.append("OverallPass")
    if include_code:
        header.append("Original_Code")
        header += [f"Repaired_Code({os_name})" for os_name in OSES]
    n_code_cols = (1 + len(OSES)) if include_code else 0

    rows = 0
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for key in sorted(data):
            per = data[key]
            overall = 0
            os_cells = []  # (cat, err, [log chunks], success_cell) per OS
            for os_name in OSES:
                v = per.get(os_name) or {}
                if v.get("success") is True:
                    overall = 1  # OverallPass = 1 if ANY OS succeeded
                chunks = chunk_log(v.get("log", "")) if include_log else [""]
                os_cells.append((v.get("cat", ""), v.get("err", ""), chunks,
                                 success_cell(v.get("success"))))

            # Code columns live only on the first row of a record (treated as metadata).
            code_cells = []
            if include_code:
                code_cells = [read_original_code(repo_root, key)]
                code_cells += [(per.get(os_name) or {}).get("code", "") for os_name in OSES]

            if not include_log:
                # No log columns -> one tidy row per record.
                row = [key]
                for cat, err, _chunks, succ in os_cells:
                    row += [cat, err, succ]
                row.append(overall)
                row += code_cells
                w.writerow(row)
                rows += 1
                continue

            # One physical row per log chunk; metadata (incl. code) only on the first.
            n_lines = max(len(chunks) for _, _, chunks, _ in os_cells)
            for i in range(n_lines):
                if i == 0:
                    row = [key]
                    for cat, err, chunks, succ in os_cells:
                        row += [cat, err, chunks[0], succ]
                    row.append(overall)
                    row += code_cells
                else:
                    row = [""]  # blank Dockerfile
                    for _, _, chunks, _ in os_cells:
                        row += ["", "", chunks[i] if i < len(chunks) else "", ""]
                    row.append("")  # blank OverallPass
                    row += [""] * n_code_cols
                w.writerow(row)
            rows += 1  # count records, not physical rows
    return rows


def main(base, exclude_path, include_log, include_code, repo_root):
    excludes = load_excludes(exclude_path)
    if excludes:
        print(f"Excluding {len(excludes)} Dockerfiles listed in {exclude_path}\n")
    print(f"Repair_Log columns: {'included' if include_log else 'omitted'}")
    print(f"Code columns: {'included (repo_root=' + repo_root + ')' if include_code else 'omitted'}\n")
    for folder, label in LLMS.items():
        data = collect(base, folder)
        data = {k: v for k, v in data.items() if k not in excludes}
        out_path = os.path.join(base, f"repair_summary_{folder}.csv")
        n = write_csv(out_path, data, include_log, include_code, repo_root)
        passed = sum(
            1 for k in data
            if any(v.get("success") is True for v in data[k].values())
        )
        print(f"{label:14} -> {out_path}  ({n} Dockerfiles, {passed} OverallPass=1)")


def str2bool(v):
    if isinstance(v, bool):
        return v
    if str(v).strip().lower() in ("1", "true", "t", "yes", "y"):
        return True
    if str(v).strip().lower() in ("0", "false", "f", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(f"expected True/False, got {v!r}")


if __name__ == "__main__":
    default_base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "output", "dockerfile_repair",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=default_base,
                        help="Dir containing <os>/<llm>/NNN.json result files")
    parser.add_argument("--exclude", default=None,
                        help="File of Dockerfile keys to omit (default: <base>/exclude_dockerfiles.txt)")
    parser.add_argument("--log", type=str2bool, default=True,
                        help="True: include Repair_Log columns (long logs split across rows); "
                             "False: omit log columns (one row per Dockerfile)")
    parser.add_argument("--code", type=str2bool, default=False,
                        help="True: append Original_Code + Repaired_Code(<os>) columns "
                             "(original read from the repo Dockerfile on disk); False: omit")
    parser.add_argument("--repo-dir", dest="repo_dir", default=None,
                        help="Dir holding cloned repos for Original_Code "
                             "(default: <base>/../../repo)")
    args = parser.parse_args()
    exclude_path = args.exclude or os.path.join(args.base, "exclude_dockerfiles.txt")
    repo_root = args.repo_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(args.base))), "repo"
    )
    main(args.base, exclude_path, args.log, args.code, repo_root)
