"""
Capture evidence for Salesforce metadata changes discovered by
check_changes.py.

For each change: resolve the Salesforce user, retrieve the component,
capture the Git diff, query supporting Setup Audit Trail evidence, and
write a self-contained evidence directory under audit/.

Does not commit and does not push — that is the caller's responsibility.
Does not modify Salesforce.

Usage:
    python scripts/capture_changes.py --changes changes.json --target-org MyOrgAlias [--environment UAT]

    Where changes.json is the JSON emitted by check_changes.py
    (i.e. {"changes": [...], "count": N}).
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

METADATA_TYPE_MAP = {
    "ApexClass": "ApexClass",
    "ApexTrigger": "ApexTrigger",
    "ApexPage": "ApexPage",
    "Flow": "Flow",
    "LightningComponentBundle": "LightningComponentBundle",
    "CustomObject": "CustomObject",
}


def run_sf_json(args: list[str]) -> dict:
    sf_command = "sf.cmd" if sys.platform == "win32" else "sf"
    result = subprocess.run(
        [sf_command] + args + ["--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    payload = json.loads(result.stdout) if result.stdout else {}
    payload["_returncode"] = result.returncode
    payload["_stderr"] = result.stderr
    return payload


def resolve_user(target_org: str, user_id: str) -> dict:
    soql = f"SELECT Id, Name, Username FROM User WHERE Id = '{user_id}'"
    payload = run_sf_json(
        ["data", "query", "--query", soql, "--target-org", target_org]
    )
    if payload.get("_returncode") != 0:
        return {
            "id": user_id,
            "name": None,
            "username": None,
            "resolved": False,
            "queryFailed": True,
            "error": payload.get("_stderr") or payload.get("message"),
        }
    records = payload.get("result", {}).get("records", [])
    if not records:
        return {
            "id": user_id,
            "name": None,
            "username": None,
            "resolved": False,
        }
    record = records[0]
    return {
        "id": record["Id"],
        "name": record["Name"],
        "username": record["Username"],
        "resolved": True,
    }


def retrieve_component(target_org: str, member_type: str, member_name: str) -> dict:
    metadata_arg = f"{METADATA_TYPE_MAP[member_type]}:{member_name}"
    payload = run_sf_json(
        [
            "project",
            "retrieve",
            "start",
            "--metadata",
            metadata_arg,
            "--target-org",
            target_org,
        ]
    )
    success = payload.get("_returncode") == 0
    return {
        "success": success,
        "metadataArg": metadata_arg,
        "error": None if success else payload.get("_stderr") or payload.get("message"),
    }


def query_setup_audit_trail(target_org: str, member_name: str, since_iso: str) -> dict:
    # SetupAuditTrail.Display cannot be filtered in SOQL (Salesforce
    # restriction: "field 'Display' can not be filtered in a query call").
    # Query by date range only and match member_name client-side instead.
    soql = (
        "SELECT Id, Action, CreatedById, CreatedDate, Display, Section "
        "FROM SetupAuditTrail "
        f"WHERE CreatedDate >= {since_iso} "
        "ORDER BY CreatedDate DESC "
        "LIMIT 200"
    )
    payload = run_sf_json(
        ["data", "query", "--query", soql, "--target-org", target_org]
    )
    if payload.get("_returncode") != 0:
        return {
            "matched": False,
            "queryFailed": True,
            "error": payload.get("_stderr") or payload.get("message"),
        }
    all_records = payload.get("result", {}).get("records", [])
    matches = [r for r in all_records if member_name in (r.get("Display") or "")]
    if not matches:
        return {"matched": False}
    return {
        "matched": True,
        "records": [
            {
                "id": r["Id"],
                "action": r["Action"],
                "createdById": r["CreatedById"],
                "createdDate": r["CreatedDate"],
                "display": r["Display"],
                "section": r["Section"],
            }
            for r in matches[:5]
        ],
    }


def git_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "--", "force-app"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout or ""


def audit_time_bucket_earlier(iso_dt: str, minutes: int = 15) -> str:
    """Return a SOQL datetime literal `minutes` before iso_dt, for audit trail lookback."""
    dt = datetime.strptime(iso_dt, "%Y-%m-%dT%H:%M:%S.%f%z")
    earlier = dt.timestamp() - (minutes * 60)
    return datetime.fromtimestamp(earlier, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def capture_one(
    target_org: str, environment: str, change: dict, repo_root: Path
) -> dict:
    member_type = change["memberType"]
    member_name = change["memberName"]
    last_modified = change["lastModifiedDate"]
    changed_by_id = change["changedById"]

    detected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    user_info = resolve_user(target_org, changed_by_id)
    retrieval = retrieve_component(target_org, member_type, member_name)

    if user_info.get("queryFailed"):
        raise RuntimeError(
            f"User lookup query failed for changedById '{changed_by_id}' "
            f"(component {member_type}:{member_name}): {user_info.get('error')}"
        )

    diff_text = ""
    audit_trail = {"matched": False}
    capture_status = "RETRIEVAL_FAILED"

    if retrieval["success"]:
        diff_text = git_diff()
        since = audit_time_bucket_earlier(last_modified)
        audit_trail = query_setup_audit_trail(target_org, member_name, since)

        if not diff_text.strip():
            capture_status = "NO_CONTENT_DIFF"
        elif not user_info["resolved"]:
            capture_status = "ATTRIBUTION_UNCERTAIN"
        else:
            capture_status = "CAPTURE_COMPLETE"

    event = {
        "environment": environment,
        "component": {"type": member_type, "name": member_name},
        "salesforce": {
            "lastModifiedDate": last_modified,
            "changedById": user_info["id"],
            "changedByName": user_info["name"],
            "changedByUsername": user_info["username"],
        },
        "monitor": {"detectedAt": detected_at},
        "captureStatus": capture_status,
        "retrieval": retrieval,
    }

    event_dir_rel = None

    if capture_status != "NO_CONTENT_DIFF":
        # A no-op save (LastModifiedDate advanced but retrieved content is
        # byte-identical to what's already in Git) produces no audit trail
        # entry and no commit — it's not evidence of a real change, just
        # noise. State still advances below so it isn't redetected forever.
        event_id = f"{detected_at.replace(':', '').replace('-', '')[:15]}-{member_type.lower()}-{member_name}"
        event_dir = repo_root / "audit" / _date_path(detected_at) / event_id
        event_dir.mkdir(parents=True, exist_ok=True)

        (event_dir / "event.json").write_text(
            json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (event_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
        (event_dir / "setup-audit.json").write_text(
            json.dumps(audit_trail, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        event_dir_rel = str(event_dir.relative_to(repo_root))

    return {
        "memberType": member_type,
        "memberName": member_name,
        "lastModifiedDate": last_modified,
        "captureStatus": capture_status,
        "eventDir": event_dir_rel,
        "changedById": user_info["id"],
        "changedByName": user_info["name"],
        "changedByUsername": user_info["username"],
    }


def _date_path(iso_dt: str) -> str:
    dt = datetime.strptime(iso_dt, "%Y-%m-%dT%H:%M:%S+00:00")
    return f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}"


_ADVANCING_STATUSES = {"CAPTURE_COMPLETE", "ATTRIBUTION_UNCERTAIN", "NO_CONTENT_DIFF"}


def update_state(state_path: Path, results: list[dict]) -> None:
    """
    Advance lastProcessedTimestamps[type] to the max lastModifiedDate seen
    for that type in this batch, but ONLY for types where every change in
    the batch reached a terminal, successfully-handled status
    (_ADVANCING_STATUSES). RETRIEVAL_FAILED for any change of a type
    leaves that type's state untouched so the next run retries it — never
    silently advance state past a failure.
    NO_CONTENT_DIFF counts as handled — it produced no audit entry (see
    capture_one), but the underlying LastModifiedDate bump was real and
    should not be redetected forever.
    """
    state = json.loads(state_path.read_text(encoding="utf-8"))

    by_type: dict[str, list[dict]] = {}
    for r in results:
        by_type.setdefault(r["memberType"], []).append(r)

    for member_type, type_results in by_type.items():
        if all(r["captureStatus"] in _ADVANCING_STATUSES for r in type_results):
            newest = max(r["lastModifiedDate"] for r in type_results)
            state["lastProcessedTimestamps"][member_type] = newest

    state["lastSuccessfulCheck"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )

    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def log_no_content_diffs(log_path: Path, results: list[dict]) -> None:
    """
    NO_CONTENT_DIFF produces no audit/ folder (see capture_one), which
    would otherwise mean no record at all of *which* component was
    involved when Salesforce reported a change but the retrieved content
    matched Git exactly. Append one line per such result so it's
    traceable later — e.g. to check whether it's a real no-op save or a
    field the diff isn't sensitive to.
    """
    no_op_results = [r for r in results if r["captureStatus"] == "NO_CONTENT_DIFF"]
    if not no_op_results:
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        for r in no_op_results:
            entry = {
                "detectedAt": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00"
                ),
                "memberType": r["memberType"],
                "memberName": r["memberName"],
                "lastModifiedDate": r["lastModifiedDate"],
                "changedById": r["changedById"],
                "changedByName": r["changedByName"],
                "changedByUsername": r["changedByUsername"],
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-org", required=True)
    parser.add_argument(
        "--environment",
        default="UAT",
        help="Label recorded in event.json's 'environment' field (default: UAT).",
    )
    parser.add_argument("--changes", required=True, type=Path)
    parser.add_argument("--repo-root", default=".", type=Path)
    parser.add_argument(
        "--state", default="state/monitoring-state.json", type=Path
    )
    args = parser.parse_args()

    changes_payload = json.loads(args.changes.read_text(encoding="utf-8-sig"))
    changes = changes_payload["changes"]

    results = [
        capture_one(args.target_org, args.environment, change, args.repo_root.resolve())
        for change in changes
    ]

    if results:
        update_state(args.state, results)
        log_no_content_diffs(
            args.state.parent / "no-content-diff-log.jsonl", results
        )

    print(json.dumps({"captured": results, "count": len(results)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
