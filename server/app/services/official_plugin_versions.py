import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.plugin_official_version import PluginOfficialVersion


@dataclass(frozen=True)
class OfficialVersionCandidate:
    plugin_file: str


@dataclass(frozen=True)
class CrocoblockChangelogLookup:
    """Latest Jet versions published by Crocoblock's public changelog."""

    versions: dict[str, str]
    requested: int
    error: str | None = None


class OfficialPluginVersionService:
    """Keeps inspectable version evidence separate from update execution."""

    WORDPRESS_ORG_API = "https://api.wordpress.org/plugins/info/1.2/?action=plugin_information&request[slug]={slug}"
    ELEMENTOR_PRO_PLUGIN_FILE = "elementor-pro/elementor-pro.php"
    ELEMENTOR_PRO_CHANGELOG_URL = "https://elementor.com/pro/changelog/"
    ELEMENTOR_PRO_SOURCE = "Elementor Pro Changelog"
    PAFE_PRO_PLUGIN_FILE = "piotnet-addons-for-elementor-pro/piotnet-addons-for-elementor-pro.php"
    PAFE_PRO_CHANGELOG_URL = "https://pafe.piotnet.com/change-log/"
    PAFE_PRO_SOURCE = "PAFE Pro Changelog"
    CROCOBLOCK_CHANGELOG_URL = "https://crocoblock.com/wp-content/uploads/jet-changelog/last-updates.json"
    CROCOBLOCK_CHANGELOG_SOURCE = "Crocoblock Changelog"
    CROCOBLOCK_CHANGELOG_SLUGS = {
        "jet-appointment": "jet-appointments-booking",
        "jet-compare-wishlist": "jet-cw",
        "jet-style-manager": "jet-styles-manager",
    }
    PROVIDER_CATALOG_SOURCES = frozenset(
        {"Crocoblock Jet Dashboard", CROCOBLOCK_CHANGELOG_SOURCE, ELEMENTOR_PRO_SOURCE, PAFE_PRO_SOURCE}
    )
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

    def refresh_for_inventory(
        self,
        items: Iterable[Any],
        *,
        force: bool = False,
        max_age: timedelta | None = None,
        progress_callback: Callable[[dict[str, int]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, int]:
        candidates = self._collect_candidates(items)
        if not candidates:
            return {
                "total": 0,
                "checked": 0,
                "completed": 0,
                "cached": 0,
                "wordpress_org": 0,
                "elementor_pro": 0,
                "pafe_pro": 0,
                "provider_offer": 0,
                "unavailable": 0,
                "failed": 0,
            }

        existing = self.get_cached(candidates)
        now = datetime.now(UTC)
        stale_candidates = {
            plugin_file: candidate
            for plugin_file, candidate in candidates.items()
            if self._needs_catalog_refresh(
                candidate=candidate,
                record=existing.get(plugin_file),
                force=force,
                now=now,
                max_age=max_age,
            )
        }

        summary = {
            "total": len(candidates),
            "checked": len(stale_candidates),
            "completed": 0,
            "cached": len(candidates) - len(stale_candidates),
            "wordpress_org": 0,
            "elementor_pro": 0,
            "pafe_pro": 0,
            "provider_offer": 0,
            "unavailable": 0,
            "failed": 0,
        }
        if progress_callback is not None:
            progress_callback(summary.copy())

        catalog_results: dict[str, tuple[str | None, str | None]] = {}
        elementor_candidates = [
            candidate
            for candidate in stale_candidates.values()
            if self._is_elementor_pro_plugin(candidate.plugin_file)
        ]
        cancellation_requested = bool(should_cancel and should_cancel())
        if elementor_candidates and not cancellation_requested:
            version, error = self._fetch_elementor_pro_version()
            for candidate in elementor_candidates:
                catalog_results[candidate.plugin_file] = (version, error)
                summary["completed"] += 1
                if version:
                    summary["elementor_pro"] += 1
                elif self._has_provider_catalog(existing.get(candidate.plugin_file)):
                    summary["provider_offer"] += 1
                else:
                    summary["unavailable"] += 1
                    if error and error != "elementor_pro_version_missing":
                        summary["failed"] += 1
                if progress_callback is not None:
                    progress_callback(summary.copy())

        pafe_candidates = [
            candidate
            for candidate in stale_candidates.values()
            if self._is_pafe_pro_plugin(candidate.plugin_file)
        ]
        if pafe_candidates and not cancellation_requested:
            version, error = self._fetch_pafe_pro_version()
            for candidate in pafe_candidates:
                catalog_results[candidate.plugin_file] = (version, error)
                summary["completed"] += 1
                if version:
                    summary["pafe_pro"] += 1
                elif self._has_provider_catalog(existing.get(candidate.plugin_file)):
                    summary["provider_offer"] += 1
                else:
                    summary["unavailable"] += 1
                    if error and error != "pafe_pro_version_missing":
                        summary["failed"] += 1
                if progress_callback is not None:
                    progress_callback(summary.copy())

        wordpress_org_candidates = [
            candidate
            for candidate in stale_candidates.values()
            if not self._is_elementor_pro_plugin(candidate.plugin_file)
            and not self._is_pafe_pro_plugin(candidate.plugin_file)
        ]
        with ThreadPoolExecutor(max_workers=self.MAX_CONCURRENT_REQUESTS) as executor:
            candidates_iter = iter(wordpress_org_candidates)
            futures: dict[Any, str] = {}
            if not cancellation_requested:
                for _ in range(self.MAX_CONCURRENT_REQUESTS):
                    try:
                        candidate = next(candidates_iter)
                    except StopIteration:
                        break
                    futures[executor.submit(self._fetch_wordpress_org_version, candidate.plugin_file)] = candidate.plugin_file
            while futures:
                future = next(as_completed(futures))
                plugin_file = futures.pop(future)
                try:
                    catalog_results[plugin_file] = future.result()
                except Exception:
                    catalog_results[plugin_file] = (None, "wordpress_org_request_failed")
                summary["completed"] += 1
                version, error = catalog_results[plugin_file]
                if version:
                    summary["wordpress_org"] += 1
                elif self._has_provider_catalog(existing.get(plugin_file)):
                    summary["provider_offer"] += 1
                else:
                    summary["unavailable"] += 1
                    if error and error != "wordpress_org_not_found":
                        summary["failed"] += 1
                if progress_callback is not None:
                    progress_callback(summary.copy())

                cancellation_requested = cancellation_requested or bool(should_cancel and should_cancel())
                if cancellation_requested:
                    continue
                try:
                    candidate = next(candidates_iter)
                except StopIteration:
                    continue
                futures[executor.submit(self._fetch_wordpress_org_version, candidate.plugin_file)] = candidate.plugin_file

        checked_at = now
        for plugin_file, (version, error) in catalog_results.items():
            if version:
                official_version = version
                if self._is_elementor_pro_plugin(plugin_file):
                    source = self.ELEMENTOR_PRO_SOURCE
                elif self._is_pafe_pro_plugin(plugin_file):
                    source = self.PAFE_PRO_SOURCE
                else:
                    source = "WordPress.org"
                last_error = None
            else:
                record = existing.get(plugin_file)
                if self._has_provider_catalog(record):
                    # A temporary provider error must not replace the last verified catalog value.
                    continue
                official_version = None
                source = "No public or provider catalog available"
                last_error = error

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
    def _is_fresh(
        record: PluginOfficialVersion | None,
        *,
        now: datetime,
        max_age: timedelta | None,
    ) -> bool:
        if record is None or record.checked_at is None or max_age is None:
            return False
        if record.source.startswith("Site update provider:"):
            return False
        checked_at = record.checked_at if record.checked_at.tzinfo is not None else record.checked_at.replace(tzinfo=UTC)
        return now - checked_at <= max_age

    @classmethod
    def _needs_catalog_refresh(
        cls,
        *,
        candidate: OfficialVersionCandidate,
        record: PluginOfficialVersion | None,
        force: bool,
        now: datetime,
        max_age: timedelta | None,
    ) -> bool:
        if force:
            return True
        if cls._is_elementor_pro_plugin(candidate.plugin_file) and (record is None or record.source != cls.ELEMENTOR_PRO_SOURCE):
            return True
        if cls._is_pafe_pro_plugin(candidate.plugin_file) and (record is None or record.source != cls.PAFE_PRO_SOURCE):
            return True
        return not cls._is_fresh(record, now=now, max_age=max_age)

    @classmethod
    def _has_provider_catalog(cls, record: PluginOfficialVersion | None) -> bool:
        return bool(record and record.official_version and record.source in cls.PROVIDER_CATALOG_SOURCES)

    def record_provider_versions(self, versions: Iterable[Any], *, source: str) -> int:
        """Persist the newest verified provider catalog version for each plugin."""
        grouped: dict[str, list[str]] = {}
        for entry in versions:
            if not isinstance(entry, dict):
                continue
            plugin_file = str(entry.get("plugin_file", "")).strip()
            version = str(entry.get("version", "")).strip()
            if plugin_file and version:
                grouped.setdefault(plugin_file, []).append(version)

        if not grouped:
            return 0

        existing = self.get_cached(grouped)
        checked_at = datetime.now(UTC)
        for plugin_file, offered_versions in grouped.items():
            record = existing.get(plugin_file)
            official_version = self._highest_version(offered_versions)
            if record is None:
                self.db.add(
                    PluginOfficialVersion(
                        plugin_file=plugin_file,
                        official_version=official_version,
                        source=source[:128],
                        checked_at=checked_at,
                        last_error=None,
                    )
                )
                continue
            record.official_version = official_version
            record.source = source[:128]
            record.checked_at = checked_at
            record.last_error = None

        self.db.flush()
        return len(grouped)

    @classmethod
    def fetch_crocoblock_changelog_versions(cls, plugin_files: Iterable[str]) -> CrocoblockChangelogLookup:
        """Fetch the public Crocoblock changelog once and map it to installed Jet plugins."""
        requested = {
            normalized
            for plugin_file in plugin_files
            if (normalized := plugin_file.strip().replace("\\", "/")) and cls._crocoblock_changelog_slug(normalized)
        }
        if not requested:
            return CrocoblockChangelogLookup(versions={}, requested=0)

        request = Request(
            cls.CROCOBLOCK_CHANGELOG_URL,
            headers={"User-Agent": "Kosmos-Hub/0.1 (+https://kosmos-hub.31-70-92-95.sslip.io)"},
        )
        try:
            with urlopen(request, timeout=cls.REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310: fixed official Crocoblock endpoint.
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return CrocoblockChangelogLookup(versions={}, requested=len(requested), error=f"crocoblock_changelog_http_{exc.code}")
        except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            return CrocoblockChangelogLookup(versions={}, requested=len(requested), error="crocoblock_changelog_request_failed")

        published_versions: dict[str, str] = {}
        if isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                slug = str(entry.get("slug", "")).strip()
                version = cls._parse_crocoblock_changelog_version(str(entry.get("name", "")))
                if slug and version:
                    published_versions[slug] = version

        versions = {
            plugin_file: published_versions[slug]
            for plugin_file in requested
            if (slug := cls._crocoblock_changelog_slug(plugin_file)) in published_versions
        }
        return CrocoblockChangelogLookup(versions=versions, requested=len(requested))

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

    @classmethod
    def diagnosis(
        cls,
        *,
        current_version: str,
        reported_version: str,
        official_version: str | None,
        official_source: str,
        execution_ready: bool,
        execution_note: str,
        is_jet_plugin: bool,
    ) -> tuple[str, str, str]:
        """Explain the evidence state without attempting an update."""
        if not official_version:
            return (
                "official-unavailable",
                "Official version unavailable",
                "No public or authorized provider catalog returned a version, so this plugin cannot be compared yet.",
            )

        if reported_version and reported_version != official_version:
            return (
                "provider-conflict",
                "Provider information conflicts",
                f"The site offers {reported_version}, while {official_source} reports {official_version}. The Hub will not update until the provider data agrees.",
            )

        if current_version == official_version:
            return (
                "aligned",
                "Versions match",
                "The installed version matches the verified official version. No action is needed.",
            )

        if reported_version == official_version:
            if execution_ready:
                return (
                    "update-ready",
                    "Verified update ready",
                    f"The site offer matches {official_source} at {official_version}. The plugin update can be started.",
                )
            if is_jet_plugin:
                return (
                    "crocoblock-license-step",
                    "Crocoblock license step",
                    f"The site offer matches Jet Dashboard at {official_version}. Before updating, the Hub activates the stored Crocoblock license and rechecks the package. It installs only if the provider confirms that package.",
                )
            return (
                "provider-package-unavailable",
                "Provider package unavailable",
                execution_note or "The site reports the verified version, but the provider has not supplied an authorized download package.",
            )

        if current_version and cls._version_key(current_version) > cls._version_key(official_version):
            return (
                "site-newer-than-reference",
                "Site version is newer",
                f"The site has {current_version}, newer than the {official_version} reported by {official_source}. This may be a beta or a delayed provider catalog; no update will run.",
            )

        if is_jet_plugin:
            return (
                "crocoblock-offer-missing",
                "Crocoblock offer missing",
                f"Jet Dashboard confirms {official_version}, but this site returned no WordPress update offer or package. The Hub will not update it yet.",
            )
        return (
            "site-offer-missing",
            "Site offer missing",
            f"{official_source} confirms {official_version}, but the site returned no update offer. The Hub will not update it until the provider exposes one.",
        )

    def _collect_candidates(self, items: Iterable[Any]) -> dict[str, OfficialVersionCandidate]:
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
                if not plugin_file:
                    continue
                plugin_files.add(plugin_file)

        return {
            plugin_file: OfficialVersionCandidate(plugin_file=plugin_file)
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

    def _fetch_elementor_pro_version(self) -> tuple[str | None, str | None]:
        request = Request(
            self.ELEMENTOR_PRO_CHANGELOG_URL,
            headers={"User-Agent": "Kosmos-Hub/0.1 (+https://kosmos-hub.31-70-92-95.sslip.io)"},
        )
        try:
            with urlopen(request, timeout=self.REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310: fixed official Elementor endpoint.
                changelog = response.read().decode("utf-8")
        except HTTPError as exc:
            return None, f"elementor_pro_http_{exc.code}"
        except (URLError, TimeoutError, UnicodeDecodeError):
            return None, "elementor_pro_request_failed"

        version = self._parse_elementor_pro_version(changelog)
        return (version, None) if version else (None, "elementor_pro_version_missing")

    def _fetch_pafe_pro_version(self) -> tuple[str | None, str | None]:
        request = Request(
            self.PAFE_PRO_CHANGELOG_URL,
            headers={"User-Agent": "Kosmos-Hub/0.1 (+https://kosmos-hub.31-70-92-95.sslip.io)"},
        )
        try:
            with urlopen(request, timeout=self.REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310: fixed official PAFE endpoint.
                changelog = response.read().decode("utf-8")
        except HTTPError as exc:
            return None, f"pafe_pro_http_{exc.code}"
        except (URLError, TimeoutError, UnicodeDecodeError):
            return None, "pafe_pro_request_failed"

        version = self._parse_pafe_pro_version(changelog)
        return (version, None) if version else (None, "pafe_pro_version_missing")

    @staticmethod
    def _parse_elementor_pro_version(changelog: str) -> str | None:
        for heading in re.findall(r"<h[1-6][^>]*>(.*?)</h[1-6]>", changelog, flags=re.IGNORECASE | re.DOTALL):
            normalized = re.sub(r"<[^>]+>", " ", heading)
            match = re.search(r"\b(\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+)?)\s*[-–—]\s*\d{4}-\d{2}-\d{2}\b", normalized)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _parse_pafe_pro_version(changelog: str) -> str | None:
        match = re.search(
            r"\[PRO\]\s*(\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+)?)\s*\(\d{4}/\d{2}/\d{2}\)",
            changelog,
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else None

    @staticmethod
    def _parse_crocoblock_changelog_version(name: str) -> str | None:
        match = re.search(r"\b(\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+)?)\s*$", name.strip())
        return match.group(1) if match else None

    @classmethod
    def _crocoblock_changelog_slug(cls, plugin_file: str) -> str:
        directory = plugin_file.partition("/")[0].casefold()
        if not directory.startswith("jet-"):
            return ""
        return cls.CROCOBLOCK_CHANGELOG_SLUGS.get(directory, directory)

    @classmethod
    def _is_elementor_pro_plugin(cls, plugin_file: str) -> bool:
        return plugin_file.strip().replace("\\", "/").casefold() == cls.ELEMENTOR_PRO_PLUGIN_FILE

    @classmethod
    def _is_pafe_pro_plugin(cls, plugin_file: str) -> bool:
        return plugin_file.strip().replace("\\", "/").casefold() == cls.PAFE_PRO_PLUGIN_FILE

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
