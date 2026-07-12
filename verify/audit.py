#!/usr/bin/env python3
"""Scheduled publish audit: for every mod in the registry, verify the LATEST
release per active MC line on Modrinth + CurseForge, and POST the results to the
Discord bot's /audit-alert. The bot filters to failures and routes alpha vs
release. Scoped to current releases only — strays and history are ignored by
design (the latest per line is the highest semver, so low/old tags never match).
Its unique job over the release-time verify: catch CurseForge moderation
rejecting a fresh release hours after a successful publish.

Reads each tag's gradle.properties via the GitHub API (no cloning), so it works
with zero local checkout — give it a registry + a token and it does the rest.

stdlib-only. Configured by environment variables:
  REGISTRY        path to registry.json (default ./registry.json)
  WORKER_URL      bot base URL
  WORKER_API_KEY  X-API-Key for the bot
  GITHUB_TOKEN    token for GitHub API reads (public repos: any token works)
  DRY_RUN         if set, print results, never POST
  HEARTBEAT       if set, ask the bot to post a clean summary even with no issues

Exit 0 normally (the alert IS the signal); exit 1 only if it couldn't post.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verifier  # noqa: E402

GITHUB_API = "https://api.github.com"


def _gh(path, token, raw=False):
    headers = {
        "User-Agent": verifier.USER_AGENT,
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(GITHUB_API + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    return body if raw else json.loads(body)


def list_all_release_tags(repo, token):
    """All tag names from the repo's GitHub Releases (paginated)."""
    tags = []
    page = 1
    while page <= 10:  # 1000 releases cap — far beyond any mod
        batch = _gh(f"/repos/{repo}/releases?per_page=100&page={page}", token)
        if not batch:
            break
        tags += [r["tag_name"] for r in batch if r.get("tag_name")]
        if len(batch) < 100:
            break
        page += 1
    return tags


def _version_key(version):
    """Sortable key for a mod version. A release outranks any prerelease of the
    same X.Y.Z, so semver-max naturally ignores stray low versions and alphas."""
    base, _, pre = version.partition("-")
    parts = (base.split(".") + ["0", "0", "0"])[:3]
    nums = tuple(int(p) if p.isdigit() else 0 for p in parts)
    if pre:  # e.g. alpha.2 -> ranks below the release of the same base
        alpha_n = next((int(t) for t in pre.split(".") if t.isdigit()), 0)
        return nums + (0, alpha_n)
    return nums + (1, 0)


def _mc_line(mc_label):
    """Collapse a tag's MC label to its major.minor line (1.21.x / 1.21.11 -> 1.21)."""
    m = re.match(r"(\d+)\.(\d+)", mc_label)
    return f"{m.group(1)}.{m.group(2)}" if m else mc_label


def select_latest_per_line(tags, pattern):
    """Pick the single highest-semver release per MC line. Returns [(tag, parsed)]."""
    best = {}
    for tag in tags:
        parsed = verifier.parse_tag(tag, pattern)
        if not parsed:
            continue
        line = _mc_line(parsed["mc"])
        key = _version_key(parsed["version"])
        if line not in best or key > best[line][0]:
            best[line] = (key, tag, parsed)
    return [(tag, parsed) for _, tag, parsed in best.values()]


def read_props_at(repo, tag, token):
    raw = _gh(f"/repos/{repo}/contents/gradle.properties?ref={urllib.parse.quote(tag)}",
              token, raw=True)
    props = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        props[k.strip()] = v.strip()
    return props


def expected_mc(props):
    mc = []
    for k in ("publishMcStart", "publishMcEnd"):
        v = props.get(k, "").strip()
        if v:
            mc.append(v)
    mc += [x.strip() for x in props.get("publishExtraMcVersions", "").split(",") if x.strip()]
    return list(dict.fromkeys(mc))


