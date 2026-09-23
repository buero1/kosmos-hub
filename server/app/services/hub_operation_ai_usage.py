"""The UI and agent share the same permission-scoped cost reader."""
from app.services.ai_usage import usage_report
from app.services.hub_operations import HubOperationInputField, HubQuery, register_query


def read_usage(service, values):
    result = usage_report(db=service.db, actor=service.actor)
    if values.get("detail") != "recent":
        result.pop("runs", None)
    return result


register_query(HubQuery(key="ai.usage.read",
    description="KI-Verbrauch der letzten 30 Tage: Modelle, Tokens, Kostenschaetzung und einzelne Modellaufrufe. Normale Benutzer sehen nur ihren Verbrauch.",
    input_fields=(HubOperationInputField("detail", "Umfang", options=(("summary", "Zusammenfassung"), ("recent", "Letzte Aufrufe"))),),
    execute=read_usage))
