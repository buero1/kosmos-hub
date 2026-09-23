"""Shared status serialization for the website UI and agent readers."""
from app.services.site_admin_launch import SiteAdminLaunchService


def _fleet_refresh_status_payload(run) -> dict:
    """Keep the browser payload small while exposing every live progress counter."""
    result = run.result_json or {}
    return {
        "id": run.id,
        "status": run.status,
        "mode": run.mode,
        "error_message": run.error_message,
        "result": {
            "scope": result.get("scope", {}),
            "sites": result.get("sites", {}),
            "updates": result.get("updates", {}),
            "backups": result.get("backups", {}),
            "users": result.get("users", {}),
            "crocoblock": result.get("crocoblock", {}),
            "official_versions": result.get("official_versions", {}),
            "phase": result.get("phase", {}),
            "last_site": result.get("last_site", ""),
            "errors": result.get("errors", []),
        },
    }


def _direct_update_batch_status_payload(batch_id: str, runs: list) -> dict:
    """Expose the small, live status view needed by the direct-update workbench."""
    terminal_statuses = {"succeeded", "failed", "skipped"}

    def batch_position(run) -> int:
        position = (run.result_json or {}).get("batch_position")
        return position if isinstance(position, int) else run.id

    ordered_runs = sorted(
        runs,
        key=lambda run: (batch_position(run), run.id),
    )
    rows = []
    for run in ordered_runs:
        result = run.result_json or {}
        rows.append(
            {
                "id": run.id,
                "site_id": run.site.id,
                "site_domain": run.site.domain,
                "site_home_url": getattr(run.site, "home_url", "") or "",
                "site_admin_launch_supported": (
                    getattr(run.site, "status", "") == "verified"
                    and SiteAdminLaunchService.bridge_supports_launch(getattr(run.site, "bridge_version", None))
                ),
                "update_kind": result.get("update_kind") or "plugin",
                "update_name": result.get("update_name") or result.get("plugin_name") or "Unknown update",
                "current_version": result.get("current_version") or "-",
                "target_version": result.get("target_version") or "-",
                "status": run.status,
                "stage": result.get("stage") or "queued",
                "stage_message": result.get("stage_message", ""),
                "error_message": run.error_message or "",
            }
        )
    return {
        "batch_id": batch_id,
        "total": len(rows),
        "completed": sum(row["status"] in terminal_statuses for row in rows),
        "succeeded": sum(row["status"] == "succeeded" for row in rows),
        "failed": sum(row["status"] == "failed" for row in rows),
        "skipped": sum(row["status"] == "skipped" for row in rows),
        "cancelled": sum(row["stage"] == "cancelled" for row in rows),
        "cancellation_requested": any(isinstance((run.result_json or {}).get("cancellation"), dict) for run in runs),
        "runs": rows,
    }


def _complete_site_update_status_payload(run, child_runs: list) -> dict:
    result = run.result_json or {}
    events = result.get("events", [])
    events = [event for event in events if isinstance(event, dict)]
    steps = [
        {
            "key": step.step_key,
            "status": step.status,
            "detail": step.detail or "",
        }
        for step in run.steps
    ]
    return {
        "run_id": run.id,
        "site_id": run.site.id,
        "site_domain": run.site.domain,
        "status": run.status,
        "stage": result.get("stage", "queued"),
        "stage_message": result.get("stage_message", ""),
        "workflow_phase": result.get("workflow_phase", "queued"),
        "wave": result.get("wave", 0),
        "max_waves": result.get("max_waves", 0),
        "successful_updates": result.get("successful_updates", 0),
        "failed_updates": result.get("failed_updates", 0),
        "skipped_updates": result.get("skipped_updates", 0),
        "cancellation_requested": isinstance(result.get("cancellation"), dict),
        "completed": run.status in {"succeeded", "failed", "skipped"},
        "events": events,
        "steps": steps,
        "child_updates": [
            {
                "id": child.id,
                "status": child.status,
                "stage": (child.result_json or {}).get("stage", "queued"),
                "update_kind": (child.result_json or {}).get("update_kind", "plugin"),
                "update_name": (child.result_json or {}).get("update_name", "Unknown update"),
                "current_version": (child.result_json or {}).get("current_version", "-"),
                "target_version": (child.result_json or {}).get("target_version", "-"),
                "error_message": child.error_message or "",
            }
            for child in child_runs
        ],
    }

