"""Planning and explicit execution of useful cross-module Hub work."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib import error, request
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.hub_agent import HubAgentAction, HubAgentJob
from app.services.ai_assistant import OPENAI_RESPONSES_URL
from app.services.ai_provider import AiProviderConfigError, AiProviderConfigService
from app.services.customer_activities import CustomerActivityError, CustomerActivityService
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_mailbox_transport import DEFAULT_HUB_MAILBOX_SENDER_EMAIL

MAX_AGENT_REQUEST_LENGTH = 4_000
_ACTION_TYPES = frozenset({"create_contact", "create_task", "create_email_draft"})


class HubAgentError(ValueError):
    """A safe error that can be presented in the Hub interface."""


@dataclass(frozen=True)
class HubAgentCapability:
    key: str
    name: str
    status: str
    description: str


HUB_AGENT_CAPABILITIES = (
    HubAgentCapability(
        key="create_contact",
        name="Kontakte anlegen",
        status="available",
        description="Legt Hub-Kontakte an und kann sie einem eindeutig erkannten Kunden zuordnen.",
    ),
    HubAgentCapability(
        key="create_task",
        name="Aufgaben planen",
        status="available",
        description="Erstellt eine Aufgabe mit Termin, Kundenverknüpfung und Popup- oder E-Mail-Erinnerung.",
    ),
    HubAgentCapability(
        key="create_email_draft",
        name="E-Mail-Entwürfe erstellen",
        status="available",
        description="Erstellt einen bereinigten Entwurf im Hub-Ordner „Entwürfe“, ohne ihn zu versenden.",
    ),
    HubAgentCapability(
        key="email_context",
        name="E-Mails als Kontext verstehen",
        status="planned",
        description="Übernimmt Absender, Inhalt und Anhänge einer ausgewählten E-Mail als Arbeitsgrundlage.",
    ),
    HubAgentCapability(
        key="case_management",
        name="Fälle aus E-Mails bearbeiten",
        status="planned",
        description="Legt Fälle aus einer E-Mail an, verknüpft sie und bereitet Änderungen oder Abschlüsse vor.",
    ),
    HubAgentCapability(
        key="customer_updates",
        name="Kunden- und Kontaktdaten aktualisieren",
        status="planned",
        description="Bereitet Änderungen an vorhandenen Stammdaten aus klaren Nutzeranweisungen vor.",
    ),
    HubAgentCapability(
        key="calendar_management",
        name="Anrufe und Meetings planen",
        status="planned",
        description="Erstellt geplante Anrufe und Meetings mit Termin und Erinnerungen.",
    ),
    HubAgentCapability(
        key="automatic_email_delivery",
        name="E-Mails automatisch versenden",
        status="disabled",
        description="Bleibt deaktiviert. Der Agent kann Entwürfe vorbereiten, der Versand erfolgt weiterhin bewusst durch den Nutzer.",
    ),
)


@dataclass(frozen=True)
class HubAgentActionView:
    id: int
    action_type: str
    title: str
    details: str
    preview_lines: tuple[str, ...]
    status: str
    result_label: str | None
    result_href: str | None
    error: str | None


@dataclass(frozen=True)
class HubAgentJobView:
    id: int
    request_text: str
    summary: str
    response_text: str
    status: str
    created_at: datetime
    actions: tuple[HubAgentActionView, ...]


class HubAgentService:
    """Turns a request into a stored plan, then executes one approved action at a time."""

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher
        self.provider_service = AiProviderConfigService(db=db, cipher=cipher)

    @staticmethod
    def capabilities() -> tuple[HubAgentCapability, ...]:
        return HUB_AGENT_CAPABILITIES

    def plan(self, *, instruction: str, actor: str) -> HubAgentJobView:
        normalized_instruction = self._normalize_instruction(instruction)
        try:
            config, api_key = self.provider_service.get_enabled_openai_api_key()
        except AiProviderConfigError as exc:
            raise HubAgentError(str(exc)) from exc

        try:
            plan = self._create_plan(api_key=api_key, model=config.model, instruction=normalized_instruction)
        except HubAgentError as exc:
            self.provider_service.record_request_error(config, code=str(exc))
            raise
        self.provider_service.record_request_success(config)

        job = HubAgentJob(
            created_by_username=actor,
            status="ready" if plan["actions"] else "completed",
            encrypted_request_json=self._encrypt_json({"instruction": normalized_instruction}),
            encrypted_plan_json=self._encrypt_json({"summary": plan["summary"], "response": plan["response"]}),
        )
        self.db.add(job)
        self.db.flush()
        for index, action in enumerate(plan["actions"]):
            self.db.add(
                HubAgentAction(
                    job_id=job.id,
                    sort_order=index,
                    action_type=action["action_type"],
                    status="proposed",
                    encrypted_payload_json=self._encrypt_json(action),
                )
            )
        self.db.flush()
        return self._job_view(job)

    def list_jobs(self, *, actor: str, limit: int = 15) -> tuple[HubAgentJobView, ...]:
        jobs = self.db.scalars(
            select(HubAgentJob)
            .options(selectinload(HubAgentJob.actions))
            .where(HubAgentJob.created_by_username == actor)
            .order_by(HubAgentJob.created_at.desc(), HubAgentJob.id.desc())
            .limit(limit)
        ).all()
        return tuple(self._job_view(job) for job in jobs)

    def execute_action(self, *, action_id: int, actor: str) -> HubAgentActionView:
        action = self.db.scalar(
            select(HubAgentAction)
            .options(selectinload(HubAgentAction.job))
            .where(HubAgentAction.id == action_id)
        )
        if action is None or action.job.created_by_username != actor:
            raise HubAgentError("Die Agent-Aktion wurde nicht gefunden.")
        if action.status != "proposed":
            raise HubAgentError("Diese Agent-Aktion wurde bereits bearbeitet.")

        payload = self._decrypt_json(action.encrypted_payload_json)
        action.status = "executing"
        self.db.flush()
        try:
            result = self._execute_payload(action_type=action.action_type, payload=payload, actor=actor)
        except (HubAgentError, CustomerActivityError, ValueError) as exc:
            action.status = "failed"
            action.encrypted_result_json = self._encrypt_json({"error": str(exc)})
            self.db.flush()
            self._refresh_job_status(action.job)
            return self._action_view(action)

        action.status = "completed"
        action.encrypted_result_json = self._encrypt_json(result)
        self.db.flush()
        self._refresh_job_status(action.job)
        return self._action_view(action)

    def _create_plan(self, *, api_key: str, model: str, instruction: str) -> dict[str, Any]:
        payload = {
            "model": model,
            "store": False,
            "max_output_tokens": 1_400,
            "parallel_tool_calls": False,
            "tool_choice": {"type": "function", "name": "propose_hub_actions"},
            "tools": [self._proposal_tool_definition()],
            "instructions": (
                "Du bist der Hub-Agent von Kosmos und antwortest ausschließlich auf Deutsch. "
                "Erstelle einen konkreten, aber noch nicht ausgeführten Arbeitsplan. "
                "Du darfst nur drei Aktionstypen vorschlagen: Kontakt im Hub anlegen, Aufgabe anlegen und E-Mail-Entwurf anlegen. "
                "Versende niemals eine E-Mail und behaupte nie, dass etwas bereits umgesetzt wurde. "
                "Nutze nur Tatsachen, die der Nutzer in der Anweisung genannt hat. Erfinde keine Namen, E-Mail-Adressen, Termine, Kunden oder Inhalte. "
                "Wenn Angaben fehlen, erkläre sie im response-Text und schlage keine unvollständige Aktion vor. "
                "Ein Kontakt braucht mindestens Anrede und Nachname. Eine Aufgabe braucht einen exakten Kunden-Namen, ein Datum im Format YYYY-MM-DD und eine Uhrzeit HH:MM. "
                "Ein E-Mail-Entwurf braucht Empfängeradresse, Betreff und sicheren HTML-Inhalt mit einfachen p- und br-Tags. "
                "Heute ist "
                f"{datetime.now(UTC).astimezone(ZoneInfo('Europe/Berlin')).date().isoformat()}. Interpretiere relative Datumsangaben daran und nenne die aufgelösten Daten. "
                "Die Aktion wird anschließend einzeln vom Nutzer bestätigt."
            ),
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"Anweisung:\n{instruction}"}],
                }
            ],
        }
        response_payload = self._create_openai_response(api_key=api_key, payload=payload)
        calls = [
            item
            for item in response_payload.get("output", [])
            if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == "propose_hub_actions"
        ]
        if len(calls) != 1:
            raise HubAgentError("OpenAI konnte keinen nutzbaren Arbeitsplan erzeugen.")
        arguments = calls[0].get("arguments")
        if not isinstance(arguments, str):
            raise HubAgentError("OpenAI hat einen ungültigen Arbeitsplan geliefert.")
        try:
            raw_plan = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise HubAgentError("OpenAI hat einen ungültigen Arbeitsplan geliefert.") from exc
        return self._normalize_plan(raw_plan)

    @staticmethod
    def _proposal_tool_definition() -> dict[str, Any]:
        return {
            "type": "function",
            "name": "propose_hub_actions",
            "description": "Returns the safe plan and its individual proposed Hub actions.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "summary": {"type": "string"},
                    "response": {"type": "string"},
                    "actions": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "action_type": {"type": "string", "enum": sorted(_ACTION_TYPES)},
                                "title": {"type": "string"},
                                "details": {"type": "string"},
                                "input": {
                                    "type": "object",
                                    "additionalProperties": {
                                        "type": "string"
                                    },
                                },
                            },
                            "required": ["action_type", "title", "details", "input"],
                        },
                    },
                },
                "required": ["summary", "response", "actions"],
            },
        }

    @staticmethod
    def _create_openai_response(*, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        http_request = request.Request(
            OPENAI_RESPONSES_URL,
            data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=45) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise HubAgentError(f"OpenAI-Anfrage fehlgeschlagen (HTTP {exc.code}).") from exc
        except error.URLError as exc:
            raise HubAgentError("OpenAI ist gerade nicht erreichbar. Bitte erneut versuchen.") from exc
        except TimeoutError as exc:
            raise HubAgentError("OpenAI hat nicht rechtzeitig geantwortet. Bitte erneut versuchen.") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HubAgentError("OpenAI hat eine nicht lesbare Antwort geliefert.") from exc
        if not isinstance(response_payload, dict):
            raise HubAgentError("OpenAI hat eine nicht lesbare Antwort geliefert.")
        return response_payload

    def _execute_payload(self, *, action_type: str, payload: dict[str, Any], actor: str) -> dict[str, str]:
        input_values = payload.get("input")
        if not isinstance(input_values, dict):
            raise HubAgentError("Die gespeicherten Aktionsdaten sind ungültig.")
        if action_type == "create_contact":
            return self._create_contact(input_values)
        if action_type == "create_task":
            return self._create_task(input_values, actor=actor)
        if action_type == "create_email_draft":
            return self._create_email_draft(input_values)
        raise HubAgentError("Dieser Aktionstyp wird noch nicht unterstützt.")

    def _create_contact(self, values: dict[str, Any]) -> dict[str, str]:
        customer = self._resolve_customer(self._text(values.get("customer_name")), required=False)
        submitted_values = {
            "contact_field__salutation": self._text(values.get("salutation")),
            "contact_field__letter_salutation": self._text(values.get("letter_salutation")),
            "contact_field__first_name": self._text(values.get("first_name")),
            "contact_field__last_name": self._text(values.get("last_name")),
            "contact_field__title": self._text(values.get("title")),
            "contact_field__function": self._text(values.get("function")),
            "contact_field__email": self._text(values.get("email")),
            "contact_field__phone": self._text(values.get("phone")),
            "contact_field__mobile": self._text(values.get("mobile")),
        }
        contact = CustomerDirectoryService(db=self.db, cipher=self.cipher).create_hub_contact(
            customer_id=customer.id if customer is not None else None,
            submitted_values=submitted_values,
        )
        return {"label": "Kontakt öffnen", "href": f"/contacts/{contact.id}"}

    def _create_task(self, values: dict[str, Any], *, actor: str) -> dict[str, str]:
        customer = self._resolve_customer(self._required_text(values.get("customer_name"), "Kunde"), required=True)
        task = CustomerActivityService(db=self.db).schedule_task(
            customer_id=customer.id,
            actor=actor,
            name=self._required_text(values.get("task_name"), "Name der Aufgabe"),
            status="planned",
            due_date=self._required_text(values.get("due_date"), "Datum der Aufgabe"),
            due_time=self._required_text(values.get("due_time"), "Uhrzeit der Aufgabe"),
            reminder_channel=self._text(values.get("reminder_channel")) or "popup",
            reminder_minutes_before=self._text(values.get("reminder_minutes_before")) or "0",
            description=self._text(values.get("task_description")),
        )
        return {"label": "Aufgabe beim Kunden öffnen", "href": f"/customers/{customer.id}#customer-activities", "task_id": str(task.id)}

    def _create_email_draft(self, values: dict[str, Any]) -> dict[str, str]:
        recipient_email = self._required_text(values.get("recipient_email"), "Empfängeradresse")
        if "@" not in recipient_email:
            raise HubAgentError("Die Empfängeradresse des E-Mail-Entwurfs ist ungültig.")
        content = self._required_text(values.get("email_html"), "E-Mail-Inhalt")
        if "<" not in content or ">" not in content:
            content = f"<p>{escape(content)}</p>"
        customer = self._resolve_customer(self._text(values.get("customer_name")), required=False)
        mailbox = HubMailboxService(
            db=self.db,
            cipher=self.cipher,
            public_base_url=get_settings().public_base_url,
        )
        # Agent output follows the same allow-list as user-composed message HTML.
        content = mailbox.communications._sanitized_email_content(content)
        draft = mailbox.save_draft(
            draft_id=None,
            sender_email=DEFAULT_HUB_MAILBOX_SENDER_EMAIL,
            recipient_email=recipient_email,
            recipient_key="",
            recipient_customer_id=customer.id if customer is not None else None,
            recipient_name=self._text(values.get("recipient_name")),
            subject=self._required_text(values.get("email_subject"), "E-Mail-Betreff"),
            content=content,
            cc_emails="",
            template_id="",
            reply_to_email_id="",
            forward_from_email_id="",
        )
        return {"label": "E-Mail-Entwurf öffnen", "href": f"/emails?folder=drafts&selected=unassigned-{draft.id}"}

    def _resolve_customer(self, name: str, *, required: bool) -> Customer | None:
        if not name:
            if required:
                raise HubAgentError("Für diese Aktion fehlt ein Kunde.")
            return None
        matches = self.db.scalars(select(Customer).where(Customer.name == name)).all()
        if len(matches) != 1:
            raise HubAgentError(f"Der Kunde „{name}“ konnte nicht eindeutig zugeordnet werden.")
        return matches[0]

    def _refresh_job_status(self, job: HubAgentJob) -> None:
        statuses = {action.status for action in job.actions}
        if statuses and statuses <= {"completed"}:
            job.status = "completed"
        elif "proposed" in statuses or "executing" in statuses:
            job.status = "ready"
        elif "failed" in statuses:
            job.status = "needs_attention"
        self.db.flush()

    def _job_view(self, job: HubAgentJob) -> HubAgentJobView:
        request_payload = self._decrypt_json(job.encrypted_request_json)
        plan_payload = self._decrypt_json(job.encrypted_plan_json)
        return HubAgentJobView(
            id=job.id,
            request_text=self._text(request_payload.get("instruction")),
            summary=self._text(plan_payload.get("summary")),
            response_text=self._text(plan_payload.get("response")),
            status=job.status,
            created_at=job.created_at,
            actions=tuple(self._action_view(action) for action in job.actions),
        )

    def _action_view(self, action: HubAgentAction) -> HubAgentActionView:
        payload = self._decrypt_json(action.encrypted_payload_json)
        result = self._decrypt_json(action.encrypted_result_json) if action.encrypted_result_json else {}
        input_values = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        return HubAgentActionView(
            id=action.id,
            action_type=action.action_type,
            title=self._text(payload.get("title")),
            details=self._text(payload.get("details")),
            preview_lines=self._preview_lines(action.action_type, input_values),
            status=action.status,
            result_label=self._text(result.get("label")) or None,
            result_href=self._text(result.get("href")) or None,
            error=self._text(result.get("error")) or None,
        )

    @staticmethod
    def _preview_lines(action_type: str, values: dict[str, Any]) -> tuple[str, ...]:
        if action_type == "create_contact":
            return tuple(
                line
                for line in (
                    f"Kontakt: {' '.join(part for part in (HubAgentService._text(values.get('first_name')), HubAgentService._text(values.get('last_name'))) if part)}".strip(),
                    f"E-Mail: {HubAgentService._text(values.get('email'))}" if HubAgentService._text(values.get("email")) else "",
                    f"Kunde: {HubAgentService._text(values.get('customer_name'))}" if HubAgentService._text(values.get("customer_name")) else "Ohne Kundenverknüpfung",
                )
                if line
            )
        if action_type == "create_task":
            return tuple(
                line
                for line in (
                    f"Aufgabe: {HubAgentService._text(values.get('task_name'))}",
                    f"Kunde: {HubAgentService._text(values.get('customer_name'))}",
                    f"Termin: {HubAgentService._text(values.get('due_date'))} {HubAgentService._text(values.get('due_time'))}",
                    f"Erinnerung: {HubAgentService._text(values.get('reminder_channel')) or 'popup'} {HubAgentService._text(values.get('reminder_minutes_before')) or '0'} Min. vorher",
                )
                if line
            )
        if action_type == "create_email_draft":
            return tuple(
                line
                for line in (
                    f"An: {HubAgentService._text(values.get('recipient_email'))}",
                    f"Betreff: {HubAgentService._text(values.get('email_subject'))}",
                    "Der Entwurf wird nicht automatisch versendet.",
                )
                if line
            )
        return ()

    @staticmethod
    def _normalize_instruction(value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 3:
            raise HubAgentError("Bitte gib eine Anweisung mit mindestens drei Zeichen ein.")
        if len(normalized) > MAX_AGENT_REQUEST_LENGTH:
            raise HubAgentError(f"Bitte begrenze die Anweisung auf {MAX_AGENT_REQUEST_LENGTH} Zeichen.")
        return normalized

    @classmethod
    def _normalize_plan(cls, raw_plan: object) -> dict[str, Any]:
        if not isinstance(raw_plan, dict):
            raise HubAgentError("OpenAI hat einen ungültigen Arbeitsplan geliefert.")
        summary = cls._required_text(raw_plan.get("summary"), "Zusammenfassung")[:800]
        response = cls._required_text(raw_plan.get("response"), "Antwort")[:2_000]
        raw_actions = raw_plan.get("actions")
        if not isinstance(raw_actions, list) or len(raw_actions) > 5:
            raise HubAgentError("OpenAI hat ungültige Aktionsvorschläge geliefert.")
        actions = [cls._normalize_action(action) for action in raw_actions]
        return {"summary": summary, "response": response, "actions": actions}

    @classmethod
    def _normalize_action(cls, raw_action: object) -> dict[str, Any]:
        if not isinstance(raw_action, dict):
            raise HubAgentError("OpenAI hat einen ungültigen Aktionsvorschlag geliefert.")
        action_type = cls._text(raw_action.get("action_type"))
        if action_type not in _ACTION_TYPES:
            raise HubAgentError("OpenAI hat einen nicht erlaubten Aktionsvorschlag geliefert.")
        raw_input = raw_action.get("input")
        if not isinstance(raw_input, dict):
            raise HubAgentError("OpenAI hat unvollständige Aktionsdaten geliefert.")
        input_values = {
            str(key): cls._text(value)[:20_000]
            for key, value in raw_input.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        return {
            "action_type": action_type,
            "title": cls._required_text(raw_action.get("title"), "Aktionstitel")[:255],
            "details": cls._required_text(raw_action.get("details"), "Aktionsbeschreibung")[:1_000],
            "input": input_values,
        }

    def _encrypt_json(self, value: dict[str, Any]) -> str:
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def _decrypt_json(self, value: str) -> dict[str, Any]:
        try:
            decoded = json.loads(self.cipher.decrypt(value))
        except Exception as exc:
            raise HubAgentError("Die gespeicherten Agent-Daten konnten nicht gelesen werden.") from exc
        if not isinstance(decoded, dict):
            raise HubAgentError("Die gespeicherten Agent-Daten sind ungültig.")
        return decoded

    @staticmethod
    def _text(value: object) -> str:
        return value.strip() if isinstance(value, str) else ""

    @classmethod
    def _required_text(cls, value: object, label: str) -> str:
        text_value = cls._text(value)
        if not text_value:
            raise HubAgentError(f"{label} fehlt.")
        return text_value
