"""
Detect Salesforce metadata changes since the last processed timestamp.

Read-only. Does not retrieve metadata, does not commit, does not modify
Salesforce in any way.

Usage:
    python scripts/check_changes.py --target-org MyOrgAlias [--state state/monitoring-state.json]

Exit code 0 with JSON output either way. Callers decide what to do next.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Maps monitored metadata type -> (SOQL object name, display-name field)
#
# NOTE: CustomMetadata is intentionally excluded. It is a retrieve/deploy
# category, not a queryable Tooling API sObject — individual records live
# under their own per-type object (e.g. MyType__mdt), which is dynamic
# per org. See README's "Monitored metadata" section. It can still be
# retrieved in full/baseline snapshots via manifest/package.xml, just
# not polled here.
MONITORED_TYPES = {
    "ApexClass": "Name",
    "ApexTrigger": "Name",
    "ApexPage": "Name",
    "Flow": "MasterLabel",
    "LightningComponentBundle": "DeveloperName",
    "CustomObject": "DeveloperName",
}

# Types eligible for deletion detection, and how their component names
# map to on-disk paths under force-app/main/default/. ApexClass/
# ApexTrigger/ApexPage are file-per-component; LWC is
# directory-per-component. Only types whose MONITORED_TYPES query field
# is guaranteed to equal the on-disk file/directory name are included:
#
# - Flow is excluded: MasterLabel is a human-readable label that can
#   differ from the API name used as the filename (e.g. "Auto Assign
#   Lead to User" vs Auto_Assign_Lead_to_User.flow-meta.xml). The field
#   that does match the filename, FullName, can only be queried one
#   record at a time in the Tooling API, making a full-set comparison
#   impractical (one query per existing Flow).
# - CustomObject is excluded: querying it returns every custom object
#   type definition in the org (including ones never retrieved into
#   force-app/, e.g. installed-package internals), while standard
#   objects like Account/Opportunity don't appear via this query at
#   all — DeveloperName here isn't a reliable proxy for "the set of
#   CustomObjects present in Git."
DELETION_CHECK_LAYOUT = {
    "ApexClass": ("classes", "{name}.cls"),
    "ApexTrigger": ("triggers", "{name}.trigger"),
    "ApexPage": ("pages", "{name}.page"),
    "LightningComponentBundle": ("lwc", "{name}"),
}


def run_soql(target_org: str, soql: str) -> list[dict]:
    sf_command = "sf.cmd" if sys.platform == "win32" else "sf"
    result = subprocess.run(
        [
            sf_command,
            "data",
            "query",
            "--query",
            soql,
            "--target-org",
            target_org,
            "--use-tooling-api",
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"sf data query failed (exit {result.returncode}) for org "
            f"'{target_org}':\nquery: {soql}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    payload = json.loads(result.stdout)
    return payload["result"]["records"]


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        raise FileNotFoundError(
            f"State file not found: {state_path}. "
            "Establish a baseline (see README's Setup section) before running detection."
        )
    with state_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def names_in_git(member_type: str, repo_root: Path) -> set[str]:
    subdir, pattern = DELETION_CHECK_LAYOUT[member_type]
    base = repo_root / "force-app" / "main" / "default" / subdir
    if not base.exists():
        return set()

    is_dir_per_component = "{name}" == pattern
    names = set()
    for entry in base.iterdir():
        if is_dir_per_component:
            if entry.is_dir():
                names.add(entry.name)
        else:
            suffix = pattern.split("{name}", 1)[1]
            if entry.name.endswith(suffix) and not entry.name.endswith(
                suffix + "-meta.xml"
            ):
                names.add(entry.name[: -len(suffix)])
    return names


def names_in_salesforce(target_org: str, member_type: str, name_field: str) -> set[str]:
    soql = f"SELECT {name_field} FROM {member_type}"
    return {record[name_field] for record in run_soql(target_org, soql)}


def find_deletions(target_org: str, repo_root: Path) -> list[dict]:
    """
    Compare the full current component-name set per type against what's on
    disk in force-app/. A name present in Git but absent from Salesforce is
    a likely deletion. This is a full-set comparison, not a
    since-last-check one — it can't be timestamp-scoped because a deleted
    record has no LastModifiedDate to filter on.
    """
    deletions = []
    for member_type in DELETION_CHECK_LAYOUT:
        name_field = MONITORED_TYPES[member_type]
        git_names = names_in_git(member_type, repo_root)
        if not git_names:
            continue
        sf_names = names_in_salesforce(target_org, member_type, name_field)
        for missing_name in sorted(git_names - sf_names):
            deletions.append(
                {"memberType": member_type, "memberName": missing_name}
            )
    return deletions


def find_changes(target_org: str, last_processed: dict) -> list[dict]:
    changes = []
    for member_type, name_field in MONITORED_TYPES.items():
        since = last_processed.get(member_type)
        if not since:
            raise ValueError(
                f"No lastProcessedTimestamps entry for {member_type}. "
                "State file is incomplete relative to MONITORED_TYPES."
            )

        soql = (
            f"SELECT Id, {name_field}, LastModifiedDate, LastModifiedById "
            f"FROM {member_type} "
            f"WHERE LastModifiedDate > {since} "
            f"ORDER BY LastModifiedDate ASC"
        )

        for record in run_soql(target_org, soql):
            changes.append(
                {
                    "memberType": member_type,
                    "memberName": record[name_field],
                    "lastModifiedDate": record["LastModifiedDate"],
                    "changedById": record["LastModifiedById"],
                }
            )

    changes.sort(key=lambda c: c["lastModifiedDate"])
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-org", required=True)
    parser.add_argument(
        "--state", default="state/monitoring-state.json", type=Path
    )
    parser.add_argument(
        "--check-deletions",
        action="store_true",
        help=(
            "Also scan for deletions by comparing the full Salesforce "
            "component set against force-app/. This queries every "
            "monitored type in full, not just since the last check, so "
            "it costs more than the default modification check — off "
            "by default."
        ),
    )
    parser.add_argument("--repo-root", default=".", type=Path)
    args = parser.parse_args()

    state = load_state(args.state)
    changes = find_changes(args.target_org, state["lastProcessedTimestamps"])

    deletions = []
    if args.check_deletions:
        deletions = find_deletions(args.target_org, args.repo_root.resolve())

    print(
        json.dumps(
            {
                "changes": changes,
                "count": len(changes),
                "deletions": deletions,
                "deletionCount": len(deletions),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
