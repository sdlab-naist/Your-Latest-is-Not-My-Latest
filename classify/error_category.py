#!/usr/bin/env python3
"""
categorize_error_type.py
========================

Reads a CSV of Docker build failures and adds a column giving the ERROR TYPE,
one of four categories:

    - Dependency Error
    - Missing Base Image Error
    - Command Execution Error
    - No Longer Supported Error

(plus "Uncategorized" when nothing matches).

Same machinery as the time-induced classifier: logs that got wrapped across
several CSV rows are stitched back together, each OS log (windows / linux / mac)
is classified independently, and the results are combined.

How a record's category is decided
----------------------------------
Each OS log is matched against the rule sets in the precedence order given by
CATEGORY_ORDER (first match wins for that OS). Architecture-mismatch noise
(`exec format error`, `no matching manifest for linux/...`) is treated as
non-informative and skipped, because these logs frequently show the real error
on the native-arch OS and only arch noise on the others. Across OSes, the
highest-precedence category found wins, and "Triggering OS" lists which OS(es)
produced it.

Calibration status
------------------
* "Dependency Error" rules are mined from the provided Dependency_Error_20p.csv
  sample (10/10 classified correctly).
* The other three categories currently use PROVISIONAL starter patterns based on
  domain knowledge. Send a labelled sample for each and they can be tightened the
  same way. They are ordered/limited so they do not steal Dependency records:
  e.g. a removed/obsolete package is treated as a Dependency Error (matching the
  sample), not "No Longer Supported".

Precedence (edit CATEGORY_ORDER to change it):
    Missing Base Image  ->  Dependency  ->  No Longer Supported  ->  Command Execution
Command Execution is last on purpose: almost every failed build ends with a
generic "returned a non-zero code", so it only applies when no more specific
category matched.

Usage
-----
    python error_category.py                       # uses the default below
    python error_category.py input/classify/categorize/input.csv             # custom input
    python error_category.py input/classify/categorize/input.csv output/classify/categorize/output.csv  # custom input + output
"""

import argparse
import os
import re
import sys

import pandas as pd


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

ID_COLUMN = "Repository"
LOG_COLUMNS = ["Log (windows)", "Log (linux)", "Log (mac)"]
DEFAULT_INPUT = "Dependency_Error_20p.csv"

# Precedence: earlier = higher priority when more than one category matches.
CATEGORY_ORDER = [
    "Missing Base Image Error",
    "Dependency Error",
    "No Longer Supported Error",
    "Command Execution Error",
]


