"""Validated payloads for owner requests, folder uploads, and transfer history."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class LoginRequest(RequestModel):
    # Passwords hashed by the owner CLI may intentionally include whitespace.
    model_config = ConfigDict(str_strip_whitespace=False)
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
    single_threaded: bool = Field(default=False, alias="singleThreaded")
    client_request_id: str | None = Field(
        default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


class TorrentStartRequest(RequestModel):
    url: str = Field(min_length=1, max_length=16384)
    path: str = Field(min_length=1, max_length=4096)
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


class FolderUploadMetadata(BaseModel):
    """Stable identity for one explicitly selected device folder upload."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=255)
    root_path: str = Field(min_length=2, max_length=4096, pattern=r"^/")
    created_at: float = Field(ge=0, allow_inf_nan=False)


TransferIdentity = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
]


class TransferTaskHistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal[
        "all", "finished", "queued", "running", "completed", "error", "cancelled"
    ] = "all"
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=20, ge=1, le=200)
    q: str = Field(default="", max_length=255)
    offset: int | None = Field(default=None, ge=0)
    exclude_folders: list[TransferIdentity] = Field(
        default_factory=list, max_length=2000
    )
