#!/usr/bin/env python3
"""
classify_docker_errors.py
=========================

Reads a CSV of Docker build failures and adds a classification column saying
whether each record is a TIME-INDUCED problem, a NON-TIME-INDUCED problem, or
UNCATEGORIZED.

Definitions
-----------
TIME-INDUCED     The build would have worked when the Dockerfile was written but
                 broke later purely because time passed / upstream drifted:
                 base image upgrades, EOL or archived package repos, packages or
                 pinned versions removed upstream, expired signing keys, etc.

NON-TIME-INDUCED The failure comes from the repo / Dockerfile / build environment
                 itself and is independent of time: missing build-context files,
                 blank base-image ARGs, invalid image references, source-code /
                 command failures, permission errors, architecture mismatches,
                 BuildKit-only features used with the legacy builder, etc.
                 These patterns were mined directly from the provided
                 non-time-induced sample.

UNCATEGORIZED    Neither rule set matched -> review the record by hand.

Both rule sets are plain regex lists (TIME_INDUCED_RULES and
NON_TIME_INDUCED_RULES) so you can add or remove signals easily.

Precedence: time-induced rules are checked first (drift evidence is the more
notable root cause); if none match, non-time-induced rules are checked; the
non-time-induced list is ordered specific -> generic so the most informative
reason wins.

Usage
-----
    python classify_docker_errors.py                       # uses the defaults below
    python classify_docker_errors.py input.csv             # custom input
    python classify_docker_errors.py input.csv output.csv  # custom input + output
"""

import argparse
import os
import re
import sys

import pandas as pd


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Column that is only filled on the FIRST row of each record. Rows where this is
# blank are treated as continuation rows (a long log that spilled onto extra CSV
# rows) and are stitched back onto the record above them before matching.
ID_COLUMN = "Repository"

# Log columns to scan. All present ones are combined per record before matching.
LOG_COLUMNS = ["Log (windows)", "Log (linux)", "Log (mac)"]

# Default input CSV if none is passed on the command line.
DEFAULT_INPUT = "non_time_induced_sample_20p.csv"


# --------------------------------------------------------------------------- #
# TIME-INDUCED signals  (edit me) — each entry is (reason, regex)
# --------------------------------------------------------------------------- #
TIME_INDUCED_RULES = [
    ("Expired/invalid repo signature (GPG key rotated or expired over time)",
     r"The repository .* is not signed|public key is not available|GPG error"),

    ("Stale/EOL apt repo (release archived / Release file gone over time)",
     r"does not have a Release file|Failed to fetch .*(Release|Packages)|"
     r"E: The repository .* does not have a Release"),

    ("apt 404 on a package repo (packages removed or moved over time)",
     r"(security\.debian\.org|archive\.ubuntu\.com|deb\.debian\.org|/dists/|"
     r"packages\.sury\.org|http\.debian\.net)[^\n]*404\s+Not Found|"
     r"404\s+Not Found[^\n]*(Release|Packages|/dists/)"),

    ("apt package or pinned version no longer available",
     r"Unable to locate package|has no installation candidate|"
     r"E: Version '.*' for '.*' was not found|"
     r"Couldn't find any package by (glob|regex)"),

    ("yum/dnf repo metadata or mirror drift",
     r"Cannot find a valid baseurl for repo|No more mirrors to try|"
     r"Failed to download metadata for repo|Error: Unable to find a match|"
     r"nothing provides"),

    ("Alpine apk index/signature drift",
     r"unable to select packages|UNTRUSTED signature"),

    ("pip: required version no longer available (yanked/removed upstream)",
     r"Could not find a version that satisfies the requirement|"
     r"No matching distribution found"),

    ("Remote artifact/download returns 404/410 (pinned URL removed upstream)",
     r"failed to GET [^\n]*\b(404|410)\b|"
     r"ADD failed: failed to GET[^\n]*\b(404|410)\b|"
     r"The requested URL returned error:\s*(404|410)|"
     r"ERROR (404|410):|HTTP request sent[^\n]*\b(404|410)\b"),

    ("Base image or tag no longer exists in registry (removed/retagged over time)",
     r'failed to resolve reference "[^"]+":[^"\n]*(not found|manifest unknown)|'
     r"manifest unknown"),

    ("Base image repo no longer pullable (removed / access revoked over time)",
     r"pull access denied for [^,]+, repository does not exist"),

    ("conda: pinned package/version no longer resolvable",
     r"ResolvePackageNotFound|PackagesNotFoundError|Solving environment:[^\n]*failed"),

    ("composer: requirements no longer resolvable to an installable set",
     r"Your requirements could not be resolved to an installable set of packages"),

    ("TeX Live tlmgr cross-release mismatch (local older than remote year)",
     r"is older than remote repository|Cross release updates are only supported"),

    ("pacman: target package removed from repo over time",
     r"error: target not found:"),

    ("PECL/PHP extension already loaded (base image now bundles it after upgrade)",
     r"Extension '[^']+' already loaded"),

    ("pip: wheel build fails against upgraded base interpreter (broad — verify)",
     r"Failed to build installable wheels|error: failed-wheel-build-for-install|"
     r"Failed building wheel for"),

    ("npm: unpinned dependency pulled a newer, broken version (broad — verify)",
     r"npm ERR!.*postinstall|postinstall script"),
]