def _error_result(entry, tag, version, is_alpha, where, err):
    return {
        "mod": entry["key"], "modName": entry.get("name"), "tag": tag,
        "version": version, "is_alpha": is_alpha, "verdict": "error",
        "modrinth": {"status": "error", "error": f"{where}: {err}"},
        "curseforge": {"status": "error", "missing": [], "failed": [], "pending": []},
    }


def audit_mod(entry, defaults, token):
    """Verify only the latest release per active MC line — the current versions
    the user cares about. Strays and history are ignored by design."""
    repo = entry["repo"]
    pattern = entry.get("tag_pattern") or defaults.get("tag_pattern") or verifier.DEFAULT_TAG_PATTERN
    platforms = entry.get("platforms") or defaults.get("platforms") or ["fabric", "forge", "neoforge"]
    results, skipped = [], 0

    try:
        tags = list_all_release_tags(repo, token)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return [_error_result(entry, None, None, False, "github releases", e)], 0

    for tag, parsed in select_latest_per_line(tags, pattern):
        try:
            props = read_props_at(repo, tag, token)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            results.append(_error_result(entry, tag, parsed["version"], parsed["is_alpha"],
                                         "gradle.properties", e))
            continue
        mc = expected_mc(props)
        if not mc:
            skipped += 1  # too old to carry publish metadata — can't verify
            continue
        res = verifier.verify_release(
            mod_key=entry["key"],
            modrinth_id=props.get("modrinthProjectId") or entry["modrinth"]["id"],
            curseforge_id=props.get("curseforgeProjectId") or entry["curseforge"]["id"],
            mod_version=parsed["version"],
            platforms=platforms,
            expected_mc=mc,
            is_alpha=parsed["is_alpha"],
        )
        res.update({"modName": entry.get("name"), "tag": tag, "repo": repo})
        results.append(res)
        time.sleep(0.1)  # be polite to the APIs
    return results, skipped


ISSUE_VERDICTS = ("fail", "error", "unconfirmed")


def post_audit(worker_url, api_key, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        worker_url.rstrip("/") + "/audit-alert", data=data,
        headers={"Content-Type": "application/json", "X-API-Key": api_key}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status


def _run_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    rid = os.environ.get("GITHUB_RUN_ID", "")
    if repo and rid:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server}/{repo}/actions/runs/{rid}"
    return ""


def main():
    reg_path = os.environ.get("REGISTRY", "registry.json")
    worker_url = os.environ.get("WORKER_URL", "")
    api_key = os.environ.get("WORKER_API_KEY", "")
    token = os.environ.get("GITHUB_TOKEN")
    dry_run = bool(os.environ.get("DRY_RUN"))
    heartbeat = bool(os.environ.get("HEARTBEAT"))

    reg = verifier.load_registry(reg_path)
    defaults = reg.get("defaults", {})

    all_results, total_skipped = [], 0
    for entry in reg["mods"]:
        res, skipped = audit_mod(entry, defaults, token)
        all_results.extend(res)
        total_skipped += skipped
        issues = sum(1 for r in res if r["verdict"] in ISSUE_VERDICTS)
        print(f"  {entry['key']:14} {len(res):3} lines  {skipped:2} skipped  {issues} issue(s)")

    failures = [r for r in all_results if r["verdict"] in ISSUE_VERDICTS]
    print(f"\nAudited the latest release per MC line across {len(reg['mods'])} mods "
          f"({len(all_results)} checked, {total_skipped} skipped) -> {len(failures)} issue(s)")
    for r in failures:
        print(f"  [{r['verdict']}] {r['mod']} {r.get('version')} ({r.get('tag')})")

    if dry_run or not worker_url or not api_key:
        print("[dry-run] not posting to /audit-alert")
        return 0
    if not failures and not heartbeat:
        print("clean — nothing to post")
        return 0

    payload = {"results": all_results, "run_url": _run_url(), "heartbeat": heartbeat}
    try:
        status = post_audit(worker_url, api_key, payload)
        print(f"posted /audit-alert -> HTTP {status}")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"::error::failed to post audit-alert: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