# --------------------------------------------------------------------------- #
# Category rules — each entry is (reason, regex). Matching is case-insensitive
# and spans newlines. Add/remove signals freely.
# --------------------------------------------------------------------------- #
CATEGORY_RULES = {

    # ---- CALIBRATED from Missing_Image_Error_20p.csv -------------------------
    "Missing Base Image Error": [
        ("Base image / --from image cannot be resolved or pulled",
         r'failed to resolve reference "[^"]+":[^"\n]*(not found|manifest unknown)|'
         r"failed to resolve source metadata for|"
         r"pull access denied for [^,]+, repository does not exist|"
         r"manifest unknown|repository does not exist or may require|"
         r"invalid from flag value"),

        ("Remote base/artifact download not found (ADD/GET 404/403/410)",
         r"ADD failed: failed to GET [^\n]*status (404|403|410)\b|"
         r"failed to GET [^\n]*\b(404|403|410) Not Found"),

        ("Blank base-image ARG (build ARG never passed)",
         r"base name \([^)]*\) should not be blank"),

        ("Invalid image reference",
         r"invalid reference format"),
    ],

    # ---- CALIBRATED from Dependency_Error_20p.csv ----------------------------
    "Dependency Error": [
        ("apk cannot select/find package",
         r"unable to select packages|\(no such package\)|breaks: world\["),

        ("apt package/version unavailable",
         r"Unable to locate package|has no installation candidate|"
         r"E: Version .* was not found|"
         r"is not available, but is referred to by another package|"
         r"Depends:.*but it is not (installable|going to be installed)|"
         r"held broken packages"),

        ("yum/dnf package or repo unavailable",
         r"No package .* available|Cannot find a valid baseurl for repo|"
         r"Error: Unable to find a match|No match for argument|"
         r"nothing provides|Failed to download metadata for repo"),

        ("pip resolution/build failure",
         r"Could not find a version that satisfies the requirement|"
         r"No matching distribution found|Failed building wheel for|"
         r"Failed to build installable wheels|ResolutionImpossible"),

        ("npm/yarn resolution failure",
         r"npm ERR! (code )?(E404|ETARGET|ERESOLVE|ENOVERSIONS|ENOTFOUND)|"
         r"Couldn.t find any versions for|unable to resolve dependency tree"),

        ("conda resolution failure",
         r"ResolvePackageNotFound|PackagesNotFoundError|"
         r"Solving environment:[^\n]*failed|UnsatisfiableError"),

        ("composer resolution failure",
         r"Your requirements could not be resolved to an installable set of packages"),

        ("gem/bundler resolution failure",
         r"Could not find .* in any of the sources|"
         r"Bundler could not find compatible versions"),

        ("cargo resolution failure",
         r"no matching package named|failed to select a version for"),

        ("go module resolution failure",
         r"cannot find module providing package|"
         r"no required module provides package|missing go.sum entry"),

        ("swupd (Clear Linux) update failure",
         r"Failed to connect to update server|Unable to determine the server version"),

        # Fallback handled in code (see _failed_command_is_install): the failed
        # RUN step was itself a dependency/install command even though no
        # package-manager-specific error string was captured.
    ],

    # ---- PROVISIONAL: send a No-Longer-Supported sample to calibrate ---------
    # Kept narrow on purpose. A generic removed/obsolete *package* is a
    # Dependency Error per the sample, so those signals are NOT here. Note the
    # Docker "legacy builder is deprecated" boilerplate is deliberately excluded.
    "No Longer Supported Error": [
        ("EOL apt repo (Release file archived/gone)",
         r"does not have a Release file|Failed to fetch .*(Release|Packages)"),

        ("Expired/invalid repo signature",
         r"KEYEXPIRED|EXPKEYSIG|following signatures were invalid|NO_PUBKEY"),

        ("Cross-release / end-of-life",
         r"is older than remote repository|Cross release updates are only supported|"
         r"no longer supported|reached end.?of.?life|\bend of life\b"),
    ],

    # ---- CALIBRATED from Command_Error_20p.csv ------------------------------
    "Command Execution Error": [
        ("Compile / make / build error",
         r"make(\[\d+\])?: \*\*\*|No rule to make target|recipe for target .* failed|"
         r"collect2: error|undefined reference to|fatal error: .*: No such file|"
         r"CMake Error|configure: error|ninja: build stopped|"
         r"(cc|gcc|g\+\+|clang|c\+\+): error|ld: .*undefined|error: ld returned"),

        ("Runtime / test failure during build",
         r"Segmentation fault|core dumped|Assertion .* failed|Tests? failed|"
         r"Traceback \(most recent call last\)"),

        ("Command not found / shell syntax error",
         r"command not found|syntax error near|not a valid identifier"),

        # Fallback for logs truncated mid-build: no terminal error was captured,
        # but a compiler/build tool was clearly running. Low confidence, and only
        # reachable because Command Execution is last in CATEGORY_ORDER (so a more
        # specific category always wins first). Covers the truncated pecl/xdebug
        # compile in the sample.
        ("Build/compile step was running (log truncated before the error)",
         r"--mode=compile|running: make\b|\bphpize\b|/configure\b|cmake --build"),

        # Generic catch-all — keep LAST.
        ("Build step / command failed (non-zero exit)",
         r"returned a non-zero code: \d+|"
         r"did not complete successfully: exit code: \d+|"
         r'process ".*" did not complete'),
    ],
}


