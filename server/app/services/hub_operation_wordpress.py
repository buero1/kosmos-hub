"""Automatically expose the common remote-action catalog to the Hub agent."""
from functools import partial

from app.services.hub_operations import HubOperation, HubOperationInputField as Field, HubQuery, register_operation, register_query
from app.services.wordpress_remote_catalog import ACTIONS
from app.services.wordpress_jobs import enqueue, job_status, cancel_job
from app.services.wordpress_readers import read_users, read_backups, update_options, read_runs, read_plugin_catalog, read_deletion_batch, read_update_snapshot


for spec in ACTIONS.values():
    register_operation(HubOperation(key=spec.key, module="websites", label=spec.label,
        description=spec.label + ". Identische Fachfunktion wie die Hub-Maske; nach Bestaetigung als dauerhafter Auftrag.",
        input_guide="Konkrete IDs/Auswahlschluessel aus den Lesezugriffen verwenden. Bestehende Sicherheitspruefungen bleiben aktiv. queued/submitted bedeutet nicht fertig; wordpress.jobs.read und Wartungslaeufe nachlesen. Unklare Ergebnisse nicht automatisch wiederholen.",
        input_fields=spec.fields, defaults=spec.defaults,
        preview_fields=tuple((field.name, field.label) for field in spec.fields() if field.name != "password"),
        execute=partial(enqueue, key=spec.key), result_fields=(("job_id", "WordPress-Auftrags-ID"), ("status", "Auftragsstatus"))))

JOB_FIELDS = (Field("job_id", "WordPress-Auftrags-ID", required=True, max_length=18),)
register_query(HubQuery("wordpress.jobs.read", "Status eines bestaetigten WordPress-Auftrags und seiner Wartungslaeufe lesen. submitted ist nur die Uebergabe, uncertain erfordert Pruefung ohne Wiederholung.", JOB_FIELDS, job_status))
register_operation(HubOperation(key="wordpress.jobs.cancel", module="websites", label="WordPress-Auftrag abbrechen",
    description="Noch nicht gestarteten WordPress-Auftrag abbrechen.", input_guide="job_id aus Auftrag verwenden.",
    input_fields=lambda: JOB_FIELDS, preview_fields=(("job_id", "Auftrag"),), execute=cancel_job))

READ_FIELDS = (Field("site_id", "Website-ID", required=True, max_length=18), Field("offset", "Seitenbeginn"))
for key, label, callback in (
    ("wordpress.users.list", "Gespeicherte WordPress-Benutzer fuer konkrete Aktionen lesen. Nur Admin; kein Fernzugriff.", read_users),
    ("wordpress.backups.list", "Gespeicherte Backup-Auswahl inkl. Identitaet/Schutzstatus lesen. Kein Fernzugriff.", read_backups),
    ("wordpress.updates.options", "Update-Auswahlschluessel und Versionen wie in der Hub-Maske lesen. Gespeicherter Bestand.", update_options),
    ("wordpress.updates.read", "Gespeicherten Update-Snapshot mit Erfassungszeit lesen. Kein Fernzugriff.", read_update_snapshot),
    ("wordpress.runs.list", "Wartungslaeufe, Batch-IDs und Fortschritt einer Website lesen.", read_runs),
):
    register_query(HubQuery(key, label, READ_FIELDS, callback))
register_query(HubQuery("wordpress.plugins.catalog", "WordPress.org-Katalog wie in der Installationsmaske durchsuchen. Externe Leseabfrage, installiert nichts.",
    (Field("search", "Suchbegriff"), Field("browse", "Bereich", options=(("popular", "Beliebt"), ("recommended", "Empfohlen"), ("new", "Neu"))), Field("page", "Seite 1-100")), read_plugin_catalog))
register_query(HubQuery("wordpress.users.deletion_read", "Vorbereitete Benutzerloeschung mit Inhaltszuordnung, erlaubten Ersatzadministratoren und Fortschritt lesen.",
    (Field("batch_id", "Loeschlauf-ID", required=True, max_length=18), Field("offset", "Seitenbeginn")), read_deletion_batch))
