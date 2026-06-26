#!/usr/bin/env python3
"""
Publish verification library -stdlib-only (runs in barebones CI images).

Confirms a release's jar actually landed on Modrinth + CurseForge for every
expected (loader, minecraft-version) and, on CurseForge, that the file's
moderation `fileStatus` is acceptable (catches async "Rejected" moderation).

Importable as a module (`verify_release(...)`) and runnable as a CLI.

No third-party dependencies. JSON in, structured result out.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

MODRINTH_API = "https://api.modrinth.com/v2"
USER_AGENT = "moogsmods-publish-verify/1.0 (+https://github.com/FinnSetchell/release-actions)"

# Default release-tag shape across the mod family: <version>[-alpha.N]-<mc-label>,
# e.g. 3.0.0-1.21.x or 3.0.0-alpha.2-1.21.x. Mirrors registry.json defaults.tag_pattern.
DEFAULT_TAG_PATTERN = r"^v?(?P<version>\d+\.\d+\.\d+(?:-alpha\.\d+)?)-(?P<mc>.+)$"

# CurseForge's website-internal files API needs NO API key — unlike the gated
# Eternal API (api.curseforge.com), whose x-api-key is approval-only. It returns
# the same fields we need: gameVersions (loaders + MC versions), releaseType, and
# the moderation `status` (the FileStatus enum). It serves only public files, so a
# moderation-rejected file is simply absent (we flag it 'missing') rather than
# shown as 'Rejected'. Either way the failure is caught.
CURSEFORGE_FILES_API = "https://www.curseforge.com/api/v1/mods"
# Browser-like UA so the website endpoint doesn't bot-challenge CI runners.
CF_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# CurseForge Core API FileStatus enum -> (name, classification).
# classification: "ok" (published/approved), "pending" (in moderation),
# "fail" (rejected/broken). Tweak here in one place if CF changes the enum.
# Ref: https://docs.curseforge.com/rest-api/#tocS_FileStatus
CF_FILE_STATUS = {
    1:  ("Processing",          "pending"),
    2:  ("ChangesRequired",     "fail"),
    3:  ("UnderReview",         "pending"),
    4:  ("Approved",            "ok"),
    5:  ("Rejected",            "fail"),
    6:  ("MalwareDetected",     "fail"),
    7:  ("Deleted",             "fail"),
    8:  ("Archived",            "ok"),       # previously approved, retired -not a failure
    9:  ("Testing",             "pending"),
    10: ("Released",            "ok"),
    11: ("ReadyForReview",      "pending"),
    12: ("Deprecated",          "ok"),       # previously approved, superseded -not a failure
    13: ("Baking",              "pending"),
    14: ("AwaitingPublishing",  "pending"),
    15: ("FailedPublishing",    "fail"),
}

# CurseForge releaseType enum.
CF_RELEASE_TYPE = {"release": 1, "beta": 2, "alpha": 3}

# Loader name as it appears in CurseForge `gameVersions` (capitalized).
CF_LOADER_NAME = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}


# ─────────────────────────── HTTP ───────────────────────────

def _get_json(url, headers=None, retries=3):
    """GET url -> (parsed_json, error_str). Retries transient 429/5xx with backoff."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp), None
        except urllib.error.HTTPError as e:
            body = e.read(300).decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                last = f"HTTP {e.code}: {body}"
                continue
            return None, f"HTTP {e.code}: {body}".strip()
        except Exception as e:  # noqa: BLE001 - surface any transport error as a string
            last = f"{type(e).__name__}: {e}"
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None, last
    return None, last


# ─────────────────────────── Modrinth ───────────────────────────

def check_modrinth(project_id, mod_version, platforms, expected_mc, is_alpha):
    """Find the Modrinth version matching version_number + version_type and
    assert it covers every expected loader and minecraft version."""
    want_type = "alpha" if is_alpha else "release"
    result = {
        "found": False, "status": "missing", "ok": False,
        "missing_loaders": [], "missing_mc": [], "version_type": None,
        "url": f"https://modrinth.com/mod/{project_id}/version/{mod_version}",
        "error": None,
    }
    versions, err = _get_json(f"{MODRINTH_API}/project/{project_id}/version")
    if err:
        result["status"] = "error"
        result["error"] = err
        return result

    # The SAME version_number is published once per MC line (e.g. "3.0.0" exists
    # as a 1.20 version and a 1.21 version, each with its own game_versions). So
    # evaluate every same-numbered version and accept the one that best covers the
    # expected loaders + MC; picking the first match would check the wrong line.
    same_number = [v for v in versions if v.get("version_number") == mod_version]
    if not same_number:
        return result  # genuinely absent

    typed = [v for v in same_number if v.get("version_type") == want_type]
    if not typed:
        # Exists but published under a different type than the tag implies.
        result["status"] = "wrong_type"
        result["version_type"] = same_number[0].get("version_type")
        result["error"] = (
            f"version {mod_version} exists but type is "
            f"'{same_number[0].get('version_type')}', expected '{want_type}'"
        )
        return result

    best_missing = None
    for v in typed:
        loaders = set(v.get("loaders", []))
        game_versions = set(v.get("game_versions", []))
        missing_loaders = [p for p in platforms if p not in loaders]
        missing_mc = [mc for mc in expected_mc if mc not in game_versions]
        if best_missing is None or (len(missing_loaders) + len(missing_mc)) < \
                (len(best_missing[0]) + len(best_missing[1])):
            best_missing = (missing_loaders, missing_mc)
        if not missing_loaders and not missing_mc:
            break

    result["found"] = True
    result["version_type"] = want_type
    result["missing_loaders"], result["missing_mc"] = best_missing
    if not best_missing[0] and not best_missing[1]:
        result["status"] = "ok"
        result["ok"] = True
    else:
        result["status"] = "incomplete"
    return result


