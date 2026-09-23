"""Validated commands for the internal service; calculation models remain unchanged."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import ExportLine


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Login(Command):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class CreateUser(Command):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "manager"] = "manager"


class UpdateUser(Command):
    active: bool | None = None
    role: Literal["admin", "manager"] | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)


class SaveDecisions(Command):
    revision: int = Field(ge=1)
    lines: list[ExportLine] = Field(max_length=100000)


class UpdateOrder(Command):
    revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=160)
    archived: bool | None = None


class ExportOrder(Command):
    revision: int = Field(ge=1)
    approved_only: bool = True
