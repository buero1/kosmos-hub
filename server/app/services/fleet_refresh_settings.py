from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.fleet_refresh_settings import FleetRefreshSettings
from app.models.hub_user import HubUser


class FleetRefreshSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class FleetRefreshRuntimeSettings:
    max_parallel_site_checks: int = 5
    max_parallel_direct_updates: int = 5
    auto_refresh_enabled: bool = True
    auto_refresh_interval_hours: int = 24
    auto_refresh_time: str = "03:00"
    auto_refresh_next_run_at: datetime | None = None


class FleetRefreshSettingsService:
    """Stores bounded refresh tuning in the Hub instead of server environment files."""

    MIN_PARALLEL_SITE_CHECKS = 1
    MAX_PARALLEL_SITE_CHECKS = 6
    MIN_PARALLEL_DIRECT_UPDATES = 1
    MAX_PARALLEL_DIRECT_UPDATES = 10
    MIN_AUTO_REFRESH_INTERVAL_HOURS = 24
    MAX_AUTO_REFRESH_INTERVAL_HOURS = 168
    BERLIN_TIMEZONE = ZoneInfo("Europe/Berlin")

    def __init__(self, *, db: Session):
        self.db = db

    def get_runtime_settings(self) -> FleetRefreshRuntimeSettings:
        config = self.db.get(FleetRefreshSettings, 1)
        if config is None:
            return FleetRefreshRuntimeSettings()
        return FleetRefreshRuntimeSettings(
            max_parallel_site_checks=config.max_parallel_site_checks,
            max_parallel_direct_updates=config.max_parallel_direct_updates,
            auto_refresh_enabled=config.auto_refresh_enabled,
            auto_refresh_interval_hours=config.auto_refresh_interval_hours,
            auto_refresh_time=config.auto_refresh_time,
            auto_refresh_next_run_at=config.auto_refresh_next_run_at,
        )

    def configure(
        self,
        *,
        actor: HubUser,
        max_parallel_site_checks: int,
        max_parallel_direct_updates: int,
        auto_refresh_enabled: bool,
        auto_refresh_interval_hours: int,
        auto_refresh_time: str,
        now: datetime | None = None,
    ) -> FleetRefreshSettings:
        if actor.role != "admin":
            raise FleetRefreshSettingsError("Only Hub administrators can change fleet refresh settings.")
        self._validate(
            max_parallel_site_checks=max_parallel_site_checks,
            max_parallel_direct_updates=max_parallel_direct_updates,
            auto_refresh_interval_hours=auto_refresh_interval_hours,
            auto_refresh_time=auto_refresh_time,
        )

        config = self.db.get(FleetRefreshSettings, 1)
        schedule_changed = config is None or any(
            (
                config.auto_refresh_enabled != auto_refresh_enabled,
                config.auto_refresh_interval_hours != auto_refresh_interval_hours,
                config.auto_refresh_time != auto_refresh_time,
            )
        )
        if config is None:
            config = FleetRefreshSettings(id=1)
            self.db.add(config)
        config.max_parallel_site_checks = max_parallel_site_checks
        config.max_parallel_direct_updates = max_parallel_direct_updates
        config.auto_refresh_enabled = auto_refresh_enabled
        config.auto_refresh_interval_hours = auto_refresh_interval_hours
        config.auto_refresh_time = auto_refresh_time
        config.configured_by_user_id = actor.id
        configured_at = self._as_utc(now or datetime.now(UTC))
        config.configured_at = configured_at
        if not auto_refresh_enabled:
            config.auto_refresh_next_run_at = None
        elif schedule_changed:
            config.auto_refresh_next_run_at = self._next_scheduled_run_at(
                auto_refresh_time=auto_refresh_time,
                now=configured_at,
            )
        self.db.flush()
        return config

    def ensure_auto_refresh_schedule(self, *, now: datetime) -> FleetRefreshRuntimeSettings:
        """Persist an initial due time for older configuration rows exactly once."""
        config = self.db.get(FleetRefreshSettings, 1)
        if config is None:
            config = FleetRefreshSettings(id=1)
            self.db.add(config)
            self.db.flush()
        if config.auto_refresh_enabled and config.auto_refresh_next_run_at is None:
            config.auto_refresh_next_run_at = self._next_scheduled_run_at(
                auto_refresh_time=config.auto_refresh_time,
                now=now,
            )
            self.db.flush()
        return self.get_runtime_settings()

    def advance_auto_refresh_schedule(self, *, now: datetime) -> None:
        """Move the persisted due time forward without drifting the Berlin wall-clock time."""
        config = self.db.get(FleetRefreshSettings, 1)
        if config is None or not config.auto_refresh_enabled or config.auto_refresh_next_run_at is None:
            return

        next_run_at = self._as_utc(config.auto_refresh_next_run_at)
        interval_days = config.auto_refresh_interval_hours // 24
        now_utc = self._as_utc(now)
        while next_run_at <= now_utc:
            next_date = next_run_at.astimezone(self.BERLIN_TIMEZONE).date() + timedelta(days=interval_days)
            next_run_at = self._scheduled_datetime_for_date(
                date_value=next_date,
                auto_refresh_time=config.auto_refresh_time,
            )
        config.auto_refresh_next_run_at = next_run_at
        self.db.flush()

    @classmethod
    def _next_scheduled_run_at(cls, *, auto_refresh_time: str, now: datetime) -> datetime:
        berlin_now = cls._as_utc(now).astimezone(cls.BERLIN_TIMEZONE)
        candidate = cls._scheduled_datetime_for_date(
            date_value=berlin_now.date(),
            auto_refresh_time=auto_refresh_time,
        )
        if candidate <= berlin_now.astimezone(UTC):
            candidate = cls._scheduled_datetime_for_date(
                date_value=berlin_now.date() + timedelta(days=1),
                auto_refresh_time=auto_refresh_time,
            )
        return candidate

    @classmethod
    def _scheduled_datetime_for_date(cls, *, date_value, auto_refresh_time: str) -> datetime:
        scheduled_time = datetime.strptime(auto_refresh_time, "%H:%M").time()
        return datetime.combine(date_value, scheduled_time, tzinfo=cls.BERLIN_TIMEZONE).astimezone(UTC)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @classmethod
    def _validate(
        cls,
        *,
        max_parallel_site_checks: int,
        max_parallel_direct_updates: int,
        auto_refresh_interval_hours: int,
        auto_refresh_time: str,
    ) -> None:
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