# ─────────────────────────── CurseForge ───────────────────────────

def _cf_all_files(mod_id):
    """Paginate the website files API (no key). Returns (files_list, error_str)."""
    files = []
    page_index = 0
    page_size = 50
    while True:
        # removeAlphas=false is REQUIRED — the website API hides alpha files by
        # default, which would false-positive 'missing' on every alpha release.
        url = (f"{CURSEFORGE_FILES_API}/{mod_id}/files"
               f"?pageSize={page_size}&pageIndex={page_index}&removeAlphas=false")
        data, err = _get_json(url, headers={"User-Agent": CF_BROWSER_UA})
        if err:
            return None, err
        page = data.get("data", [])
        files.extend(page)
        total = data.get("pagination", {}).get("totalCount", len(files))
        if not page or len(files) >= total:
            break
        page_index += 1
        if page_index > 200:  # hard stop, defensive
            break
    return files, None


def check_curseforge(mod_id, mod_version, platforms, expected_mc, is_alpha):
    """Find CF files for this version and classify each expected (loader, mc)
    cell by the file's moderation status (the FileStatus enum, field `status`)."""
    result = {
        "found": False, "status": "missing", "ok": False,
        "missing": [], "pending": [], "failed": [], "cells": [],
        "url": f"https://www.curseforge.com/projects/{mod_id}/files",
        "error": None,
    }
    want_rt = CF_RELEASE_TYPE["alpha" if is_alpha else "release"]
    files, err = _cf_all_files(mod_id)
    if err:
        result["status"] = "error"
        result["error"] = err
        return result

    # Candidate files: right releaseType + version string in the name.
    candidates = []
    for f in files:
        if f.get("releaseType") != want_rt:
            continue
        name = (f.get("fileName", "") + " " + f.get("displayName", ""))
        if mod_version not in name:
            continue
        candidates.append(f)

    # For each expected (loader, mc), find a candidate whose gameVersions cover both.
    worst = "ok"
    for platform in platforms:
        loader_name = CF_LOADER_NAME.get(platform, platform.capitalize())
        for mc in expected_mc:
            hit = None
            for f in candidates:
                gv = f.get("gameVersions", [])
                if loader_name in gv and mc in gv:
                    hit = f
                    break
            if hit is None:
                result["missing"].append(f"{platform}/{mc}")
                worst = "fail"
                result["cells"].append({"platform": platform, "mc": mc, "status": "missing"})
                continue
            code = hit.get("status")
            name, cls = CF_FILE_STATUS.get(code, (f"Unknown({code})", "fail"))
            result["found"] = True
            cell = {"platform": platform, "mc": mc, "status": cls,
                    "fileStatus": name, "fileId": hit.get("id"),
                    "fileName": hit.get("fileName")}
            result["cells"].append(cell)
            if cls == "fail":
                result["failed"].append(f"{platform}/{mc} [{name}]")
                worst = "fail"
            elif cls == "pending":
                result["pending"].append(f"{platform}/{mc} [{name}]")
                if worst == "ok":
                    worst = "pending"

    if not candidates:
        result["status"] = "missing"
    else:
        result["status"] = worst
        result["ok"] = (worst == "ok")
    return result


# ─────────────────────────── Top-level ───────────────────────────

