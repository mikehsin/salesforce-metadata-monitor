# Salesforce Metadata Monitor

Automated, read-only change-history recorder for a shared Salesforce
org. Useful when multiple developers/admins work in the same sandbox
without Git, and you need an independent record of **what changed, who
Salesforce says changed it, and when**.

It is not a deployment pipeline and does not replace normal development
process. It never writes to Salesforce.

## How it works

```
Salesforce org
      │
      │ every 5 minutes
      ▼
GitHub Actions ──► authenticate (OAuth client credentials)
      │
      ▼
check_changes.py ──► query each monitored type for
      │               LastModifiedDate > last known timestamp
      ▼
  any changes?
      │
  no ─┴─ done
      │
  yes
      ▼
capture_changes.py ──► resolve the Salesforce user
      │                retrieve the changed component
      │                diff against Git
      │                query Setup Audit Trail for corroboration
      ▼
  write audit/<date>/<event>/ (event.json, diff.patch, setup-audit.json)
      │
      ▼
  commit + push to GitHub
```

Once a day, a separate scan compares the full component set in
Salesforce against what's in Git to catch deletions and writes a
report (see [Deletion scanning](#deletion-scanning)).

## Monitored metadata

| Type | Detected (modified) | Detected (deleted) |
| --- | --- | --- |
| ApexClass | ✅ | ✅ |
| ApexTrigger | ✅ | ✅ |
| ApexPage | ✅ | ✅ |
| LightningComponentBundle | ✅ | ✅ |
| Flow | ✅ | ❌ not feasible — see below |
| CustomObject | ✅ | ❌ not feasible — see below |
| CustomMetadata | retrieval only, not polled | ❌ |

**Flow** deletion can't be detected: the field that's safe to bulk-query
(`MasterLabel`) doesn't reliably match the on-disk filename, and the
field that does (`FullName`) can only be queried one record at a time.

**CustomObject** deletion can't be detected: the query returns
unrelated system/package objects while omitting standard objects
(Account, Opportunity, ...) entirely, so it's not a reliable proxy for
"what's actually in Git."

**CustomMetadata** isn't pollable at all — it's a retrieve/deploy
category, not a queryable object. Its record types are per-org and
dynamic. It's still captured in full baseline retrieves, just not
change-detected automatically.

## Repository layout

```
force-app/                Salesforce metadata (the actual audited content)
manifest/package.xml      What gets retrieved
scripts/
  check_changes.py        Detect modifications (+ optional deletion scan)
  capture_changes.py      Resolve user, retrieve, diff, write evidence
state/monitoring-state.example.json   Template — copy to monitoring-state.json
audit/YYYY/MM/DD/<event>/ One folder per captured change (created at runtime)
reports/deletion-scans/   Daily deletion-scan reports (created at runtime)
.github/workflows/monitor.yml
```

## Evidence format

Each captured change gets `audit/<date>/<event-id>/`:

- **event.json** — component, Salesforce user (id/name/username),
  timestamp, `captureStatus`
- **diff.patch** — the actual Git diff (source of truth for content
  changes)
- **setup-audit.json** — matching Setup Audit Trail record(s), if any
  (`{"matched": false}` if none found — never fabricated)

`captureStatus` values:

| Status | Meaning |
| --- | --- |
| `CAPTURE_COMPLETE` | Full evidence captured, user resolved |
| `NO_CONTENT_DIFF` | Salesforce reported a change but retrieved content is identical to Git — no-op save, no audit folder written |
| `ATTRIBUTION_UNCERTAIN` | Retrieved fine, but the changing user couldn't be resolved |
| `RETRIEVAL_FAILED` | Could not retrieve the component |

`NO_CONTENT_DIFF` skips the audit folder to avoid committing empty
evidence for no-op saves, but still appends one line to
`state/no-content-diff-log.jsonl` (component, timestamp, who) — enough
to trace which component and user was involved later.

Commit authorship is always the bot identity configured in the
workflow — never the Salesforce user who made the change. The
Salesforce user is recorded inside the evidence, not as the Git author.

## Known limitations

- **Cannot guarantee every intermediate save is captured.** If someone
  saves twice between two 5-minute checks, only the latest version is
  retrievable — the earlier one is gone once overwritten in Salesforce.
- **`SourceMember`/source tracking doesn't exist on non-scratch orgs**
  (it's a scratch-org-only feature). Detection uses `LastModifiedDate`
  per type instead — reliable for the types listed above, at the cost
  of one query per type instead of one combined query.
- Deletion detection is scoped to 4 of 7 monitored types (see table
  above), and is **report-only** — it never deletes files or commits
  removals automatically. A human reviews the daily report and decides.

## Setup

**1. Salesforce side**

- Permission Set with `API Enabled`, `View All Data`, `Author Apex`,
  `View Setup and Configuration`, `View All Users`.
- Dedicated integration user, assigned that Permission Set.
- External Client App (Setup → External Client Apps) with the Client
  Credentials OAuth flow enabled, "Run As" set to the integration user.
  Note the Consumer Key and Consumer Secret.

**2. This repository**

- `cp state/monitoring-state.example.json state/monitoring-state.json`
  and fill in real starting timestamps (or `2024-01-01T00:00:00.000+0000`
  for all types to detect everything on the first run).
- Adjust `manifest/package.xml` and `MONITORED_TYPES` in
  `scripts/check_changes.py` if you want a different metadata scope.
- Retrieve an initial baseline into `force-app/` and commit it before
  enabling the schedule:
  ```bash
  sf project retrieve start --manifest manifest/package.xml --target-org YourOrgAlias
  git add force-app state && git commit -m "chore: initial baseline"
  ```

**3. GitHub side**

Repository secrets: `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_LOGIN_URL`
(your org's instance/My Domain URL). Optionally set the repository
variable `MONITOR_ENVIRONMENT` (e.g. `UAT`, `Staging`) — it's recorded
in each `event.json`, purely a label.

Update the two `TARGET_ORG` placeholders in
`.github/workflows/monitor.yml` to your org alias (or just leave it —
it's an alias registered by the workflow itself during auth, not a
real hostname).

Test with `workflow_dispatch` before trusting the 5-minute schedule.

## Deletion scanning

Run manually any time:

```bash
python scripts/check_changes.py --target-org YourOrgAlias --check-deletions
```

Runs automatically once a day via the workflow, writing
`reports/deletion-scans/<timestamp>.json` if anything is flagged.
Nothing is deleted automatically — review the report and remove the
corresponding `force-app/` files manually if confirmed.

## Safety rules

- Never deploy to Salesforce from this repository or its automation.
- Never force-push, rewrite, or delete evidence history.
- Never fabricate audit data — an unavailable source is recorded as
  such (`"matched": false`, `ATTRIBUTION_UNCERTAIN`, etc.), not guessed.
- A failed capture must never advance `state/monitoring-state.json` —
  it must remain retryable on the next run.

## License

MIT — see [LICENSE](LICENSE).
