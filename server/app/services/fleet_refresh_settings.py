from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.fleet_refresh_settings import FleetRefreshSettings
from app.models.hub_user import HubUser


class FleetRefreshSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class FleetRefreshRuntimeSettings:
    site_status_max_age_minutes: int = 15
    official_version_max_age_hours: int = 24
    max_parallel_site_checks: int = 5
    max_parallel_direct_updates: int = 5
    auto_refresh_enabled: bool = True
    auto_refresh_interval_hours: int = 24
    auto_refresh_time: str = "03:00"


class FleetRefreshSettingsService:
    """Stores bounded refresh tuning in the Hub instead of server environment files."""

    MIN_STATUS_MAX_AGE_MINUTES = 1
    MAX_STATUS_MAX_AGE_MINUTES = 180
    MIN_OFFICIAL_VERSION_MAX_AGE_HOURS = 1
    MAX_OFFICIAL_VERSION_MAX_AGE_HOURS = 168
    MIN_PARALLEL_SITE_CHECKS = 1
    MAX_PARALLEL_SITE_CHECKS = 6
    MIN_PARALLEL_DIRECT_UPDATES = 1
    MAX_PARALLEL_DIRECT_UPDATES = 10
    MIN_AUTO_REFRESH_INTERVAL_HOURS = 24
    MAX_AUTO_REFRESH_INTERVAL_HOURS = 168

    def __init__(self, *, db: Session):
        self.db = db

    def get_runtime_settings(self) -> FleetRefreshRuntimeSettings:
        config = self.db.get(FleetRefreshSettings, 1)
        if config is None:
            return FleetRefreshRuntimeSettings()
        return FleetRefreshRuntimeSettings(
            site_status_max_age_minutes=config.site_status_max_age_minutes,
            official_version_max_age_hours=config.official_version_max_age_hours,
            max_parallel_site_checks=config.max_parallel_site_checks,
            max_parallel_direct_updates=config.max_parallel_direct_updates,
            auto_refresh_enabled=config.auto_refresh_enabled,
            auto_refresh_interval_hours=config.auto_refresh_interval_hours,
            auto_refresh_time=config.auto_refresh_time,
        )

    def configure(
        self,
        *,
        actor: HubUser,
        site_status_max_age_minutes: int,
        official_version_max_age_hours: int,
        max_parallel_site_checks: int,
        max_parallel_direct_updates: int,
        auto_refresh_enabled: bool,
        auto_refresh_interval_hours: int,
        auto_refresh_time: str,
    ) -> FleetRefreshSettings:
        if actor.role != "admin":
            raise FleetRefreshSettingsError("Only Hub administrators can change fleet refresh settings.")
        self._validate(
            site_status_max_age_minutes=site_status_max_age_minutes,
            official_version_max_age_hours=official_version_max_age_hours,
            max_parallel_site_checks=max_parallel_site_checks,
            max_parallel_direct_updates=max_parallel_direct_updates,
            auto_refresh_interval_hours=auto_refresh_interval_hours,
            auto_refresh_time=auto_refresh_time,
        )

        config = self.db.get(FleetRefreshSettings, 1)
        if config is None:
            config = FleetRefreshSettings(id=1)
            self.db.add(config)
        config.site_status_max_age_minutes = site_status_max_age_minutes
        config.official_version_max_age_hours = official_version_max_age_hours
        config.max_parallel_site_checks = max_parallel_site_checks
        config.max_parallel_direct_updates = max_parallel_direct_updates
        config.auto_refresh_enabled = auto_refresh_enabled
        config.auto_refresh_interval_hours = auto_refresh_interval_hours
        config.auto_refresh_time = auto_refresh_time
        config.configured_by_user_id = actor.id
        config.configured_at = datetime.now(UTC)
        self.db.flush()
        return config

    @classmethod
    def _validate(
        cls,
        *,
        site_status_max_age_minutes: int,
        official_version_max_age_hours: int,
        max_parallel_site_checks: int,
        max_parallel_direct_updates: int,
        auto_refresh_interval_hours: int,
        auto_refresh_time: str,
    ) -> None:
        if not cls.MIN_STATUS_MAX_AGE_MINUTES <= site_status_max_age_minutes <= cls.MAX_STATUS_MAX_AGE_MINUTES:
            raise FleetRefreshSettingsError(
                f"Site status cache must be between {cls.MIN_STATUS_MAX_AGE_MINUTES} and {cls.MAX_STATUS_MAX_AGE_MINUTES} minutes."
            )
        if not cls.MIN_OFFICIAL_VERSION_MAX_AGE_HOURS <= official_version_max_age_hours <= cls.MAX_OFFICIAL_VERSION_MAX_AGE_HOURS:
            raise FleetRefreshSettingsError(
                "Official version cache must be between "
                f"{cls.MIN_OFFICIAL_VERSION_MAX_AGE_HOURS} and {cls.MAX_OFFICIAL_VERSION_MAX_AGE_HOURS} hours."
            )
        if not cls.MIN_PARALLEL_SITE_CHECKS <= max_parallel_site_checks <= cls.MAX_PARALLEL_SITE_CHECKS:
            raise FleetRefreshSettingsError(
                f"Parallel site checks must be between {cls.MIN_PARALLEL_SITE_CHECKS} and {cls.MAX_PARALLEL_SITE_CHECKS}."
            )
        if not cls.MIN_PARALLEL_DIRECT_UPDATES <= max_parallel_direct_updates <= cls.MAX_PARALLEL_DIRECT_UPDATES:
            raise FleetRefreshSettingsError(
                "Parallel direct updates must be between "
                f"{cls.MIN_PARALLEL_DIRECT_UPDATES} and {cls.MAX_PARALLEL_DIRECT_UPDATES}."
            )
        if not cls.MIN_AUTO_REFRESH_INTERVAL_HOURS <= auto_refresh_interval_hours <= cls.MAX_AUTO_REFRESH_INTERVAL_HOURS:
            raise FleetRefreshSettingsError(
                "Automatic refresh interval must be between "
                f"{cls.MIN_AUTO_REFRESH_INTERVAL_HOURS} and {cls.MAX_AUTO_REFRESH_INTERVAL_HOURS} hours."
            )
        if auto_refresh_interval_hours % 24:
            raise FleetRefreshSettingsError("Automatic refresh interval must be a whole number of days (24, 48, ..., 168 hours).")
        try:
            datetime.strptime(auto_refresh_time, "%H:%M")
        except ValueError as exc:
            raise FleetRefreshSettingsError("Automatic refresh time must use the HH:MM format.") from exc
