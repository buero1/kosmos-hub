"""Optional experimental alternative to the stored-inventory Update Workbench.

The normal ``Show updates`` workflow does not call this service. It is exposed
only through the explicit fresh-update option in the site-selector menu, so a
future version can remove or expand this bounded experiment as one unit.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.security import SecretCipher, get_secret_cipher
from app.db.session import SessionLocal
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.fleet_inventory import FleetInventoryService
from app.services.official_plugin_versions import OfficialPluginVersionService
from app.services.site_inventory import SiteInventoryService
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.site_updates import SiteUpdateService


@dataclass(frozen=True)
class FreshUpdateWorkbenchOutcome:
    requested: int
    refreshed: int
    failed_domains: tuple[str, ...]
    official_checked: int

    @property
    def message(self) -> str:
        message = (
            f"Fresh update data was read for {self.refreshed}/{self.requested} selected "
            f"site{'s' if self.requested != 1 else ''}; {self.official_checked} official plugin "
            "catalogue entries were checked."
        )
        if self.failed_domains:
            return f"{message} Could not refresh: {', '.join(self.failed_domains)}."
        return message


class FreshUpdateWorkbenchService:
    """Retained synchronous prototype for a future, narrowly scoped fresh-data flow.

    The Update Workbench uses FleetRefreshService.MODE_FRESH_UPDATES instead,
    so arbitrary selected-site sets run in the persisted background worker.
    """

    MAX_SITES_PER_REQUEST = 12
    MAX_PARALLEL_SITE_CHECKS = 3

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher
        self.repository = SiteRepository(db)

    def refresh_selected_sites(self, site_ids: set[int]) -> FreshUpdateWorkbenchOutcome:
        selected_ids = sorted({site_id for site_id in site_ids if site_id > 0})
        if not selected_ids:
            raise ValueError("Select at least one site before loading fresh updates.")
        if len(selected_ids) > self.MAX_SITES_PER_REQUEST:
            raise ValueError(
                f"The temporary fresh Show updates path supports up to {self.MAX_SITES_PER_REQUEST} sites at once. "
                "Use the background refresh for larger selections."
            )

        sites_by_id = {site.id: site for site in self.repository.list_sites(limit=1000)}
        missing_ids = [site_id for site_id in selected_ids if site_id not in sites_by_id]
        if missing_ids:
            raise ValueError("One or more selected sites no longer exist.")

        refreshed_ids: set[int] = set()
        failed_domains: list[str] = []
        with ThreadPoolExecutor(max_workers=min(self.MAX_PARALLEL_SITE_CHECKS, len(selected_ids))) as executor:
            futures = {
                executor.submit(self._refresh_one_site, site_id): site_id
                for site_id in selected_ids
            }
            for future in as_completed(futures):
                site_id = futures[future]
                try:
                    future.result()
                except SiteMcpProxyError:
                    failed_domains.append(sites_by_id[site_id].domain)
                except Exception:
                    failed_domains.append(sites_by_id[site_id].domain)
                else:
                    refreshed_ids.add(site_id)

        # Worker sessions wrote the fresh snapshots; discard request-local objects first.
        self.db.expire_all()
        official_checked = self._refresh_official_diagnosis(refreshed_ids)
        write_audit_log(
            self.db,
            site=None,
            actor="kosmos-hub",
            source="hub-web",
            action="fresh-update-workbench-view",
            result="ok" if not failed_domains else "partial",
            detail=(
                f"Fresh update workbench request refreshed {len(refreshed_ids)}/{len(selected_ids)} sites; "
                f"checked {official_checked} official plugin catalogues."
            ),
        )
        self.db.commit()
        return FreshUpdateWorkbenchOutcome(
            requested=len(selected_ids),
            refreshed=len(refreshed_ids),
            failed_domains=tuple(sorted(failed_domains, key=str.casefold)),
            official_checked=official_checked,
        )

    def _refresh_one_site(self, site_id: int) -> None:
        # One session per worker keeps the selected websites independent and bounded.
        with SessionLocal() as db:
            cipher = get_secret_cipher()
            SiteInventoryService(db=db, cipher=cipher).refresh_site_state(site_id)
            SiteUpdateService(db=db, cipher=cipher).refresh_site_updates(site_id)

    def _refresh_official_diagnosis(self, site_ids: set[int]) -> int:
        if not site_ids:
            return 0
        inventory = FleetInventoryService(db=self.db, cipher=self.cipher)
        items = [item for item in inventory.list_items(limit=1000) if item.site.id in site_ids]
        summary = OfficialPluginVersionService(db=self.db).refresh_for_inventory(items, force=True)
        return summary["checked"]
