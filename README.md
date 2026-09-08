# Salesforce Metadata Monitor

A one-person backup mechanism for keeping an independent eye on a
shared Salesforce UAT/staging org — no cooperation from the client
required.

## Why this exists

On a lot of implementation projects, each team gets its own isolated
Dev org, so nobody else is touching your code there. Production is
locked down and change-controlled. But the **UAT/staging org in the
middle is often shared** — the client's own IT team or in-house
engineers frequently have access to the same sandbox the implementation
team is testing in, and it's common for the client to not provide any
Git or version control for that environment at all.

That means changes can land in UAT from someone outside your team, at
any time, with zero paper trail — and if something breaks later, there's
no way to say who touched what, or prove your own work wasn't the cause.

This tool solves that for **a single engineer** who wants a
independent, tamper-evident record of every metadata change in that
org — without needing the client to set up anything, without asking
anyone's permission for a shared process, and without interrupting
anyone else's workflow. It runs entirely in your own GitHub account,
using an integration user only you control.

It is not a deployment pipeline, doesn't touch Dev/Prod, and never
writes to Salesforce.

## How it works

```
Salesforce org (e.g. shared UAT)
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
  commit + push to your GitHub repo
```

Once a day, a separate scan compares the full component set in
Salesforce against what's in Git to catch deletions and writes a
report (see [Deletion scanning](#deletion-scanning)).

Everything runs on GitHub's infrastructure via a scheduled Action —
your laptop doesn't need to be on, and nobody else in the org needs to
know it's running.

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
Salesforce user is recorded inside the evidence, not as the Git author,
so it's clear this is an automated audit trail, not someone's real
commit history.

## Known limitations

- **Cannot guarantee every intermediate save is captured.** If someone
  saves twice between two 5-minute checks, only the latest version is
  retrievable — the earlier one is gone once overwritten in Salesforce.
- **`SourceMember`/source tracking doesn't exist on non-scratch orgs**
  (it's a scratch-org-only feature) — which rules it out for most
  shared UAT sandboxes anyway. Detection uses `LastModifiedDate` per
  type instead — reliable for the types listed above, at the cost of
  one query per type instead of one combined query.
- Deletion detection is scoped to 4 of 7 monitored types (see table
  above), and is **report-only** — it never deletes files or commits
  removals automatically. You review the daily report and decide.

---

## Getting it running

You'll do three things: set up a Salesforce integration user, clone
this repo and connect it to your own GitHub account, then wire the two
together with GitHub Secrets. None of this touches the client's own
tooling or requires their involvement beyond normal org access you
likely already have.

### 1. Salesforce side (needs System Administrator access to the org)

1. **Setup → Permission Sets → New.** Create one (e.g. `Metadata Monitor`),
   and under **System Permissions** enable:
   `API Enabled`, `View All Data`, `Author Apex`,
   `View Setup and Configuration`, `View All Users`.
2. **Setup → Users → New User.** Create a dedicated integration user
   (not your own user) with a minimal profile — the Permission Set
   above supplies the actual access. Assign it the Permission Set.
3. **Setup → External Client Apps → New External Client App.**
   Enable OAuth, add the `api` scope, and turn on the **Client
   Credentials Flow**. Set its "Run As" user to the integration user
   from step 2.
4. Open the app and note the **Consumer Key** and **Consumer Secret**
   (may require "Manage Consumer Details" + an email verification
   step). Also note your org's **My Domain login URL**
   (`https://<yourdomain>.my.salesforce.com`, or
   `https://<yourdomain>--<sandboxname>.sandbox.my.salesforce.com` for
   a sandbox).

### 2. Get the project running in VS Code

1. Clone this repo and open it in VS Code:
   ```bash
   git clone https://github.com/<your-username>/salesforce-metadata-monitor.git
   cd salesforce-metadata-monitor
   ```
2. Install the [Salesforce Extension Pack](https://marketplace.visualstudio.com/items?itemName=salesforce.salesforcedx-vscode)
   and the [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli)
   if you don't have them.
3. Authenticate your own user to the org for local setup (this is just
   for the one-time baseline — the integration user above is what the
   automation uses later):
   ```bash
   sf org login web --alias UAT --instance-url https://<yourdomain>--<sandboxname>.sandbox.my.salesforce.com
   ```
4. Copy the state template and fill in a starting point — using an old
   date detects everything currently in the org as the baseline:
   ```bash
   cp state/monitoring-state.example.json state/monitoring-state.json
   ```
5. If you want a different metadata scope than the default (Apex
   classes/triggers/pages, Flows, LWC, Custom Objects/Metadata), edit
   `manifest/package.xml` and `MONITORED_TYPES` in
   `scripts/check_changes.py` to match.
6. Pull down the current state of the org as your baseline, then
   commit it — this becomes "everything before this point is already
   accounted for":
   ```bash
   sf project retrieve start --manifest manifest/package.xml --target-org UAT
   git add force-app state
   git commit -m "chore: initial baseline"
   ```
7. Create your own GitHub repo (private is fine — this is your audit
   trail, not something you need to share) and push:
   ```bash
   git remote set-url origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin master
   ```

### 3. Wire it up on GitHub

1. In your repo: **Settings → Secrets and variables → Actions →
   New repository secret.** Add:
   - `SF_CLIENT_ID` — the Consumer Key from step 1.4
   - `SF_CLIENT_SECRET` — the Consumer Secret from step 1.4
   - `SF_LOGIN_URL` — the My Domain URL from step 1.4
2. (Optional) **Settings → Secrets and variables → Actions →
   Variables** tab: add `MONITOR_ENVIRONMENT` (e.g. `UAT`) — just a
   label recorded in each captured event, not functional.
3. Go to **Actions** tab → select the workflow → **Run workflow** to
   trigger it manually once. Confirm it authenticates and finishes
   without errors before trusting the 5-minute schedule.
4. From here it runs on its own. Check back on the repo's commit
   history whenever you want to see what's changed in the org — no
   further action needed on your part.

## Deletion scanning

Run manually any time:

```bash
python scripts/check_changes.py --target-org UAT --check-deletions
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

## License

MIT — see [LICENSE](LICENSE).