def verify_release(mod_key, modrinth_id, curseforge_id, mod_version,
                   platforms, expected_mc, is_alpha):
    """Run both platform checks and produce an overall verdict.

    verdict: 'pass' | 'fail' | 'pending' | 'error'
    (v1 alerting acts on 'fail'; 'pending' is reported but silent.)
    """
    mr = check_modrinth(modrinth_id, mod_version, platforms, expected_mc, is_alpha)
    cf = check_curseforge(curseforge_id, mod_version, platforms, expected_mc, is_alpha)

    statuses = {mr["status"], cf["status"]}
    if "missing" in statuses or "incomplete" in statuses \
            or "wrong_type" in statuses or cf["failed"]:
        # missing/incomplete/wrong_type/failed are all actionable failures
        verdict = "fail"
    elif "pending" in statuses:
        verdict = "pending"
    elif mr["ok"] and cf["ok"]:
        verdict = "pass"
    else:
        verdict = "fail"

    # An error (transient API failure) is distinct from a real miss.
    if "error" in statuses and not (mr["status"] in ("missing", "incomplete", "wrong_type") or cf["missing"] or cf["failed"]):
        verdict = "error"

    return {
        "mod": mod_key,
        "version": mod_version,
        "is_alpha": is_alpha,
        "expected_platforms": platforms,
        "expected_mc": expected_mc,
        "modrinth": mr,
        "curseforge": cf,
        "verdict": verdict,
    }


# ─────────────────────────── Registry / CLI ───────────────────────────

def load_registry(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def parse_tag(tag, pattern):
    """Parse a release tag with the registry's tag_pattern.
    Returns {'version', 'mc', 'is_alpha'} or None if it doesn't match.
    Alpha is derived from the tag's version (-alpha.N), which is the reliable
    source of truth (gradle.properties alpha flags are inconsistent across mods)."""
    m = re.match(pattern, tag)
    if not m:
        return None
    version = m.group("version")
    return {"version": version, "mc": m.group("mc"), "is_alpha": "-alpha." in version}


def registry_entry(registry, mod_key):
    for m in registry.get("mods", []):
        if m["key"] == mod_key:
            return m
    raise KeyError(f"mod '{mod_key}' not in registry")


def _parse_csv(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def main(argv=None):
    p = argparse.ArgumentParser(description="Verify a Minecraft mod release landed on Modrinth + CurseForge.")
    p.add_argument("--registry", help="path to registry.json (when using --mod)")
    p.add_argument("--mod", help="registry mod key (e.g. mns)")
    p.add_argument("--modrinth-id", help="Modrinth project id (overrides registry)")
    p.add_argument("--curseforge-id", help="CurseForge numeric project id (overrides registry)")
    p.add_argument("--version", required=True, help="mod version, e.g. 3.0.0")
    p.add_argument("--mc", required=True, help="comma-separated expected MC versions (e.g. 1.21,1.21.11,26.2)")
    p.add_argument("--platforms", default="fabric,forge,neoforge", help="comma-separated loaders")
    p.add_argument("--alpha", action="store_true", help="treat as an alpha release")
    p.add_argument("--json", action="store_true", help="emit full JSON result to stdout")
    args = p.parse_args(argv)

    modrinth_id = args.modrinth_id
    curseforge_id = args.curseforge_id
    platforms = _parse_csv(args.platforms)

    if args.mod:
        if not args.registry:
            p.error("--mod requires --registry")
        reg = load_registry(args.registry)
        entry = registry_entry(reg, args.mod)
        modrinth_id = modrinth_id or entry["modrinth"]["id"]
        curseforge_id = curseforge_id or entry["curseforge"]["id"]
        platforms = entry.get("platforms") or reg.get("defaults", {}).get("platforms") or platforms

    if not modrinth_id or not curseforge_id:
        p.error("need --mod (with --registry) or both --modrinth-id and --curseforge-id")

    result = verify_release(
        mod_key=args.mod or modrinth_id,
        modrinth_id=modrinth_id,
        curseforge_id=curseforge_id,
        mod_version=args.version,
        platforms=platforms,
        expected_mc=_parse_csv(args.mc),
        is_alpha=args.alpha,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)

    return {"pass": 0, "pending": 0, "fail": 1, "error": 3}[result["verdict"]]


def _print_human(r):
    tag = {"pass": "[PASS]", "pending": "[PEND]", "fail": "[FAIL]", "error": "[ERR ]"}[r["verdict"]]
    print(f"{tag} {r['mod']} {r['version']} ({'alpha' if r['is_alpha'] else 'release'}) -> {r['verdict'].upper()}")
    mr = r["modrinth"]
    print(f"  Modrinth: {mr['status']}"
          + (f" -missing loaders {mr['missing_loaders']}" if mr["missing_loaders"] else "")
          + (f" -missing mc {mr['missing_mc']}" if mr["missing_mc"] else "")
          + (f" -{mr['error']}" if mr["error"] else ""))
    cf = r["curseforge"]
    extra = ""
    if cf["missing"]:
        extra += f" -missing {cf['missing']}"
    if cf["failed"]:
        extra += f" -FAILED {cf['failed']}"
    if cf["pending"]:
        extra += f" -pending {cf['pending']}"
    if cf["error"]:
        extra += f" -{cf['error']}"
    print(f"  CurseForge: {cf['status']}{extra}")


if __name__ == "__main__":
    sys.exit(main())
