"""One allowlisted domain catalog for UI execution and queued agent execution."""
from dataclasses import dataclass
from datetime import UTC, datetime
import inspect
import json
from typing import get_origin, get_args, get_type_hints

from app.models.plugin_installation_package import PluginInstallationPackage
from app.models.maintenance_run import MaintenanceRun
from app.services.hub_operations import HubOperationError, HubOperationInputField as Field
from app.services.hub_operation_websites import website_site
from app.services.hub_record_access import require_actor
from app.services.maintenance_runs import MaintenanceRunService
from app.services.plugin_installation_packages import PluginInstallationPackageService
from app.services.site_backups import SiteBackupService
from app.services.site_updates import SiteUpdateService
from app.services.site_users import SiteUserService
from app.services.site_inventory import SiteInventoryService
from app.services.plugin_auto_updates import PluginAutoUpdateService
from app.services.user_deletion_batches import UserDeletionBatchService


def install_plugin(service, *, site_ids: list[int], actor: str, wordpress_org_slug: str = "",
                   package_id: int | None = None, activate: bool = False, replace_existing: bool = False):
    if bool(wordpress_org_slug.strip()) == bool(package_id):
        raise HubOperationError("Entweder WordPress.org-Slug oder ID eines bereits geprueften ZIP-Pakets angeben.")
    if package_id:
        package = service.db.get(PluginInstallationPackage, package_id)
        if package is None or (package.expires_at and package.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)):
            raise HubOperationError("Das gepruefte Plugin-Paket fehlt oder ist abgelaufen.")
    else:
        package = PluginInstallationPackageService(db=service.db).prepare_wordpress_org_plugin(slug=wordpress_org_slug)
    return MaintenanceRunService(db=service.db, cipher=service.cipher).start_plugin_installations(
        site_ids=site_ids, package=package, activate=activate, replace_existing=replace_existing, actor=actor)


@dataclass(frozen=True)
class RemoteAction:
    key: str
    label: str
    service_class: object
    method: str
    admin_only: bool = True

    @property
    def function(self):
        return install_plugin if self.service_class is None else getattr(self.service_class, self.method)

    def parameters(self):
        return {name: param for name, param in inspect.signature(self.function).parameters.items()
                if name not in {"self", "service", "actor", "refresh_inventory"}}

    def fields(self):
        hints = get_type_hints(self.function)
        fields = []
        for name, param in self.parameters().items():
            options = ()
            if hints.get(name) is bool:
                options = (("true", "Ja"), ("false", "Nein"))
            if name == "role":
                options = tuple((role, role) for role in SiteUserService.ROLE_OPTIONS)
            fields.append(Field(name, name.replace("_", " "), required=param.default is inspect.Parameter.empty,
                options=options, max_length=40_000 if get_origin(hints.get(name)) is list else 4096,
                encoding="JSON array" if get_origin(hints.get(name)) is list else ""))
        return tuple(fields)

    def defaults(self, _values):
        return {name: encode(param.default) for name, param in self.parameters().items()
                if param.default is not inspect.Parameter.empty and param.default is not None}


def encode(value):
    return json.dumps(value) if isinstance(value, (list, dict, bool)) else str(value)