# --------------------------------------------------------------------------- #
# NON-TIME-INDUCED signals  (mined from the non-time-induced sample; edit me)
# Ordered specific -> generic so the most informative reason is reported.
# --------------------------------------------------------------------------- #
NON_TIME_INDUCED_RULES = [
    ("Missing file / build context (COPY or ADD found nothing)",
     r"COPY failed: file not found in build context|"
     r"COPY failed: no source files were specified|"
     r"ADD failed:|"
     r"stat .*: file does not exist|"
     r"failed to compute cache key.*not found|"
     r"failed to calculate checksum of ref.*not found|"
     r"\"/[^\"]*\": not found"),

    ("Blank base-image ARG (build ARG never passed)",
     r"base name \([^)]*\) should not be blank"),

    ("Invalid image reference format",
     r"invalid reference format"),

    ("Invalid platform / OS specifier",
     r"is an invalid OS component|failed to parse platform|"
     r"OSAndVersion specifier"),

    ("BuildKit-only feature used with legacy builder (--mount/--chmod/--link)",
     r"the --(mount|chmod|link) option requires BuildKit|requires BuildKit"),

    ("Permission error",
     r"[Pp]ermission denied|Failure writing output"),

    ("Architecture / manifest mismatch",
     r"no matching manifest for linux|no match for platform|"
     r"exec format error|cannot execute binary file"),

    ("Private/self-hosted registry unreachable",
     r"failed to resolve reference.*(no such host|dial tcp|connection refused)|"
     r"image-registry\.openshift-image-registry"),

    ("Dockerfile syntax / parse error",
     r"dockerfile parse error|unknown instruction"),

    ("Build export / daemon storage failure",
     r"failed to export layer|mount callback failed|lease does not exist|"
     r"CreateDiff"),

    ("RHEL subscription / entitlement unavailable",
     r"not registered with an entitlement server|Unable to read consumer identity",
     r"KEYEXPIRED|EXPKEYSIG|following signatures were invalid|NO_PUBKEY|"),

    # Generic catch-all: some RUN/build step exited non-zero with no more
    # specific signature above. Keep this LAST.
    ("Build step / command failed (non-zero exit)",
     r"returned a non-zero code: \d+|"
     r"did not complete successfully: exit code: \d+|"
     r"process \".*\" did not complete"),
]


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

def _compile(rules):
    compiled = []
    for entry in rules:
        reason, *pattern_parts = entry        # first item = reason, the rest = pattern fragment(s)
        pattern = "".join(pattern_parts)      # join fragments whether or not commas slipped in
        compiled.append((reason, re.compile(pattern, re.IGNORECASE | re.DOTALL)))
    return compiled


