#!/usr/bin/env python3
"""Close open `package-update` issues once their package has been built.

Usage:
    close-update-issues.py RESULTS_DIR [--repo OWNER/REPO] [--token TOKEN]
                                      [--dry-run]

Reads per-arch build result files (`build-results-<ARCH>/results.tsv`,
lines `name<TAB>version<TAB>status`) produced by `build-packages.sh`,
lists open issues carrying the `package-update` label, and closes each
one whose package was built successfully (an `ok` result with no `fail`
on any arch) at exactly the upstream version requested in the issue body.

The package and requested version come from the hidden markers
`<!-- package-update:NAME -->` and `<!-- upstream-version:VERSION -->`,
falling back to the `**Package:**` and `**Upstream version:**` headings.

At most one open issue per package is expected (the update report is
deduplicated), but if duplicates exist all matching ones are closed.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

PKG_RE = re.compile(r"<!-- package-update:(\S+?) -->")
VER_RE = re.compile(r"<!-- upstream-version:(\S+?) -->")
PKG_BODY_RE = re.compile(r"\*\*Package:\*\*\s*`([^`]+)`")
VER_BODY_RE = re.compile(r"\*\*Upstream version:\*\*\s*`([^`]+)`")
API = "https://api.github.com/repos/{}"
LABEL = "package-update"


def collect_results(results_dir: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (ok_versions, fail_versions): map pkg_lower -> set of versions."""
    ok: dict[str, set[str]] = {}
    fail: dict[str, set[str]] = {}
    if not os.path.isdir(results_dir):
        return ok, fail
    for d in sorted(os.listdir(results_dir)):
        if not d.startswith("build-results-"):
            continue
        tsv = os.path.join(results_dir, d, "results.tsv")
        if not os.path.isfile(tsv):
            continue
        with open(tsv, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                pkg, version, status = parts
                if status not in ("ok", "fail"):
                    continue
                bucket = ok if status == "ok" else fail
                bucket.setdefault(pkg.lower(), set()).add(version)
    return ok, fail


def api(repo: str, token: str, method: str, path: str, data=None):
    """GitHub API call; returns parsed JSON or None on HTTP error."""
    url = API.format(repo) + path
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, method=method, data=payload)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:200]
        except Exception:
            pass
        sys.stderr.write(f"warning: HTTP {e.code} on {url}: {detail}\n")
        return None


def list_open_update_issues(repo: str, token: str) -> list[dict]:
    issues: list[dict] = []
    page = 1
    while True:
        data = api(
            repo, token, "GET",
            f"/issues?state=open&labels={LABEL}&per_page=100&page={page}",
        )
        if not isinstance(data, list) or not data:
            break
        issues.extend(data)
        if len(data) < 100:
            break
        page += 1
    return issues


def extract_pkg_version(body: str) -> tuple[str | None, str | None]:
    pkg = PKG_RE.search(body) or PKG_BODY_RE.search(body)
    ver = VER_RE.search(body) or VER_BODY_RE.search(body)
    if not pkg or not ver:
        return None, None
    return pkg.group(1).strip(), ver.group(1).strip()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Close open package-update issues once their package is built.")
    ap.add_argument("results_dir", help="directory with build-results-*/results.tsv")
    ap.add_argument("--repo", required=True, help="OWNER/REPO slug")
    ap.add_argument("--token", required=True, help="GitHub token with issues:write")
    ap.add_argument("--dry-run", action="store_true", help="list matches without closing")
    args = ap.parse_args()

    ok, fail = collect_results(args.results_dir)
    issues = list_open_update_issues(args.repo, args.token)

    closed = kept = unparsed = 0
    for issue in issues:
        number = issue.get("number", "?")
        title = (issue.get("title") or "").strip()
        pkg, ver = extract_pkg_version(issue.get("body") or "")
        if not pkg or not ver:
            unparsed += 1
            print(f"  #{number}: {title}: no package/-version markers, kept open")
            continue
        key = pkg.lower()
        built = ver in ok.get(key, set())
        if built and ver not in fail.get(key, set()):
            closed += 1
            print(f"  #{number}: {pkg} built at {ver} -> closing")
            if args.dry_run:
                continue
            api(args.repo, args.token, "PATCH",
                f"/issues/{number}", {"state": "closed"})
            comment = f"Resolved: **{pkg}** built at `{ver}`. <!-- resolved:v{ver} -->"
            for _ in range(3):
                if api(args.repo, args.token, "POST",
                       f"/issues/{number}/comments", {"body": comment}) is not None:
                    break
        else:
            kept += 1
            print(f"  #{number}: {pkg}@{ver} not built (or failed), kept open")

    if args.dry_run:
        print(f"Dry run: {closed} to close, {kept} kept open, {unparsed} unparsed")
    else:
        print(f"Closed {closed}, kept open {kept}, unparsed {unparsed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())