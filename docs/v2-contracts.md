# v2 Interface Contracts

Authoritative for all v2 components. If an implementation needs to deviate, the deviation is
made HERE first. Companion docs: `PUBLISH_SYSTEM_AUDIT_FINDINGS.md`, `PUBLISH_SYSTEM_TARGET_DESIGN.md`.

## File layout (release-actions)

v2 lives in NEW files so main can hold both lines without merge friction; the `@v2` tag = main.
- `.github/workflows/release-v2.yml` — tag push → plan → build → review card
- `.github/workflows/publish-v2.yml` — dispatch → reconcile-publish → GH release → verify → notify
- `.github/workflows/verify-v2.yml` — reusable verify (also re-dispatchable for approval polling)
- `v2/plan.py`, `v2/reconcile.py`, `v2/verify.py`, `v2/lib/*.py` — stdlib-only Python
- `v2/tests/` — `python -m unittest` suite, runs in CI
- v1 files stay untouched until Phase 4 decommission.

## Manifest — `.github/moogs-publish.yml` (schema 1)

Lives ONLY on the mod repo's default branch; all workflows fetch it from there
(`gh api /repos/{r}/contents/.github/moogs-publish.yml?ref=<default-branch>`).

```yaml
schema: 1
mod:
  id: msl                      # short id, [a-z0-9_]+
  name: MoogsStructureLib
  displayPrefix: MSL
  validate: false              # optional; true = run the datapack validator before publish
platforms:                     # mod-level platform projects (required)
  curseforge: { projectId: 1337167, slug: moogs-structure-lib }
  modrinth:   { projectId: 1oUDhxuy, slug: moogs-structure-lib }
discord:                       # all optional except embedColor
  embedColor: "#39313f"
  avatarUrl: "https://..."
  bannerUrl: "https://..."
  thumbnailUrl: "https://..."
  pingDefault: true            # approve-modal default; NOT mutable state
artifacts:
  - type: mod-jar              # mod-jar | universal-jar | config-pack
  # config-pack additionally:
  # - type: config-pack
  #   platforms: { curseforge: {projectId: 732906}, modrinth: {projectId: kV5gBvz6} }
  #   includeAlpha: false      # default false
targets:                       # one entry per publish branch
  - branch: 26.2.0             # REQUIRED — branch identity is declared, never derived
    mc: { start: "26.2", end: "26.2" }   # inclusive window; plan expands via platform version lists
    loaders: [fabric, neoforge]          # omit for universal-jar (loaders implied [])
    java: 25
    displaySuffix: "[FABRIC + NEOFORGE]" # optional
```

Constraints: `loaders` vocab `fabric|forge|neoforge`; `mc.start`/`mc.end` are strings;
targets must have unique `branch`; every target branch must exist in the repo (validator
checks). Universal mods use `type: universal-jar` and targets without `loaders`.

## Tag → target resolution (plan step)

Tag formats: `X.Y.Z-<mcsuffix>` or `X.Y.Z-alpha.N-<mcsuffix>`; `modVersion` = the part before
`-<mcsuffix>`; alpha iff `-alpha.N` present. Resolution: the target whose `mc.start ≤ mcsuffix
≤ mc.end` (version-compare, `X.Y` ≡ `X.Y.0`). Exactly one match required; 0 or >1 → plan fails
loudly. Sanity warning (non-fatal) if `git branch -r --contains <tag>` doesn't include the
resolved target branch.

## publish-plan.json (planVersion 1)

Produced by the plan step at tag time; uploaded as workflow artifact `publish-plan`.

