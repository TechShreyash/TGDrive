"""Domain exceptions for TGDrive's metadata layer."""

from __future__ import annotations


class DriveError(Exception):
    """Base class for metadata failures that callers may safely handle."""


class ValidationError(DriveError, ValueError):
    """Stored or user-supplied metadata failed validation."""


class InvalidNameError(ValidationError):
    """An item name is unsafe or outside the supported limits."""


class InvalidPathError(ValidationError):
    """An ID path is malformed or contains traversal syntax."""


class RootMutationError(InvalidPathError):
    """An operation attempted to rename, trash, or delete the root."""


class DuplicateNameError(ValidationError):
    """A sibling already has the requested case-insensitive name."""


class NotFoundError(DriveError, LookupError):
    """The requested item or path does not exist."""


class NotDirectoryError(DriveError):
    """A path component or requested item is not a folder."""


class NotFileError(DriveError):
    """The requested item is not a file."""


class TrashedItemError(DriveError):
    """The requested item is hidden by its own or an ancestor's trash state."""


class CorruptStoreError(DriveError):
    """A metadata file is malformed or internally inconsistent."""


class UnsupportedSchemaError(CorruptStoreError):
    """The metadata schema version is not supported by this implementation."""


class PersistenceError(DriveError):
    """An atomic metadata write could not be completed."""


class InvalidRevisionError(DriveError, ValueError):
    """A backup acknowledgement references an impossible revision."""