ACTIONS = {action.key: action for action in (
    RemoteAction("wordpress.inventory.refresh", "Website-Inventar aktualisieren", SiteInventoryService, "refresh_site_state", False),
    RemoteAction("wordpress.capabilities.refresh", "Bridge-Faehigkeiten aktualisieren", SiteInventoryService, "refresh_site_inventory", False),
    RemoteAction("wordpress.users.refresh", "WordPress-Benutzer aktualisieren", SiteUserService, "refresh_site_users"),
    RemoteAction("wordpress.users.create", "WordPress-Benutzer anlegen", SiteUserService, "create_user"),
    RemoteAction("wordpress.users.password", "WordPress-Passwort aendern", SiteUserService, "update_password"),
    RemoteAction("wordpress.users.role", "WordPress-Rolle aendern", SiteUserService, "update_role"),
    RemoteAction("wordpress.users.delete", "WordPress-Benutzer loeschen", SiteUserService, "delete_user"),
    RemoteAction("wordpress.users.bulk_create", "WordPress-Benutzer mehrfach anlegen", SiteUserService, "create_users_bulk"),
    RemoteAction("wordpress.users.bulk_password", "WordPress-Passwoerter aendern", SiteUserService, "update_passwords_bulk"),
    RemoteAction("wordpress.users.bulk_role", "WordPress-Rollen aendern", SiteUserService, "update_roles_bulk"),
    RemoteAction("wordpress.users.deletion_prepare", "Benutzerloeschung vorbereiten", UserDeletionBatchService, "prepare_batch"),
    RemoteAction("wordpress.users.deletion_start", "Gepruefte Benutzerloeschung starten", UserDeletionBatchService, "start_batch"),
    RemoteAction("wordpress.users.deletion_cancel", "Benutzerloeschung abbrechen", UserDeletionBatchService, "cancel_batch"),
    RemoteAction("wordpress.updates.refresh", "WordPress-Updates pruefen", SiteUpdateService, "refresh_site_updates", False),
    RemoteAction("wordpress.updates.start", "Ausgewaehlte Updates starten", MaintenanceRunService, "start_direct_updates", False),
    RemoteAction("wordpress.updates.site", "Website-Auswahl aktualisieren", MaintenanceRunService, "start_site_updates", False),
    RemoteAction("wordpress.updates.complete", "Website komplett aktualisieren", MaintenanceRunService, "start_complete_site_update"),
    RemoteAction("wordpress.updates.cancel_batch", "Update-Batch abbrechen", MaintenanceRunService, "cancel_direct_update_batch"),
    RemoteAction("wordpress.updates.cancel_complete", "Komplett-Update abbrechen", MaintenanceRunService, "cancel_complete_site_update"),
    RemoteAction("wordpress.plugins.install", "Plugin installieren", None, "install_plugin"),
    RemoteAction("wordpress.plugins.auto_updates", "Automatische Plugin-Updates sperren (blocked=true) oder Hub-Sperre aufheben (blocked=false, aktiviert keine Automatik)", PluginAutoUpdateService, "set_policy"),
    RemoteAction("wordpress.backups.refresh", "Backup-Bestand pruefen", SiteBackupService, "refresh_site_backup_status", False),
    RemoteAction("wordpress.backups.create", "Backup erstellen", MaintenanceRunService, "start_updraftplus_backup", False),
    RemoteAction("wordpress.backups.delete", "Ausgewaehlte Backups loeschen", MaintenanceRunService, "start_updraftplus_backup_deletion", False),
)}