```json
{
  "planVersion": 1,
  "repo": "FinnSetchell/MoogsStructureLib",
  "mod": {"id": "msl", "name": "MoogsStructureLib"},
  "tag": "3.0.7-26.2", "modVersion": "3.0.7", "isAlpha": false,
  "branch": "26.2.0",
  "mcVersions": ["26.2"],
  "java": "25",
  "changelog": "…extracted markdown…",
  "expected": [
    {"artifact": "mod-jar", "loader": "fabric", "platform": "modrinth",
     "projectId": "1oUDhxuy", "mcVersions": ["26.2"]},
    {"artifact": "mod-jar", "loader": "fabric", "platform": "curseforge",
     "projectId": "1337167", "mcVersions": ["26.2"]}
  ]
}
```

`expected` covers ALL platforms/artifacts; release-time platform selection filters it at
publish time (the plan itself is selection-agnostic). Universal-jar entries use
`"loader": null`. Config-pack entries `"artifact": "config-pack"` with their own projectIds,
omitted when `isAlpha && !includeAlpha`.

## publish-result.json (resultVersion 1)

Produced by reconcile-publish; artifact name `publish-result`; also POSTed to the worker in
`/published` (summary form).

```json
{
  "resultVersion": 1, "tag": "3.0.7-26.2", "runId": "123", "attempt": 2,
  "platformsRequested": ["curseforge", "modrinth"],
  "silent": false,
  "uploads": [
    {"artifact": "mod-jar", "loader": "fabric", "platform": "modrinth",
     "status": "uploaded", "id": "AbCd1234", "sha1": "…", "fileName": "…jar", "url": "…"},
    {"artifact": "mod-jar", "loader": "fabric", "platform": "curseforge",
     "status": "skipped-existing", "id": 8496936, "sha1": "…", "fileName": "…jar"}
  ]
}
```

`status` ∈ `uploaded | skipped-existing | failed`. Reconcile rules: Modrinth pre-check via
`GET /v2/version_file/{sha1}` (404 → upload); CurseForge pre-check via previous attempt's
publish-result artifact (same tag) THEN public listing fileName match; uploads serialized;
plugin `maxRetries=1` (retry = rerun the reconcile job, which is idempotent).

## Worker HTTP contracts

All POSTs carry `X-API-Key`. v1 payloads (no `payloadVersion`) keep exact current behavior.

### POST /release  (release-v2.yml → worker)
```json
{
  "payloadVersion": 2,
  "repo": "FinnSetchell/MoogsStructureLib", "tag": "3.0.7-26.2", "branch": "26.2.0",
  "mod": {"id": "msl", "name": "MoogsStructureLib", "displayPrefix": "MSL",
          "displaySuffix": "[FABRIC + NEOFORGE]",
          "cfSlug": "moogs-structure-lib", "mrSlug": "moogs-structure-lib",
          "embedColor": "#39313f", "avatarUrl": "…", "bannerUrl": "…", "thumbnailUrl": "…"},
  "modVersion": "3.0.7", "isAlpha": false,
  "releaseTypeDefault": "minor", "pingDefault": true,
  "platformsAvailable": ["curseforge", "modrinth"],
  "changelog": "…", "runUrl": "…", "planDigest": "sha256:…"
}
```
Worker DEDUPES on `repo+tag`: if a live (non-expired, non-completed) release exists, update
its card and return the SAME `releaseId` (HTTP 200, body `{"releaseId": "…", "deduped": true}`).
Otherwise create, return `{"releaseId": "…"}`.

### Review card buttons (v2 cards)
`approve:{id}`, `approve_silent:{id}` (same modal, silent preset), `schedule:{id}`,
`deny:{id}`, `retry:{id}`. Approve modal fields: releaseType (`major|major_everyone|minor|alpha`),
ping (`true|false`, default from pingDefault), platforms (`both|curseforge|modrinth`, default both).
`major_everyone` + ping=false ⇒ NO @everyone (ping gates everything).
Approve handler MUST use deferred response (`ctx.waitUntil`) — never synchronous dispatch.

### workflow_dispatch → mod repo publish workflow (worker → GitHub)
inputs (all strings): `tag`, `releaseType`, `releaseId`, `platforms` ("curseforge modrinth" |
"curseforge" | "modrinth"), `silent` ("true"|"false"), `ping` ("true"|"false"),
`payloadVersion` ("2").

