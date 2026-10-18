"""Validated request payloads for the compatibility API."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class LoginRequest(RequestModel):
    password: str = Field(alias="pass", min_length=1, max_length=512)


class DirectoryRequest(RequestModel):
    path: str = Field(min_length=1, max_length=4096)
    auth: str | None = Field(default=None, max_length=512)


class FolderCreateRequest(RequestModel):
    path: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=255)


class ItemPathRequest(RequestModel):
    path: str = Field(min_length=1, max_length=4096)


class RenameRequest(ItemPathRequest):
    name: str = Field(min_length=1, max_length=255)


class TrashRequest(ItemPathRequest):
    trash: bool


class TransferRequest(RequestModel):
    id: str = Field(min_length=8, max_length=128)


class TransferLookupRequest(RequestModel):
    client_request_id: str = Field(
        min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


class TransferCancelRequest(RequestModel):
    id: str | None = Field(default=None, min_length=8, max_length=128)
    client_request_id: str | None = Field(
        default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )

    @model_validator(mode="after")
    def require_identifier(self) -> TransferCancelRequest:
        if (self.id is None) == (self.client_request_id is None):
            raise ValueError("Provide either id or client_request_id")
        return self


class RemoteInspectRequest(RequestModel):
    url: HttpUrl


class RemoteStartRequest(RemoteInspectRequest):
    path: str = Field(min_length=1, max_length=4096)
    filename: str | None = Field(default=None, max_length=255)
    single_threaded: bool = Field(default=True, alias="singleThreaded")
    client_request_id: str | None = Field(
        default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


class FolderTreeRequest(BaseModel):
    """Folder names are validated verbatim; whitespace must not be silently stripped."""

    model_config = ConfigDict(extra="forbid", strict=True)
    destination: str = Field(min_length=1, max_length=4096)
    root_name: str = Field(min_length=1, max_length=255)
    folders: list[Annotated[list[str], Field(min_length=1, max_length=63)]] = Field(
        max_length=999
    )
    client_request_id: str = Field(
        min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