_TIME = _compile(TIME_INDUCED_RULES)
_NONTIME = _compile(NON_TIME_INDUCED_RULES)

_ANSI_REAL = re.compile(r"\x1b\[[0-9;]*m")       # real ANSI escape sequences
_ANSI_LITERAL = re.compile(r"\\x1b\[[0-9;]*m")   # literally-escaped ones in text
_WS = re.compile(r"\s+")


def clean(text):
    """Strip ANSI colour codes (real and literally-escaped)."""
    if text is None:
        return ""
    text = _ANSI_REAL.sub("", text)
    text = _ANSI_LITERAL.sub("", text)
    return text


def _snippet(match):
    snip = _WS.sub(" ", match.group(0).strip())
    return snip[:160] + "…" if len(snip) > 160 else snip


def classify(text):
    """Return (classification, reason, evidence_snippet)."""
    for reason, rx in _TIME:
        m = rx.search(text)
        if m:
            return "Time-Induced", reason, _snippet(m)
    for reason, rx in _NONTIME:
        m = rx.search(text)
        if m:
            return "Non-Time-Induced", reason, _snippet(m)
    return "Uncategorized", "", ""


def label_dataframe(df):
    """Add classification columns to df in place and return it."""
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

    verdict = pd.Series("", index=df.index, dtype=object)
    reason = pd.Series("", index=df.index, dtype=object)
    evidence = pd.Series("", index=df.index, dtype=object)

    for _, idx in groups.groupby(groups).groups.items():
        parent = idx[0]
        parts = []
        for c in log_cols:
            frags = [clean(str(v)) for v in df.loc[idx, c] if pd.notna(v)]
            parts.append("\n".join(frags))
        combined = "\n".join(parts)

        v, r, e = classify(combined)
        verdict.at[parent] = v      # label only the parent row
        reason.at[parent] = r
        evidence.at[parent] = e

    df["Classification"] = verdict
    df["Classification Reason"] = reason
    df["Classification Evidence"] = evidence
    return df, int(is_new.sum()), len(df) - int(is_new.sum())


def print_summary(df, n_records, n_cont):
    labeled = df[df["Classification"] != ""]
    print(f"Logical records: {n_records}  |  continuation rows merged: {n_cont}")
    print()
    print("Classification counts (per record):")
    print(labeled["Classification"].value_counts().to_string())

    for bucket in ("Time-Induced", "Uncategorized"):
        rows = labeled[labeled["Classification"] == bucket]
        if len(rows):
            print(f"\n--- {len(rows)} record(s): {bucket} ---")
            for _, r in rows.iterrows():
                name = r[ID_COLUMN]
                reason = r["Classification Reason"] or "(no rule matched)"
                print(f"  • {name}  ->  {reason}")
                if r["Classification Evidence"]:
                    print(f"      evidence: {r['Classification Evidence']}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Classify Docker build failures as time-induced or not."
    )
    parser.add_argument("input", nargs="?", default=DEFAULT_INPUT,
                        help=f"input CSV (default: {DEFAULT_INPUT})")
    parser.add_argument("output", nargs="?", default=None,
                        help="output CSV (default: <input>_labeled.csv)")
    args = parser.parse_args(argv)

    if not os.path.exists(args.input):
        parser.error(f"input file not found: {args.input}")

    output = args.output
    if output is None:
        base, ext = os.path.splitext(args.input)
        output = f"{base}_labeled{ext or '.csv'}"

    df = pd.read_csv(args.input)
    print(f"Loaded '{args.input}': {len(df)} rows, {len(df.columns)} columns.")

    df, n_records, n_cont = label_dataframe(df)
    print_summary(df, n_records, n_cont)

    df.to_csv(output, index=False)
    print(f"\nSaved labeled CSV to '{output}'")


if __name__ == "__main__":
    sys.exit(main())