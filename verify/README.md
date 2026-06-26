# Publish verification

Confirms every release we ship actually lands on **Modrinth** and **CurseForge**
for each loader + Minecraft version, and surfaces failures to Discord. Two layers:

| Layer | When | What | Where |
|---|---|---|---|
| **Workflow-time verify** | end of each publish | verifies the just-published tag; fails the run + alerts on a miss | `verify.yml` (called by `publish.yml`) |
| **Scheduled audit** | every 6h (cron) | re-checks the **latest release per MC line** of every mod; catches async CurseForge moderation rejection | `audit.yml` in the bot repo |

Both call the same stdlib-only library here (`verifier.py`). Verification logic is
Python; the Discord presentation (embeds, channel routing) lives in the Worker bot.

## Components

| File | Repo | Role |
|---|---|---|
| `verify/verifier.py` | release-actions | core library: query Modrinth + CF, classify, verdict |
| `verify/verify_publish.py` | release-actions | CI entry: one tag, reads `gradle.properties`, alerts `/verify-alert` |
| `verify/audit.py` | release-actions | cron entry: latest-per-line across the registry, alerts `/audit-alert` |
| `.github/workflows/verify.yml` | release-actions | reusable `@v1`; a `verify` job in `publish.yml` calls it |
| `registry.json` | moogsmods-bot | one entry per mod (IDs + slugs); consumed by the audit |
| `.github/workflows/audit.yml` | moogsmods-bot | 6h cron + manual dispatch |
| `/verify-alert`, `/audit-alert` | moogsmods-bot Worker | render embeds, route alpha vs release channel |

## Adding a mod

1. **Add one entry to `registry.json`** (in the bot repo):
   ```json
   {
     "key": "mxs",
     "name": "MoogsExampleStructures",
     "repo": "FinnSetchell/MoogsExampleStructures",
     "modrinth":   { "id": "abcd1234", "slug": "mxs-moogs-example-structures" },
     "curseforge": { "id": 1234567,   "slug": "mxs-moogs-example-structures" }
   }
   ```
   IDs/slugs come from the mod's `gradle.properties`
   (`modrinthProjectId` / `modrinthProjectSlug` / `curseforgeProjectId` / `curseforgeProjectSlug`).
   Modrinth and CurseForge slugs are often different — copy both, don't derive.

2. **That's it for workflow-time verify** — because `publish.yml` (central) calls
   `verify.yml` for every mod on the central flow, a new mod gets verification for
   free. Only a mod with its *own* `publish.yml` (not the central one) needs the
   verify job added manually:
   ```yaml
   verify:
     needs: publish
     if: ${{ !cancelled() }}
     uses: FinnSetchell/release-actions/.github/workflows/verify.yml@v1
     with: { tag: ${{ inputs.tag }} }
     secrets: inherit
   ```

Platforms default to `fabric, forge, neoforge`; override per entry with
`"platforms": [...]`. The tag shape defaults to the family pattern; override per
entry with `"tag_pattern": "..."` (named groups `version` + `mc`).

## How matching works (and the gotchas baked in)

- **Alpha vs release is read from the TAG** (`-alpha.N`), not `gradle.properties` —
  the gradle alpha flags are inconsistent across mods.
- **Expected MC versions** come from `publishMcStart` / `publishMcEnd` /
  `publishExtraMcVersions` at the tag. v1 asserts those *named* versions are
  covered (not every intermediate dot-release in the range).
- **CurseForge needs no API key.** We query the website-internal API
  (`www.curseforge.com/api/v1/mods/{id}/files`), not the gated Eternal API. It
  **requires `removeAlphas=false`** or alpha files are hidden (would false-positive
  every alpha). The moderation status is the `status` field (the FileStatus enum);
  the public API only lists approved files, so a *rejected* file reads as **missing**
  — still flagged.
- **Modrinth publishes the same `version_number` once per MC line** (e.g. `3.0.0`
  for both 1.20 and 1.21). The checker evaluates *all* same-numbered versions and
  accepts the best-covering one — matching only the first would check the wrong line.
- **The audit is scoped to the latest release per MC line** (highest semver), so
  strays (a mis-tagged low version) and historical partial gaps are ignored.

## Severity & alerting (v1)

`fail` (missing / rejected / incomplete / wrong-type) → red, alerts + fails the
verify job. `error` (transient API issue, e.g. CF challenging a runner) → alerts
but does **not** fail the run. `pending` (CF still in moderation) → silent.
Clean runs are silent unless `HEARTBEAT` is set.

## Running locally

```bash
# one tag
TAG=3.0.0-1.21.x DRY_RUN=1 WORKER_URL= WORKER_API_KEY= \
  python verify/verify_publish.py        # from the mod's checkout dir

# full audit (latest per line, all mods)
GITHUB_TOKEN=$(gh auth token) DRY_RUN=1 REGISTRY=path/to/registry.json \
  python verify/audit.py
```
