"""Note fields shared by forms, domain validation and operation contracts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HubNoteField:
    name: str
    label: str
    maximum: int
    required: bool


def note_fields(*, creating: bool = False) -> tuple[HubNoteField, ...]:
    return (
        HubNoteField("title", "Titel", 255, not creating),
        HubNoteField("content", "Notiz", 30_000, True),
    )


def normalize_note(*, title: str, content: str, creating: bool = False) -> dict[str, str]:
    values = {"title": title.strip(), "content": content.strip()}
    fields = note_fields(creating=creating)
    for field in fields:
        if field.required and not values[field.name]:
            raise ValueError(f"{field.label} darf nicht leer sein.")
        if len(values[field.name]) > field.maximum:
            raise ValueError(f"{field.label} darf höchstens {field.maximum:,} Zeichen enthalten.")
    if creating and not values["title"]:
        maximum = next(field.maximum for field in fields if field.name == "title")
        values["title"] = next(line.strip() for line in values["content"].splitlines() if line.strip())[:maximum]
    return values