# --------------------------------------------------------------------------- #
# Helpers for the "failed RUN was a dependency/install command" heuristic
# --------------------------------------------------------------------------- #
INSTALL_VERBS = re.compile(
    r"\b(apk add|apt-get install|apt install|apt-get build-dep|"
    r"yum (-y )?install|dnf (-y )?install|zypper (in|install)|"
    r"pip3? install|python -m pip install|npm (install|i|ci)\b|"
    r"yarn (add|install)|pnpm (add|install)|gem install|bundle install|"
    r"cargo (install|build)|go (get|install)|conda (install|env)|mamba install|"
    r"composer (install|require)|cpanm|cpan |zef install|"
    r"raku(do)?brew build|luarocks install|opam install|poetry install|"
    r"pipenv install|nimble install|stack build)\b",
    re.IGNORECASE,
)

# Capture the command that failed (legacy builder + BuildKit forms). The full
# terminator ' returned a non-zero code lets the non-greedy capture skip inner
# quotes safely, so verbs are checked strictly inside the failed command.
_FAILED_CMD_PATTERNS = [
    re.compile(r"The command '(.*?)' returned a non-zero code", re.IGNORECASE | re.DOTALL),
    re.compile(r'process "(.*?)" did not complete successfully', re.IGNORECASE | re.DOTALL),
]


def _failed_command_is_install(text):
    """If the failed RUN command contains an install verb, return that verb."""
    for rx in _FAILED_CMD_PATTERNS:
        for m in rx.finditer(text):
            vm = INSTALL_VERBS.search(m.group(1))
            if vm:
                return vm.group(0)
    return None


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

def _compile(rules):
    # Accept (reason, pattern) or (reason, pattern_part1, pattern_part2, ...).
    compiled = []
    for entry in rules:
        reason, *pattern_parts = entry
        pattern = "".join(pattern_parts)
        compiled.append((reason, re.compile(pattern, re.IGNORECASE | re.DOTALL)))
    return compiled


_COMPILED = {cat: _compile(rules) for cat, rules in CATEGORY_RULES.items()}

_ARCH_NOISE = re.compile(
    r"exec format error|no matching manifest for linux|no match for platform|"
    r"cannot execute binary file|platform .* does not match",
    re.IGNORECASE | re.DOTALL,
)

_ANSI_REAL = re.compile(r"\x1b\[[0-9;]*m")
_ANSI_LITERAL = re.compile(r"\\x1b\[[0-9;]*m")
_WS = re.compile(r"\s+")

# Sentinel used for OS logs that only show arch-mismatch noise.
_ARCH = "__ARCH_NOISE__"


def clean(text):
    """Strip ANSI colour codes (real and literally-escaped)."""
    if text is None:
        return ""
    text = _ANSI_REAL.sub("", text)
    text = _ANSI_LITERAL.sub("", text)
    return text


def _snippet(s):
    s = _WS.sub(" ", s.strip())
    return s[:160] + "…" if len(s) > 160 else s


def _os_label(col):
    """'Log (windows)' -> 'windows'; falls back to the column name."""
    m = re.search(r"\(([^)]*)\)", col)
    return m.group(1) if m else col


def classify_os(text):
    """Classify a SINGLE OS log. Return (category, reason, evidence)."""
    for cat in CATEGORY_ORDER:
        for reason, rx in _COMPILED[cat]:
            m = rx.search(text)
            if m:
                return cat, reason, _snippet(m.group(0))
        # Dependency heuristic runs at Dependency's place in the precedence order.
        if cat == "Dependency Error":
            verb = _failed_command_is_install(text)
            if verb:
                return (cat,
                        "failed RUN was a dependency/install command",
                        _snippet(f"{verb} (in the failed RUN command)"))
    if _ARCH_NOISE.search(text):
        return _ARCH, "", ""
    return "Uncategorized", "", ""


