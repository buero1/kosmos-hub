import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.plugin_official_version import PluginOfficialVersion


@dataclass(frozen=True)
class OfficialVersionCandidate:
    plugin_file: str
    reported_versions: tuple[str, ...]
    reported_sources: tuple[str, ...]


class OfficialPluginVersionService:
    """Keeps inspectable version evidence separate from update execution."""

    WORDPRESS_ORG_API = "https://api.wordpress.org/plugins/info/1.2/{slug}.json"
    REQUEST_TIMEOUT_SECONDS = 8
    MAX_CONCURRENT_REQUESTS = 6

    def __init__(self, *, db: Session):
        self.db = db

    def get_cached(self, plugin_files: Iterable[str]) -> dict[str, PluginOfficialVersion]:
        normalized_files = sorted({plugin_file.strip() for plugin_file in plugin_files if plugin_file and plugin_file.strip()})
        if not normalized_files:
            return {}
        records = self.db.scalars(
            select(PluginOfficialVersion).where(PluginOfficialVersion.plugin_file.in_(normalized_files))
        ).all()
        return {record.plugin_file: record for record in records}

    def refresh_for_inventory(self, items: Iterable[Any]) -> dict[str, int]:
        candidates = self._collect_candidates(items)
        if not candidates:
            return {"checked": 0, "wordpress_org": 0, "provider_offer": 0, "unavailable": 0, "failed": 0}

        wordpress_org_results: dict[str, tuple[str | None, str | None]] = {}
        with ThreadPoolExecutor(max_workers=self.MAX_CONCURRENT_REQUESTS) as executor:
            futures = {
                executor.submit(self._fetch_wordpress_org_version, candidate.plugin_file): candidate.plugin_file
                for candidate in candidates.values()
            }
            for future in as_completed(futures):
                plugin_file = futures[future]
                try:
                    wordpress_org_results[plugin_file] = future.result()
                except Exception:
                    wordpress_org_results[plugin_file] = (None, "wordpress_org_request_failed")

        existing = self.get_cached(candidates)
        checked_at = datetime.now(UTC)
        summary = {"checked": len(candidates), "wordpress_org": 0, "provider_offer": 0, "unavailable": 0, "failed": 0}

        for plugin_file, candidate in candidates.items():
            version, error = wordpress_org_results.get(plugin_file, (None, "wordpress_org_request_failed"))
            if version:
                official_version = version
                source = "WordPress.org"
                last_error = None
                summary["wordpress_org"] += 1
            else:
                reported_version = self._highest_version(candidate.reported_versions)
                if reported_version:
                    official_version = reported_version
                    source_name = next(iter(candidate.reported_sources), "WordPress update provider")
                    source = f"Site update provider: {source_name}"[:128]
                    last_error = None
                    summary["provider_offer"] += 1
                else:
                    official_version = None
                    source = "No public or provider version available"
                    last_error = error
                    summary["unavailable"] += 1
                    if error and error != "wordpress_org_not_found":
                        summary["failed"] += 1

            record = existing.get(plugin_file)
            if record is None:
                record = PluginOfficialVersion(
                    plugin_file=plugin_file,
                    official_version=official_version,
                    source=source,
                    checked_at=checked_at,
                    last_error=last_error,
                )
                self.db.add(record)
            else:
                record.official_version = official_version
                record.source = source
                record.checked_at = checked_at
                record.last_error = last_error

        self.db.flush()
        return summary

    @staticmethod
    def comparison(*, current_version: str, reported_version: str, official_version: str | None) -> tuple[bool, str]:
        if not official_version:
            return False, "Official version not available yet."
        if reported_version and reported_version != official_version:
            return True, f"Mismatch: site reports {reported_version}; official version is {official_version}."
        if not reported_version and current_version and current_version != official_version:
            return True, f"Mismatch: installed {current_version}; official version {official_version} has no site update offer."
        if reported_version:
            return False, "The reported update matches the official version."
        return False, "Installed version matches the official version."

    def _collect_candidates(self, items: Iterable[Any]) -> dict[str, OfficialVersionCandidate]:
        reports: dict[str, list[str]] = {}
        sources: dict[str, list[str]] = {}
        plugin_files: set[str] = set()

        for item in items:
            for plugin in getattr(item, "plugins", ()):
                if not isinstance(plugin, dict):
                    continue
                plugin_file = str(plugin.get("plugin_file", "")).strip()
                if plugin_file:
                    plugin_files.add(plugin_file)
            for update in getattr(item, "plugin_updates", ()):
                if not isinstance(update, dict):
                    continue
                plugin_file = str(update.get("plugin_file", "")).strip()
                reported_version = str(update.get("new_version", "")).strip()
                if not plugin_file:
                    continue
                plugin_files.add(plugin_file)
                if reported_version:
                    reports.setdefault(plugin_file, []).append(reported_version)
                    source = str(update.get("update_source", "")).strip() or "WordPress"
                    sources.setdefault(plugin_file, []).append(source)

        return {
            plugin_file: OfficialVersionCandidate(
                plugin_file=plugin_file,
                reported_versions=tuple(reports.get(plugin_file, ())),
                reported_sources=tuple(sources.get(plugin_file, ())),
            )
            for plugin_file in sorted(plugin_files)
        }

    def _fetch_wordpress_org_version(self, plugin_file: str) -> tuple[str | None, str | None]:
        slug = self._wordpress_org_slug(plugin_file)
        if not slug:
            return None, "wordpress_org_slug_unavailable"
        request = Request(
            self.WORDPRESS_ORG_API.format(slug=quote(slug, safe="")),
            headers={"User-Agent": "Kosmos-Hub/0.1 (+https://kosmos-hub.31-70-92-95.sslip.io)"},
        )
        try:
            with urlopen(request, timeout=self.REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310: fixed official API endpoint.
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return None, "wordpress_org_not_found" if exc.code == 404 else f"wordpress_org_http_{exc.code}"
        except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            return None, "wordpress_org_request_failed"

        version = str(payload.get("version", "")).strip() if isinstance(payload, dict) else ""
        return (version, None) if version else (None, "wordpress_org_version_missing")

    @staticmethod
    def _wordpress_org_slug(plugin_file: str) -> str:
        normalized = plugin_file.strip().replace("\\", "/")
        directory, separator, filename = normalized.partition("/")
        candidate = directory if separator else filename.removesuffix(".php")
        return candidate if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,199}", candidate) else ""

    @classmethod
    def _highest_version(cls, versions: Iterable[str]) -> str | None:
        candidates = [version.strip() for version in versions if version and version.strip()]
        return max(candidates, key=cls._version_key) if candidates else None

    @staticmethod
    def _version_key(value: str) -> tuple[tuple[int, int | str], ...]:
        parts = re.findall(r"\d+|[A-Za-z]+", value)
        return tuple((1, int(part)) if part.isdigit() else (0, part.casefold()) for part in parts)
