"""Validated commands for the internal service; calculation models remain unchanged."""
from typing import Literal, Optional

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
    active: Optional[bool] = None
    role: Optional[Literal["admin", "manager"]] = None
    password: Optional[str] = Field(default=None, min_length=12, max_length=256)


class SaveDecisions(Command):
    revision: int = Field(ge=1)
    lines: list[ExportLine] = Field(max_length=100000)


class UpdateOrder(Command):
    revision: int = Field(ge=1)
    title: Optional[str] = Field(default=None, min_length=1, max_length=160)
    archived: Optional[bool] = None


class ExportOrder(Command):
    revision: int = Field(ge=1)
    approved_only: bool = True