### POST /published  (verify job in publish-v2.yml → worker, AFTER verification)
```json
{
  "payloadVersion": 2, "releaseId": "…",
  "success": true, "verified": true, "verdict": "verified",
  "silent": false, "announce": true,
  "ping": "", "runUrl": "…",
  "summary": {"uploaded": 4, "skippedExisting": 0, "failed": 0}
}
```
`announce` = success && verified && !silent — computed by the workflow, honored by the worker.
On failure: `ping` carries the publisher mention. Worker stamps card always; announces only on
`announce:true`; cleanup-before-announce (idempotent double-POST → second is a no-op 200).

### POST /verify-alert — unchanged v1 shape (worker already hardened with fallback).

## Verify & announce timing

publish-v2.yml verify job polls: Modrinth by recorded version id / file hash; CurseForge by
fileName on the public listing (`www.curseforge.com/api/v1/mods/{id}/files`) — a file appears
there only once approved, so announce inherently waits for CF approval. Poll schedule inside
the job: every 2 min up to 30 min. If CF still pending at 30 min: POST `/published` with
`verdict: "pending-approval"`, `announce:false`, card stamped "🕐 awaiting CurseForge
approval"; a scheduled re-verify (workflow_dispatch of verify-v2.yml, dispatched by worker
cron every 30 min for releases in this state, up to 24 h) completes the flow and triggers the
announcement when approval lands. After 24 h → alert.

## Alerting invariants

1. Any failed/incomplete publish ⇒ the GitHub run is RED (verify exit ≠ 0 for `failed`,
   `error`; `pending-approval` exits 0 but keeps the card unstamped-successful).
2. Verify failure alerts go BOTH to worker `/verify-alert` AND directly to the
   `DISCORD_ALERT_WEBHOOK` org secret (plain Discord webhook POST from the workflow; skipped
   with a red annotation if the secret is unset).
3. No caller ever downgrades an alert-delivery failure below `::error` + job failure.

## Secrets & env

Mod repos (unchanged): `MODRINTH_API_KEY`, `CURSEFORGE_API_KEY`, `WORKER_API_KEY` +
`GITHUB_TOKEN` implicit. New org-level (optional but recommended): `DISCORD_ALERT_WEBHOOK`.
Workflows NEVER interpolate `${{ inputs.* }}` or worker-supplied values into `run:` bodies —
env only. Tag validated against `^[0-9]+\.[0-9]+\.[0-9]+(-alpha\.[0-9]+)?-[0-9][0-9.]*$`
before use.

## Caller workflow shape (mod repos, v2)

```yaml
# .github/workflows/release.yml  (per publish branch)
name: Release
on: { push: { tags: ['*.*.*-*', '*.*.*-alpha.*-*'] } }
jobs:
  release:
    uses: FinnSetchell/release-actions/.github/workflows/release-v2.yml@v2
    secrets: inherit
# .github/workflows/publish.yml
name: Publish
on:
  workflow_dispatch:
    inputs: { tag: {type: string}, releaseType: {type: string}, releaseId: {type: string},
              platforms: {type: string}, silent: {type: string}, ping: {type: string},
              payloadVersion: {type: string} }
jobs:
  publish:
    uses: FinnSetchell/release-actions/.github/workflows/publish-v2.yml@v2
    with: { tag: "${{ inputs.tag }}", releaseType: "${{ inputs.releaseType }}",
            releaseId: "${{ inputs.releaseId }}", platforms: "${{ inputs.platforms }}",
            silent: "${{ inputs.silent }}", ping: "${{ inputs.ping }}" }
    secrets: inherit
```
No javaVersion/configPack/validate inputs — all from the manifest. (`validate` for universal
mods becomes a manifest flag consumed by release-v2.yml: `validate: true` at mod level.)
