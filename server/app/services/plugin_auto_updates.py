"""Explicit, plugin-scoped background-update policy for signed Bridge calls."""
from dataclasses import dataclass
import re

from app.models.site import Site
from app.models.hub_wordpress_job import HubWordPressJob
from app.services.audit import write_audit_log
from app.services.hub_operations import HubOperationError
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService


@dataclass
class PluginAutoUpdateResult:
    rows: list[dict]

    def outcome(self):
        return {"plugin_auto_updates": self.rows, "outcomes": [
            {"site_id": row["site_id"], "status": row["status"]} for row in self.rows
        ]}


class PluginAutoUpdateService:
    ABILITY = "kosmos-bridge/set-plugin-auto-update-policy"

    def __init__(self, *, db, cipher):
        self.db, self.cipher = db, cipher
        self.proxy = SiteMcpProxyService(db=db, cipher=cipher)

    @staticmethod
    def validate(site_ids, plugin_files, blocked):
        if (not isinstance(site_ids, list) or not site_ids or len(site_ids) > 500
                or any(type(i) is not int or not 0 < i < 2**63 for i in site_ids)):
            raise HubOperationError("1 bis 500 konkrete Websites auswaehlen.")
        if (not isinstance(plugin_files, list) or not 1 <= len(plugin_files) <= 100
                or len(site_ids) * len(plugin_files) > 5000):
            raise HubOperationError("1 bis 100 Plugins und maximal 5000 Website/Plugin-Kombinationen auswaehlen.")
        if type(blocked) is not bool:
            raise HubOperationError("Sperre muss ausdruecklich ein- oder ausgeschaltet werden.")
        for file in plugin_files:
            if (not isinstance(file, str) or len(file) > 255 or ".." in file
                    or not re.fullmatch(r"[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*\.php", file)):
                raise HubOperationError("Ungueltiger Plugin-Dateischluessel.")

    def set_policy(self, *, site_ids: list[int], plugin_files: list[str], blocked: bool, actor: str):
        self.validate(site_ids, plugin_files, blocked)
        site_ids, plugin_files = sorted(set(site_ids)), list(dict.fromkeys(plugin_files))
        sites = [self.db.get(Site, site_id) for site_id in site_ids]
        if any(site is None for site in sites):
            raise HubOperationError("Eine ausgewaehlte Website fehlt.")
        rows = []
        for site in sites:
            result = {"site_id": site.id, "domain": site.domain, "blocked": blocked, "plugins": []}
            try:
                payload = self.proxy.execute_ability(site.id, self.ABILITY,
                    {"plugin_files": plugin_files, "blocked": blocked}, timeout_seconds=15)
                plugins = self._verified_rows(payload, plugin_files, blocked)
                result["plugins"] = plugins
                result["status"] = "succeeded" if all(p["status"] in {"succeeded", "skipped"} for p in plugins) else "failed"
                result["message"] = "Pruefung abgeschlossen. Nicht installierte Plugins bleiben unveraendert."
            except SiteMcpProxyError as exc:
                if exc.code == "KOSMOS_BRIDGE_ABILITY_NOT_FOUND":
                    result.update(status="unsupported", message="Zuerst Kosmos Bridge auf Version 0.3.68 oder neuer aktualisieren.")
                elif exc.code == "MCP_NOT_AVAILABLE":
                    result.update(status="unreachable", message="Keine Bridge-Verbindung vorhanden; nichts uebertragen.")
                elif exc.code == "KOSMOS_BRIDGE_MULTISITE_UNSUPPORTED":
                    result.update(status="unsupported", message="Multisite wird nicht veraendert: Die Einstellung gilt dort fuer das gesamte Netzwerk.")
                else:
                    result.update(status="uncertain", message=f"Bridge-Anfrage nicht bestaetigt (HTTP {exc.status_code}). Website pruefen; keine automatische Wiederholung.")
            except (TimeoutError, OSError, ValueError, TypeError, AttributeError):
                result.update(status="uncertain", message="Website nicht erreichbar oder Antwort unvollstaendig. Einstellung nicht bestaetigt; keine automatische Wiederholung.")
            rows.append(result)
            write_audit_log(self.db, site=site, actor=actor, source="hub",
                action="plugin-auto-update-policy", result=result["status"],
                detail=f"blocked={blocked}; plugins={','.join(plugin_files)}; {result['message']}")
            # Persist completed site outcomes even if a later site or the worker fails.
            job_id = self.db.info.get("wordpress_job_id")
            if job_id:
                job = self.db.get(HubWordPressJob, job_id)
                if job and job.status == "running":
                    job.result_json = PluginAutoUpdateResult(list(rows)).outcome()
                    job.message = f"{len(rows)}/{len(sites)} Websites geprueft."
            self.db.commit()
        return PluginAutoUpdateResult(rows)

    @staticmethod
    def _verified_rows(payload, files, blocked):
        raw = payload.get("result", {}).get("plugins")
        if not isinstance(raw, list) or len(raw) != len(files):
            raise ValueError("Incomplete confirmation")
        indexed = {}
        for row in raw:
            if (not isinstance(row, dict) or row.get("plugin_file") not in files or row["plugin_file"] in indexed
                    or any(type(row.get(key)) is not bool for key in ("installed", "blocked", "configured", "verified"))):
                raise ValueError("Invalid confirmation")
            indexed[row["plugin_file"]] = row
        result = []
        for file in files:
            row = indexed[file]
            status = "skipped" if not row["installed"] else (
                "succeeded" if row["verified"] and row["blocked"] is blocked and (not blocked or not row["configured"]) else "failed")
            message = {"skipped": "Nicht installiert; nichts geaendert.",
                "succeeded": "Automatische Updates gesperrt." if blocked else "Hub-Sperre aufgehoben; Automatik nicht eingeschaltet.",
                "failed": "Gewuenschte Einstellung nicht bestaetigt. Website pruefen."}[status]
            result.append({"plugin_file": file, "status": status, "message": message})
        return result
