"""HTTP byte-range parsing and Telegram chunk planning.

Only a single ``bytes`` range is accepted.  The parser deliberately rejects
multi-range requests because the streamer does not produce multipart bodies.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

TELEGRAM_CHUNK_SIZE = 1024 * 1024
_RANGE_RE = re.compile(r"^bytes=([0-9]*)-([0-9]*)$", re.IGNORECASE)


class RangeError(ValueError):
    """Base class for invalid or unsatisfiable HTTP ranges."""


class MalformedRange(RangeError):
    """The Range header is syntactically invalid or unsupported."""


class RangeNotSatisfiable(RangeError):
    """The Range header cannot select bytes from the current representation."""

    def __init__(self, total: int, message: str = "range is not satisfiable") -> None:
        self.total = total
        super().__init__(message)

    @property
    def content_range(self) -> str:
        """Value for a 416 response's ``Content-Range`` header."""

        return f"bytes */{self.total}"


@dataclass(frozen=True, slots=True)
class ByteRange:
    """A normalized inclusive byte interval."""

    start: int
    end: int
    total: int
    is_partial: bool

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError("total size cannot be negative")
        if self.total == 0:
            if (self.start, self.end) != (0, -1):
                raise ValueError("an empty representation must use range 0..-1")
            if self.is_partial:
                raise ValueError("an empty representation cannot be partial")
            return
        if not 0 <= self.start <= self.end < self.total:
            raise ValueError("byte range lies outside the representation")

    @property
    def length(self) -> int:
        return 0 if self.total == 0 else self.end - self.start + 1

    @property
    def content_range(self) -> str | None:
        if not self.is_partial:
            return None
        return f"bytes {self.start}-{self.end}/{self.total}"


@dataclass(frozen=True, slots=True)
class TelegramChunk:
    """One Telegram ``upload.GetFile`` read and the bytes retained from it."""

    offset: int
    limit: int
    cut_start: int
    cut_end: int

    @property
    def output_length(self) -> int:
        return self.cut_end - self.cut_start


@dataclass(frozen=True, slots=True)
class TelegramChunkPlan:
    """An exact aligned read plan for a normalized byte range."""

    offset: int
    chunk_size: int
    part_count: int
    first_part_cut: int
    last_part_cut: int
    requested_length: int

    def iter_chunks(self) -> Iterator[TelegramChunk]:
        """Yield every aligned Telegram read and its half-open slice."""

        for index in range(self.part_count):
            cut_start = self.first_part_cut if index == 0 else 0
            cut_end = (
                self.last_part_cut if index == self.part_count - 1 else self.chunk_size
            )
            yield TelegramChunk(
                offset=self.offset + index * self.chunk_size,
                limit=self.chunk_size,
                cut_start=cut_start,
                cut_end=cut_end,
            )


def _bounded_decimal(value: str, total: int) -> int:
    # RFC ranges have no fixed digit limit. Saturating above the representation
    # size preserves clamping/suffix semantics without Python's int digit limit.
    significant = value.lstrip("0")
    if len(significant) > len(str(total)):
        return total + 1
    return int(significant or "0")


def parse_byte_range(value: str | None, total: int) -> ByteRange:
    """Parse and normalize one HTTP ``Range`` header.

    Supported forms are ``bytes=start-end``, ``bytes=start-``, and
    ``bytes=-suffix_length``.  An absent header selects the full
    representation.  Explicit ends beyond EOF are clamped as required by
    RFC 9110.
    """

    if total < 0:
        raise ValueError("total size cannot be negative")

    if value is None:
        return ByteRange(0, total - 1, total, is_partial=False)

    raw = value.strip()
    if "," in raw:
        raise MalformedRange("multiple byte ranges are not supported")

    match = _RANGE_RE.fullmatch(raw)
    if match is None:
        raise MalformedRange("expected a single 'bytes=start-end' range")

    first, last = match.groups()
    if not first and not last:
        raise MalformedRange("a range start or suffix length is required")
    if total == 0:
        raise RangeNotSatisfiable(total)

    if not first:
        suffix_length = _bounded_decimal(last, total)
        if suffix_length <= 0:
            raise RangeNotSatisfiable(total, "suffix length must be positive")
        length = min(suffix_length, total)
        return ByteRange(total - length, total - 1, total, is_partial=True)

    start = _bounded_decimal(first, total)
    if start >= total:
        raise RangeNotSatisfiable(total, "range starts beyond end of file")

    if not last:
        end = total - 1
    else:
        requested_end = _bounded_decimal(last, total)
        if requested_end < start:
            raise RangeNotSatisfiable(total, "range end precedes range start")
        end = min(requested_end, total - 1)

    return ByteRange(start, end, total, is_partial=True)


def plan_telegram_chunks(
    byte_range: ByteRange,
    *,
    chunk_size: int = TELEGRAM_CHUNK_SIZE,
) -> TelegramChunkPlan:
    """Build aligned 1 MiB Telegram reads for ``byte_range``.

    ``first_part_cut`` and ``last_part_cut`` are slice indexes compatible
    with ``chunk[first_part_cut:last_part_cut]`` when the plan has one part.
    """

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if byte_range.length == 0:
        return TelegramChunkPlan(0, chunk_size, 0, 0, 0, 0)

    first_part = byte_range.start // chunk_size
    last_part = byte_range.end // chunk_size
    part_count = last_part - first_part + 1
    offset = first_part * chunk_size

    return TelegramChunkPlan(
        offset=offset,
        chunk_size=chunk_size,
        part_count=part_count,
        first_part_cut=byte_range.start - offset,
        last_part_cut=(byte_range.end % chunk_size) + 1,
        requested_length=byte_range.length,
    )
