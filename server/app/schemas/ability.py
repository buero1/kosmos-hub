from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class AbilityDescriptor(BaseModel):
    name: str
    label: str
    description: str
    category: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    meta: dict[str, Any]

    @field_validator("input_schema", "output_schema", "meta", mode="before")
    @classmethod
    def normalize_object_fields(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and not value:
            return {}
        raise ValueError("Ability schema fields must be objects.")


class DiscoverAbilitiesResponse(BaseModel):
    server: str
    abilities: list[AbilityDescriptor]


class AbilityInfoResponse(BaseModel):
    ability: AbilityDescriptor


class ExecuteAbilityRequest(BaseModel):
    ability_name: str
    input: dict[str, Any] | None = None

    @field_validator("input", mode="before")
    @classmethod
    def normalize_empty_input(cls, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict) and not value:
            return None
        return value


class ExecuteAbilityResponse(BaseModel):
    ability_name: str
    result: Any


class McpToolResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