def classify_record(os_logs):
    """
    Classify one Dockerfile from its per-OS logs.

    os_logs: dict {os_label: log_text}. Each OS is classified; arch-noise-only
    and blank logs are skipped. The winning category is the highest-precedence
    one found across the informative OSes. Returns
    (category, reason, evidence, triggering_os).
    """
    per_os = {}
    for name, text in os_logs.items():
        if text and text.strip() and text.strip().lower() != "nan":
            per_os[name] = classify_os(text)

    scored = [(name, c, r, e) for name, (c, r, e) in per_os.items()
              if c in CATEGORY_ORDER]
    if not scored:
        return "Uncategorized", "", "", ""

    best = min(scored, key=lambda x: CATEGORY_ORDER.index(x[1]))
    category = best[1]
    winners = [name for name, c, r, e in scored if c == category]
    _, _, reason, evidence = best
    return category, reason, evidence, ", ".join(winners)


def label_dataframe(df):
    """Add category columns to df in place and return it."""
    log_cols = [c for c in LOG_COLUMNS if c in df.columns]
    if not log_cols:
        raise ValueError(
            f"None of the expected log columns {LOG_COLUMNS} are in the CSV."
        )
    if ID_COLUMN not in df.columns:
        raise ValueError(f"ID column '{ID_COLUMN}' is not in the CSV.")

    # Regroup wrapped continuation rows under their parent record.
    is_new = df[ID_COLUMN].notna() & (df[ID_COLUMN].astype(str).str.strip() != "")
    groups = is_new.cumsum()

    category = pd.Series("", index=df.index, dtype=object)
    reason = pd.Series("", index=df.index, dtype=object)
    evidence = pd.Series("", index=df.index, dtype=object)
    trigger = pd.Series("", index=df.index, dtype=object)

    for _, idx in groups.groupby(groups).groups.items():
        parent = idx[0]
        os_logs = {}
        for c in log_cols:
            frags = [clean(str(v)) for v in df.loc[idx, c] if pd.notna(v)]
            os_logs[_os_label(c)] = "\n".join(frags)

        cat, r, e, t = classify_record(os_logs)
        category.at[parent] = cat      # label only the parent row
        reason.at[parent] = r
        evidence.at[parent] = e
        trigger.at[parent] = t

    df["Error Category"] = category
    df["Category Reason"] = reason
    df["Category Evidence"] = evidence
    df["Triggering OS"] = trigger
    return df, int(is_new.sum()), len(df) - int(is_new.sum())


def print_summary(df, n_records, n_cont):
    labeled = df[df["Error Category"] != ""]
    print(f"Logical records: {n_records}  |  continuation rows merged: {n_cont}")
    print()
    print("Error-category counts (per record):")
    print(labeled["Error Category"].value_counts().to_string())

    unc = labeled[labeled["Error Category"] == "Uncategorized"]
    if len(unc):
        print(f"\n--- {len(unc)} Uncategorized record(s) (need review) ---")
        for _, r in unc.iterrows():
            print(f"  • {r[ID_COLUMN]}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Categorize Docker build failures into four error types."
    )
    parser.add_argument("input", nargs="?", default=DEFAULT_INPUT,
                        help=f"input CSV (default: {DEFAULT_INPUT})")
    parser.add_argument("output", nargs="?", default=None,
                        help="output CSV (default: <input>_categorized.csv)")
    args = parser.parse_args(argv)

    if not os.path.exists(args.input):
        parser.error(f"input file not found: {args.input}")

    output = args.output
    if output is None:
        base, ext = os.path.splitext(args.input)
        output = f"{base}_categorized{ext or '.csv'}"

    df = pd.read_csv(args.input)
    print(f"Loaded '{args.input}': {len(df)} rows, {len(df.columns)} columns.")

    df, n_records, n_cont = label_dataframe(df)
    print_summary(df, n_records, n_cont)

    df.to_csv(output, index=False)
    print(f"\nSaved categorized CSV to '{output}'")


if __name__ == "__main__":
    sys.exit(main())