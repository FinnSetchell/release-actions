#!/usr/bin/env python3
"""CI entry point: verify the just-published tag and alert the Discord bot.

Reads gradle.properties from the current directory (the mod checked out at the
release tag), derives the published version + alpha flag from the tag, runs the
verification library, POSTs the result to the bot's /verify-alert on a failure,
and exits non-zero on a real miss so the workflow goes red.

stdlib-only. Configured entirely by environment variables:
  TAG             (required)  release tag, e.g. 3.0.0-1.21.x
  WORKER_URL      (required)  bot base URL
  WORKER_API_KEY  (required)  X-API-Key for the bot
  MOD_NAME        optional    display name (falls back to modName in props)
  FAIL_ON         optional    'fail' (default) or 'pending'
  TAG_PATTERN     optional    override the default tag regex
  DRY_RUN         optional    if set, never POST (local testing)

Exit codes: 0 = pass/pending/transient-error (no red), 1 = real failure, 3 = config error.
A CurseForge transport error (e.g. the website API bot-challenging a runner) is
treated as 'error' -> alert but do NOT fail the run, so flaky CF access can't wall
of false reds. Real misses/rejections still fail.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verifier  # noqa: E402

# Loaders we know how to detect via settings.gradle(.kts) `include(...)` heuristic.
KNOWN_LOADERS = ("fabric", "forge", "neoforge", "quilt")


def read_props(path="gradle.properties"):
    props = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
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
    extra = props.get("publishExtraMcVersions", "")
    mc += [x.strip() for x in extra.split(",") if x.strip()]
    return list(dict.fromkeys(mc))  # dedupe, preserve order


def expected_platforms(props):
    """Which loaders should have been published for this tag.

    Priority: explicit `publishLoaders=fabric,neoforge` in gradle.properties
    (authoritative, set by the mod repo). Otherwise fall back to a heuristic
    that reads settings.gradle(.kts) for `include('<loader>')` modules, since
    that's checked out at the same tag being verified. Only if neither signal
    is available do we fall back to the old fabric+forge+neoforge default -
    and that fallback should be rare, since it's exactly what caused false
    "missing forge" failures on fabric+neoforge-only branches.
    """
    explicit = props.get("publishLoaders", "")
    if explicit.strip():
        return [x.strip() for x in explicit.split(",") if x.strip()]

    text = ""
    for fname in ("settings.gradle", "settings.gradle.kts"):
        try:
            with open(fname, encoding="utf-8") as fh:
                text += fh.read()
        except FileNotFoundError:
            continue

    found = [loader for loader in KNOWN_LOADERS
             if re.search(rf"include\(\s*['\"]{loader}['\"]\s*\)", text)]
    if found:
        return found

    return ["fabric", "forge", "neoforge"]


def post_alert(worker_url, api_key, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        worker_url.rstrip("/") + "/verify-alert",
        data=data,
        # urllib's default User-Agent ("Python-urllib/x.y") gets a 403 from
        # Cloudflare in front of the worker; publish.yml's curl POST to /published
        # uses the same X-API-Key header and succeeds, the difference being curl's
        # User-Agent. Send a normal-looking one so this request isn't edge-blocked.
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "User-Agent": verifier.USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def main():
    try:
        tag = os.environ["TAG"]
    except KeyError:
        print("::error::TAG env var is required")
        return 3
    worker_url = os.environ.get("WORKER_URL", "")
    api_key = os.environ.get("WORKER_API_KEY", "")
    pattern = os.environ.get("TAG_PATTERN", verifier.DEFAULT_TAG_PATTERN)
    dry_run = bool(os.environ.get("DRY_RUN"))

    parsed = verifier.parse_tag(tag, pattern)
    if not parsed:
        print(f"::error::tag '{tag}' does not match pattern '{pattern}'")
        return 3

    try:
        props = read_props()
    except FileNotFoundError:
        print("::error::gradle.properties not found in working directory")
        return 3

    # The tag's own <version>-<mc> vs <mc>-<version> ordering is ambiguous across
    # the mod family (some repos tag "3.0.1-1.21.1", others "26.1.2-3.0.3" -mc
    # first) - when both halves are bare X.Y.Z numbers, DEFAULT_TAG_PATTERN's
    # regex has no way to tell them apart and can grab the MC label as "version".
    # gradle.properties at the checked-out tag is unambiguous and authoritative
    # (it's exactly what got built and published), so prefer modVersion/releaseType
    # from there and only fall back to the tag-parsed value if props lack them.
    mod_version = props.get("modVersion", "").strip() or parsed["version"]
    is_alpha = props.get("releaseType", "").strip().lower() == "alpha" \
        or "-alpha." in mod_version or parsed["is_alpha"]

    modrinth_id = props.get("modrinthProjectId")
    curseforge_id = props.get("curseforgeProjectId")
    if not modrinth_id or not curseforge_id:
        print("::error::gradle.properties missing modrinthProjectId / curseforgeProjectId")
        return 3

    mc = expected_mc(props)
    if not mc:
        print("::error::no publishMcStart / publishMcEnd / publishExtraMcVersions in gradle.properties")
        return 3

    result = verifier.verify_release(
        mod_key=props.get("modId", modrinth_id),
        modrinth_id=modrinth_id,
        curseforge_id=curseforge_id,
        mod_version=mod_version,
        platforms=expected_platforms(props),
        expected_mc=mc,
        is_alpha=is_alpha,
    )

    # Enrich the payload so the bot can render a rich embed.
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_url = ""
    if repo and os.environ.get("GITHUB_RUN_ID"):
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        run_url = f"{server}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    result.update({
        "modName": os.environ.get("MOD_NAME") or props.get("modName") or props.get("modId"),
        "tag": tag,
        "repo": repo,
        "run_url": run_url,
    })

    verifier._print_human(result)
    verdict = result["verdict"]

    if verdict in ("fail", "error"):
        if dry_run or not worker_url or not api_key:
            print(f"[dry-run] would POST /verify-alert (verdict={verdict})")
        else:
            try:
                status = post_alert(worker_url, api_key, result)
                print(f"posted /verify-alert -> HTTP {status}")
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
                print(f"::warning::failed to post verify-alert: {e}")

    # Only a real miss/rejection turns the run red. Transient 'error' alerts but
    # does not fail; 'pending' is silent (v1).
    fail_on = os.environ.get("FAIL_ON", "fail")
    if verdict == "fail":
        return 1
    if verdict == "pending" and fail_on == "pending":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
