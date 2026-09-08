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

## Table of contents

- [Why this exists](#why-this-exists)
- [How it works](#how-it-works)
- [Monitored metadata](#monitored-metadata)
- [Evidence format](#evidence-format)
- [Known limitations](#known-limitations)
- [Setup](#setup)
  - [Files you need to edit](#files-you-need-to-edit)
  - [1. Salesforce side](#1-salesforce-side-needs-system-administrator-access-to-the-org)
  - [2. Get the project running in VS Code](#2-get-the-project-running-in-vs-code)
  - [3. Wire it up on GitHub](#3-wire-it-up-on-github)
- [Deletion scanning](#deletion-scanning)
- [Safety rules](#safety-rules)
- [Repository layout](#repository-layout)
- [License](#license)

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

## Setup

You'll do three things: set up a Salesforce integration user, clone
this repo and connect it to your own GitHub account, then wire the two
together with GitHub Secrets. None of this touches the client's own
tooling or requires their involvement beyond normal org access you
likely already have.

> **The only two steps most people actually need:** create the
> Salesforce integration user + External Client App ([1a–1c](#1-salesforce-side-needs-system-administrator-access-to-the-org)),
> then paste the three resulting values into GitHub Secrets ([step 3](#3-wire-it-up-on-github)).
> Everything else works out of the box with no code changes.

### What each file does

| File | Purpose |
| --- | --- |
| `scripts/check_changes.py` | Queries Salesforce for anything modified since the last check; also has the optional `--check-deletions` full-scan mode |
| `scripts/capture_changes.py` | For each detected change: resolves who made it, retrieves the component, diffs it, checks Setup Audit Trail, writes the evidence folder |
| `.github/workflows/monitor.yml` | The scheduler — runs the two scripts above every 5 minutes (and the deletion scan once a day) via GitHub Actions |
| `manifest/package.xml` | Tells the Salesforce CLI which metadata types to retrieve |
| `state/monitoring-state.json` | The bot's memory — "last processed" timestamp per metadata type. You create this once from the `.example.json` template; the bot updates it automatically after that |
| `force-app/` | Where the actual retrieved Salesforce metadata lives — this is what gets diffed and committed |
| `audit/` | Auto-created. One folder per captured change, holding the evidence (diff, resolved user, Setup Audit Trail match) |
| `reports/deletion-scans/` | Auto-created. Daily reports of components that may have been deleted |

### Files you need to edit

Everything below is only needed if you want to change the default
behavior. A first-time setup only touches the first row.

| File | What to change | When |
| --- | --- | --- |
| `state/monitoring-state.json` | Doesn't exist yet — copy from `state/monitoring-state.example.json` and fill in real timestamps | Always, once, during setup (step 2.4) |
| `manifest/package.xml` | Add/remove `<types>` blocks | Only if you want to monitor different/additional metadata types than the default (Apex, Flow, LWC, Custom Object/Metadata) |
| `scripts/check_changes.py` | `MONITORED_TYPES` dict (near the top) | Only if you changed `package.xml`'s scope — this dict must match it, since it drives what gets polled |
| `.github/workflows/monitor.yml` | The two cron schedules (`*/5 * * * *` and the daily deletion scan) | Only if you want a different polling interval |
| `sfdx-project.json` | `name` field | Cosmetic only — safe to leave as-is |

Nothing else needs editing — no file contains Salesforce credentials,
org IDs, or anything org-specific. Those all live in GitHub Secrets
(step 3 below), not in the repo.

### 1. Salesforce side (needs System Administrator access to the org)

You're building three things, in order: a Permission Set (what the bot
can access), an integration user (who the bot acts as), and an
External Client App (how the bot proves who it is). Each depends on
the previous one existing.

#### 1a. Create the Permission Set

1. **Setup** (gear icon, top right) → type `Permission Sets` into the
   Quick Find box → click **Permission Sets**.
2. Click **New**.
3. Fill in:
   - **Label**: `Metadata Monitor` (API Name auto-fills to
     `Metadata_Monitor` — leave it)
   - **License**: leave as **"—None—"**
4. Click **Save**.
5. On the Permission Set's overview page, click **System Permissions**.
6. Click **Edit**, and check these boxes:
   - **API Enabled**
   - **View All Data**
   - **Author Apex**
   - **View Setup and Configuration**
   - **View All Users**
7. Click **Save**.

#### 1b. Create the integration user

1. **Setup** → Quick Find → `Users` → click **Users**.
2. Click **New User**.
3. Fill in:
   - **First Name**: `Metadata` (or anything identifiable)
   - **Last Name**: `Monitor`
   - **Alias**: something ≤8 characters, e.g. `metamon`
   - **Email**: any address you control (Salesforce may try to send a
     verification email here)
   - **Username**: must be globally unique across all of Salesforce —
     use an email-like format, e.g.
     `metadata.monitor@yourcompany.com.uatmonitor` (the trailing
     suffix just keeps it from colliding with a real address)
   - **User License**: **Salesforce** (or **Salesforce Platform** if
     your org has spare platform licenses — confirm Tooling API access
     works under it before relying on it)
   - **Profile**: **Minimum Access - Salesforce** — real access comes
     from the Permission Set, not this profile
4. Uncheck **"Generate new password and notify user immediately"** —
   not needed, since this user only ever authenticates via OAuth, never
   a password login.
5. Click **Save**.
6. On the new user's page, find the **Permission Set Assignments**
   related list → click **Edit Assignments** → move `Metadata Monitor`
   to the "Enabled" column → **Save**.

#### 1c. Create the External Client App

1. **Setup** → Quick Find → `External Client App Manager` → click it.
2. Click **New External Client App**.
3. Fill in:
   - **External Client App Name**: `Metadata Monitor`
   - **Contact Email**: your email
   - **Distribution State**: Local
4. Click **Create**.
5. Find the app's **API (OAuth)** settings section → **Edit**.
6. Check **Enable OAuth**.
7. **Callback URL**: enter a placeholder —
   `https://login.salesforce.com/services/oauth2/callback` (this field
   is required but unused by the flow this tool uses).
8. **OAuth Scopes**: add `Manage user data via APIs (api)`.
9. Find **Flow Enablement** (or similarly named) and check
   **Enable Client Credentials Flow**.
10. Click **Save**.
11. Find the app's **Policies** (or "Client Credentials Flow") settings
    → **Edit** → set **Run As** to the integration user from step 1b →
    **Save**.
12. Back on the app's main page, find **Consumer Key and Secret**
    (you may need to click **Manage Consumer Details**, which can
    trigger an email verification code). Copy both:
    - **Consumer Key** → this becomes `SF_CLIENT_ID`
    - **Consumer Secret** → this becomes `SF_CLIENT_SECRET`
13. Also note your org's **My Domain login URL** — visible in
    **Setup → My Domain**, formatted as
    `https://<yourdomain>.my.salesforce.com` for a production/Dev org,
    or `https://<yourdomain>--<sandboxname>.sandbox.my.salesforce.com`
    for a sandbox. This becomes `SF_LOGIN_URL`.

At this point you should have three values saved somewhere safe:
Consumer Key, Consumer Secret, and the My Domain URL. None of these go
into any file in this repo — they're only ever pasted into GitHub
Secrets in step 3.

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

1. Open your repo on github.com → **Settings** tab → in the left
   sidebar, **Secrets and variables → Actions**.
2. Under the **Secrets** tab, click **New repository secret** three
   times, once for each of:

   | Name (exact, case-sensitive) | Value |
   | --- | --- |
   | `SF_CLIENT_ID` | Consumer Key from step 1c.12 |
   | `SF_CLIENT_SECRET` | Consumer Secret from step 1c.12 |
   | `SF_LOGIN_URL` | My Domain URL from step 1c.13 |

   For each: paste the **Name** exactly as shown, paste the **Value**,
   click **Add secret**.
3. (Optional) Switch to the **Variables** tab → **New repository
   variable** → Name `MONITOR_ENVIRONMENT`, Value e.g. `UAT` or
   `Staging`. This is just a label written into each captured
   `event.json` — purely cosmetic, safe to skip.
4. Go to the **Actions** tab at the top of the repo. If Actions
   are disabled by default, click **"I understand my workflows, go
   ahead and enable them."**
5. In the left sidebar, click the workflow name (e.g.
   **Salesforce Metadata Monitor**).
6. Click **Run workflow** (top right) → select branch `master` →
   **Run workflow**.
7. Wait ~1 minute, then click into the run that appears. Confirm every
   step shows a green check — especially **Authenticate Salesforce**
   and **Check for Salesforce changes**. If something fails, the step's
   log will show the actual Salesforce/CLI error message.
8. Once a manual run succeeds cleanly, the existing `*/5 * * * *`
   schedule in `.github/workflows/monitor.yml` takes over on its own —
   no further action needed. Check back on the repo's commit history
   whenever you want to see what's changed in the org.

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