def prepare_remote(service, key, values):
    spec = ACTIONS.get(key)
    if spec is None:
        raise HubOperationError("Diese WordPress-Aktion ist nicht freigegeben.")
    values = {**spec.defaults(values), **values}
    fields = {field.name: field for field in spec.fields()}
    if set(values) - fields.keys():
        raise HubOperationError("Unbekannte WordPress-Eingaben.")
    hints = get_type_hints(spec.function)
    kwargs = {}
    for name, field in fields.items():
        if name not in values:
            if field.required:
                raise HubOperationError(f"{field.label} fehlt.")
            continue
        value = values[name]
        if not isinstance(value, str) or len(value) > field.max_length:
            raise HubOperationError(f"{field.label}: ungueltige Eingabe.")
        if field.options and value not in dict(field.options):
            raise HubOperationError(f"{field.label}: ungueltige Auswahl.")
        hint = hints.get(name)
        if hint is bool:
            parsed = value == "true"
        elif get_origin(hint) is list:
            try:
                parsed = json.loads(value)
            except ValueError as exc:
                raise HubOperationError(f"{field.label}: JSON-Liste erwartet.") from exc
            item_type = get_args(hint)[0]
            if not isinstance(parsed, list) or not parsed or len(parsed) > 1000 or any(type(item) is not item_type for item in parsed):
                raise HubOperationError(f"{field.label}: ungueltige Liste.")
        elif hint is int or int in get_args(hint):
            if not value or not value.isdecimal() or len(value) > 18 or int(value) < 1:
                raise HubOperationError(f"{field.label}: positive ID erwartet.")
            parsed = int(value)
        else:
            parsed = value
            if field.required and not value.strip():
                raise HubOperationError(f"{field.label} fehlt.")
        kwargs[name] = parsed
    user, _access = require_actor(service, "websites", "edit")
    if spec.admin_only and user.role != "admin":
        raise HubOperationError("Diese WordPress-Verwaltung ist nur fuer Hub-Administratoren verfuegbar.")
    site_ids = set(kwargs.get("site_ids", []))
    if kwargs.get("site_id"):
        site_ids.add(kwargs["site_id"])
    if "selected_keys" in kwargs:
        delimiter = ":" if key.startswith("wordpress.users.") else "|"
        for item in kwargs["selected_keys"]:
            prefix, separator, _rest = item.partition(delimiter)
            if not separator or not prefix.isdecimal() or len(prefix) > 18:
                raise HubOperationError("Ungueltiger Auswahl-Schluessel.")
            site_ids.add(int(prefix))
    if key == "wordpress.updates.cancel_batch":
        runs = MaintenanceRunService(db=service.db, cipher=service.cipher).list_plugin_update_batch(kwargs["batch_id"])
        site_ids.update(run.site_id for run in runs)
    if key == "wordpress.updates.cancel_complete":
        run = service.db.get(MaintenanceRun, kwargs["run_id"])
        if run is not None:
            site_ids.add(run.site_id)
    if key in {"wordpress.users.deletion_start", "wordpress.users.deletion_cancel"}:
        batch = UserDeletionBatchService(db=service.db, cipher=service.cipher).get_batch(kwargs["batch_id"])
        if batch is not None:
            site_ids.update(item.site_id for item in batch.items)
    if not site_ids:
        raise HubOperationError("Mindestens eine konkrete Website auswaehlen.")
    for site_id in site_ids:
        website_site(service, site_id, action="edit")
    if key == "wordpress.plugins.install":
        if bool(kwargs.get("wordpress_org_slug", "").strip()) == bool(kwargs.get("package_id")):
            raise HubOperationError("Entweder WordPress.org-Slug oder ID eines bereits geprueften ZIP-Pakets angeben.")
        if kwargs.get("package_id"):
            package = service.db.get(PluginInstallationPackage, kwargs["package_id"])
            if package is None or (package.expires_at and package.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)):
                raise HubOperationError("Das gepruefte Plugin-Paket fehlt oder ist abgelaufen.")
    if key.startswith("wordpress.users."):
        domain = SiteUserService(db=service.db, cipher=service.cipher)
        if "password" in kwargs:
            domain._validated_password(kwargs["password"])
    if key == "wordpress.plugins.auto_updates":
        PluginAutoUpdateService.validate(kwargs["site_ids"], kwargs["plugin_files"], kwargs["blocked"])
    return spec, kwargs, sorted(site_ids)


def execute_remote(service, key, values):
    """UI and worker enter here; legacy services own their remote transactions."""
    spec, kwargs, _site_ids = prepare_remote(service, key, values)
    if "actor" in inspect.signature(spec.function).parameters:
        kwargs["actor"] = service.actor
    if spec.service_class is None:
        result = spec.function(service, **kwargs)
    else:
        result = getattr(spec.service_class(db=service.db, cipher=service.cipher), spec.method)(**kwargs)
    # Existing pollers remain responsible for the child maintenance run.
    if key in {"wordpress.updates.start", "wordpress.updates.site", "wordpress.plugins.install"}:
        from app.services.maintenance_worker import schedule_pending_direct_updates
        schedule_pending_direct_updates()
    elif key == "wordpress.updates.complete":
        from app.services.maintenance_worker import schedule_pending_complete_site_updates
        schedule_pending_complete_site_updates()
    elif key == "wordpress.users.deletion_start":
        from app.services.maintenance_worker import schedule_pending_user_deletions
        service.after_commit("wordpress-user-deletion", schedule_pending_user_deletions)
    return result


def execute_ui_remote(service, key, **kwargs):
    kwargs.pop("actor", None)  # Identity always comes from the authenticated adapter.
    return execute_remote(service, key, {name: encode(value) for name, value in kwargs.items() if value is not None})
