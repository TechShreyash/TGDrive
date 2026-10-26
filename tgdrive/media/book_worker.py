"""Isolated, bounded archive parsing. Never extract a book onto the filesystem."""

from __future__ import annotations

import base64
import io
import json
import posixpath
import re
import struct
import sys
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

from defusedxml import ElementTree as ET

BLOCK = 1024 * 1024
MAX_READ = 32 * BLOCK
MAX_ENTRY = 16 * BLOCK
MAX_PAGE = 64 * BLOCK
MAX_PAGE_READ = 96 * BLOCK
MAX_TEXT = 2 * BLOCK
MAX_ENTRIES = 10000
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_EDGE = 32768
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}


class BookError(ValueError):
    pass


def safe_name(name):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 1024
        or name.startswith("/")
        or "\\" in name
        or any(ord(c) < 32 for c in name)
    ):
        raise BookError("This book contains an invalid resource path.")
    if any(p in {"..", "."} for p in name.split("/")):
        raise BookError("This book contains an unsafe resource path.")
    return name


def local_ref(base, value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    url = urlsplit(value)
    if url.scheme or url.netloc or value.startswith(("/", "\\")):
        return None
    path = (
        posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(url.path)))
        if url.path
        else base
    )
    try:
        safe_name(path)
    except BookError:
        return None
    return path + ("#" + unquote(url.fragment) if url.fragment else "")


class RemoteFile(io.RawIOBase):
    def __init__(self, size, fetch, *, maximum=MAX_READ):
        self.size, self.fetch, self.pos = size, fetch, 0
        self.maximum = maximum
        self.cache = OrderedDict()
        self.used = 0
        self.failure = None

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        value = (
            offset
            if whence == 0
            else self.pos + offset
            if whence == 1
            else self.size + offset
            if whence == 2
            else -1
        )
        if not 0 <= value <= self.size:
            raise BookError("The book requests an invalid byte range.")
        self.pos = value
        return value

    def read(self, amount=-1):
        if amount is None or amount < 0:
            amount = self.size - self.pos
        if amount > self.maximum:
            raise BookError(
                "This book needs too much data for a selective preview. "
                "Download it instead."
            )
        end = min(self.size, self.pos + amount)
        output = bytearray()
        while self.pos < end:
            block = self.pos // BLOCK
            if block not in self.cache:
                count = min(BLOCK, self.size - block * BLOCK)
                if self.used + count > self.maximum:
                    raise BookError(
                        f"This archive needs more than {self.maximum // BLOCK} MiB "
                        "to read this page. "
                        "Download it instead."
                    )
                data = self.fetch(block * BLOCK, count)
                if len(data) != count:
                    raise BookError("The book ended before its declared length.")
                self.used += count
                self.cache[block] = data
                while len(self.cache) > 4:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(block)
            chunk = self.cache[block][
                self.pos % BLOCK : min(BLOCK, end - block * BLOCK)
            ]
            output.extend(chunk)
            self.pos += len(chunk)
        return bytes(output)

    def readinto(self, target):
        data = self.read(len(target))
        target[: len(data)] = data
        return len(data)


class SafeRarSource:
    # ctypes callbacks must return an error sentinel instead of raising across C.
    def __init__(self, source):
        self.source = source

    def seekable(self):
        return True

    def seek(self, *args):
        try:
            return self.source.seek(*args)
        except Exception as exc:
            self.source.failure = exc
            return self.source.pos

    def tell(self):
        return self.source.pos

    def readinto(self, target):
        if self.source.failure:
            return 0
        try:
            return self.source.readinto(target)
        except Exception as exc:
            self.source.failure = exc
            return 0


def open_zip(source):
    # Bound the directory before ZipFile allocates one object per entry.
    tail_size = min(source.size, 65557)
    source.seek(source.size - tail_size)
    tail = source.read(tail_size)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        raise BookError("This is not a readable ZIP book.")
    _, disk, cd_disk, disk_count, count, length, offset, comment = struct.unpack_from(
        "<4s4H2LH", tail, at
    )
    if at + 22 + comment != len(tail) or disk or cd_disk or disk_count != count:
        raise BookError("Split-volume ZIP books are not supported.")
    if count == 65535 or length == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        position = source.size - tail_size + at
        if position < 20:
            raise BookError("Invalid ZIP64 book.")
        source.seek(position - 20)
        locator = source.read(20)
        signature, disk, zip64_at, disks = struct.unpack("<4sLQL", locator)
        if signature != b"PK\x06\x07" or disk or disks != 1:
            raise BookError("Split-volume books are not supported.")
        source.seek(zip64_at)
        record = struct.unpack("<4sQ2H2L4Q", source.read(56))
        if (
            record[0] != b"PK\x06\x06"
            or record[4]
            or record[5]
            or record[6] != record[7]
        ):
            raise BookError("Invalid ZIP64 directory.")
        count, length, offset = record[7:10]
    if count > MAX_ENTRIES or length > 2 * BLOCK or offset + length > source.size:
        raise BookError("This book has too many resources for the reader.")
    archive = ZipFile(source)
    names = set()
    for entry in archive.infolist():
        if entry.is_dir():
            continue
        safe_name(entry.filename)
        if entry.filename in names or entry.flag_bits & 1:
            archive.close()
            raise BookError("Duplicate or encrypted book resources are not supported.")
        names.add(entry.filename)
    return archive


def zip_bytes(archive, name, maximum=MAX_TEXT):
    safe_name(name)
    try:
        entry = archive.getinfo(name)
    except KeyError as exc:
        raise BookError("This book resource is missing.") from exc
    if entry.file_size > maximum:
        raise BookError(
            f"This resource exceeds the {maximum // BLOCK} MiB preview limit."
        )
    if entry.compress_type not in {0, 8}:
        raise BookError("This resource uses an unsupported ZIP compression method.")
    if entry.file_size > max(1, entry.compress_size) * 1000:
        raise BookError("This resource has an excessive compression ratio.")
    with archive.open(entry) as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise BookError("This page is too large to preview.")
    return data


def xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise BookError("Books with XML entity declarations are not supported.")
    return ET.fromstring(data)


def natural(name):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", name)]


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    raise BookError("This page is not a supported image.")


def image_dimensions(data, mime):
    """Read bounded raster headers only; never allocate decoded pixels."""
    if mime == "image/png":
        if len(data) < 33 or data[8:16] != b"\x00\x00\x00\rIHDR":
            raise ValueError
        dimensions = [struct.unpack_from(">II", data, 16)]
        position = 33
        for _ in range(MAX_ENTRIES):
            if position + 12 > len(data):
                raise ValueError
            length, kind = struct.unpack_from(">I4s", data, position)
            if position + length + 12 > len(data):
                raise ValueError
            if kind == b"fcTL":
                if length != 26:
                    raise ValueError
                width, height, left, top = struct.unpack_from(
                    ">IIII", data, position + 12
                )
                dimensions.extend(((width, height), (left + width, top + height)))
            if kind == b"IEND":
                return dimensions
            position += length + 12
        raise ValueError
    if mime == "image/gif":
        if len(data) < 13:
            raise ValueError
        dimensions = [struct.unpack_from("<HH", data, 6)]
        position = 13 + (3 * (2 << (data[10] & 7)) if data[10] & 0x80 else 0)
        for _ in range(MAX_ENTRIES):
            if position >= len(data):
                raise ValueError
            kind = data[position]
            position += 1
            if kind == 0x3B:
                return dimensions
            if kind == 0x2C:
                if position + 9 > len(data):
                    raise ValueError
                left, top, width, height = struct.unpack_from("<HHHH", data, position)
                dimensions.extend(((width, height), (left + width, top + height)))
                packed = data[position + 8]
                position += 10 + (3 * (2 << (packed & 7)) if packed & 0x80 else 0)
            elif kind == 0x21:
                position += 1
            else:
                raise ValueError
            for _ in range(MAX_ENTRIES):
                if position >= len(data):
                    raise ValueError
                length = data[position]
                position += length + 1
                if not length:
                    break
            else:
                raise ValueError
        raise ValueError
    if mime == "image/jpeg":
        position = 2
        for _ in range(MAX_ENTRIES):
            if position + 4 > min(len(data), BLOCK) or data[position] != 0xFF:
                raise ValueError
            while position < min(len(data), BLOCK) and data[position] == 0xFF:
                position += 1
            if position + 3 > min(len(data), BLOCK):
                raise ValueError
            marker = data[position]
            position += 1
            if marker == 0x01:
                continue
            if marker in {0xD9, 0xDA}:
                raise ValueError
            length = int.from_bytes(data[position : position + 2], "big")
            if length < 2 or position + length > len(data):
                raise ValueError
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                if length < 8:
                    raise ValueError
                height, width = struct.unpack_from(">HH", data, position + 3)
                return [(width, height)]
            position += length
        raise ValueError
    if mime == "image/webp":
        end = int.from_bytes(data[4:8], "little") + 8
        if end > len(data) or end < 20:
            raise ValueError
        pending = [(12, end)]
        dimensions = []
        count = 0
        while pending:
            position, end = pending.pop()
            while position < end:
                count += 1
                if count > MAX_ENTRIES or position + 8 > end:
                    raise ValueError
                kind = data[position : position + 4]
                length = int.from_bytes(data[position + 4 : position + 8], "little")
                start = position + 8
                if start + length > end:
                    raise ValueError
                if kind == b"VP8X" and length >= 10:
                    dimensions.append(
                        tuple(
                            int.from_bytes(
                                data[start + offset : start + offset + 3], "little"
                            )
                            + 1
                            for offset in (4, 7)
                        )
                    )
                elif kind == b"VP8 " and length >= 10:
                    if data[start + 3 : start + 6] != b"\x9d\x01\x2a":
                        raise ValueError
                    dimensions.append(
                        tuple(
                            value & 0x3FFF
                            for value in struct.unpack_from("<HH", data, start + 6)
                        )
                    )
                elif kind == b"VP8L" and length >= 5 and data[start] == 0x2F:
                    bits = int.from_bytes(data[start + 1 : start + 5], "little")
                    dimensions.append(
                        ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
                    )
                elif kind == b"ANMF" and length >= 16:
                    left, top, width, height = (
                        int.from_bytes(
                            data[start + offset : start + offset + 3], "little"
                        )
                        for offset in (0, 3, 6, 9)
                    )
                    dimensions.extend(
                        (
                            (width + 1, height + 1),
                            (left * 2 + width + 1, top * 2 + height + 1),
                        )
                    )
                    pending.append((start + 16, start + length))
                position = start + length + (length & 1)
        return dimensions
    if mime == "image/avif":
        # Visit only dimension-bearing HEIF properties and AV1 sample entries.
        containers = {
            b"meta": 4,
            b"iprp": 0,
            b"ipco": 0,
            b"moov": 0,
            b"trak": 0,
            b"mdia": 0,
            b"minf": 0,
            b"stbl": 0,
            b"stsd": 8,
        }
        pending = [(0, len(data), 0)]
        dimensions = []
        count = 0
        while pending:
            position, end, depth = pending.pop()
            while position < end:
                count += 1
                if count > MAX_ENTRIES or depth > 8 or position + 8 > end:
                    raise ValueError
                length, kind = struct.unpack_from(">I4s", data, position)
                header = 8
                if length == 1:
                    if position + 16 > end:
                        raise ValueError
                    length = struct.unpack_from(">Q", data, position + 8)[0]
                    header = 16
                elif length == 0:
                    length = end - position
                if length < header or position + length > end:
                    raise ValueError
                start, stop = position + header, position + length
                if kind == b"ispe":
                    if stop - start < 12:
                        raise ValueError
                    dimensions.append(struct.unpack_from(">II", data, start + 4))
                elif kind == b"av01":
                    if stop - start < 78:
                        raise ValueError
                    dimensions.append(struct.unpack_from(">HH", data, start + 24))
                elif kind in containers:
                    child = start + containers[kind]
                    if child > stop:
                        raise ValueError
                    pending.append((child, stop, depth + 1))
                position = stop
        return dimensions
    raise ValueError


def image_result(data, *, raw_images):
    mime = image_type(data)
    try:
        dimensions = image_dimensions(data, mime)
        if not dimensions or any(
            not width or not height for width, height in dimensions
        ):
            raise ValueError
    except ValueError, struct.error, IndexError:
        raise BookError(
            "This page has invalid or overly complex image dimensions. "
            "Download the original to open it in another reader."
        ) from None
    if any(
        width > MAX_IMAGE_EDGE
        or height > MAX_IMAGE_EDGE
        or width * height > MAX_IMAGE_PIXELS
        for width, height in dimensions
    ):
        raise BookError(
            "This page exceeds the 16 million pixel or 32768-pixel edge limit. "
            "Download the original to open it in another reader."
        )
    return {
        "kind": "image",
        "mime": mime,
        **(
            {"content": data}
            if raw_images
            else {"data": base64.b64encode(data).decode()}
        ),
    }


class ChapterParser(HTMLParser):
    allowed = frozenset(
        [
            "p",
            "div",
            "span",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "strong",
            "b",
            "em",
            "i",
            "u",
            "s",
            "a",
            "img",
            "ul",
            "ol",
            "li",
            "blockquote",
            "pre",
            "code",
            "br",
            "hr",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
            "caption",
            "dl",
            "dt",
            "dd",
            "sup",
            "sub",
            "figure",
            "figcaption",
            "section",
            "article",
        ]
    )
    blocked = frozenset(
        [
            "script",
            "style",
            "iframe",
            "object",
            "embed",
            "form",
            "audio",
            "video",
            "svg",
            "math",
            "template",
            "head",
        ]
    )

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.root = []
        self.stack = [("", self.root)]
        self.hidden = 0
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.blocked:
            self.hidden += 1
        if self.hidden or tag not in self.allowed:
            return
        self.nodes += 1
        if self.nodes > 20000 or len(self.stack) > 100:
            raise BookError("This chapter is too complex to preview.")
        attrs = dict(attrs)
        node = {"tag": tag, "children": []}
        if attrs.get("id") and len(attrs["id"]) <= 128:
            node["id"] = attrs["id"]
        if tag == "a":
            node["href"] = local_ref(self.base, attrs.get("href", ""))
        if tag == "img":
            node["src"] = local_ref(self.base, attrs.get("src", ""))
            node["alt"] = (attrs.get("alt") or "")[:500]
        self.stack[-1][1].append(node)
        if tag not in {"img", "br", "hr"}:
            self.stack.append((tag, node["children"]))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in {"img", "br", "hr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.blocked and self.hidden:
            self.hidden -= 1
            return
        if self.hidden:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i][0] == tag:
                self.stack = self.stack[:i]
                break

    def handle_data(self, data):
        if not self.hidden and data:
            self.nodes += 1
            if self.nodes > 20000:
                raise BookError("This chapter is too complex to preview.")
            self.stack[-1][1].append(data)


def epub_index(archive):
    if "META-INF/encryption.xml" in archive.namelist():
        raise BookError(
            "Encrypted EPUB resources are not supported. Use an authorized reading app."
        )
    container = xml(zip_bytes(archive, "META-INF/container.xml"))
    rootfile = next(
        (
            n.get("full-path")
            for n in container.iter()
            if n.tag.rsplit("}", 1)[-1] == "rootfile"
        ),
        None,
    )
    if not rootfile:
        raise BookError("The EPUB package is missing.")
    package = xml(zip_bytes(archive, rootfile))
    manifest = {}
    title = ""
    for node in package.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "title" and not title:
            title = (node.text or "")[:300]
        if tag == "item" and node.get("id") and node.get("href"):
            name = local_ref(rootfile, node.get("href"))
            if name:
                manifest[node.get("id")] = (
                    name.split("#")[0],
                    node.get("media-type"),
                    node.get("properties", ""),
                )
    labels = {}
    for name, _mime, properties in manifest.values():
        if "nav" in properties.split():
            nav = xml(zip_bytes(archive, name))
            for link in nav.iter():
                if link.tag.rsplit("}", 1)[-1] == "a":
                    target = local_ref(name, link.get("href", ""))
                    if target:
                        labels.setdefault(
                            target.split("#")[0],
                            " ".join(link.itertext()).strip()[:200],
                        )
    entries = []
    for node in package.iter():
        if (
            node.tag.rsplit("}", 1)[-1] == "itemref"
            and node.get("linear", "yes") != "no"
        ):
            item = manifest.get(node.get("idref"))
            if item and item[1] in {"application/xhtml+xml", "text/html"}:
                name = item[0]
                entries.append(
                    {
                        "name": name,
                        "label": labels.get(name) or posixpath.basename(name),
                    }
                )
    if not entries or len(entries) > 5000:
        raise BookError("This EPUB has no supported reading order.")
    return {"kind": "epub", "title": title, "entries": entries}


def read_book(source, fmt, entry=None, *, raw_images=False):
    image_limit = MAX_PAGE if raw_images else MAX_ENTRY
    if fmt in {"epub", "cbz"}:
        with open_zip(source) as archive:
            if entry is None:
                if fmt == "epub":
                    return epub_index(archive)
                pages = sorted(
                    (
                        n
                        for n in archive.namelist()
                        if posixpath.splitext(n)[1].lower() in IMAGES
                        and not n.startswith("__MACOSX/")
                    ),
                    key=natural,
                )
                if not pages:
                    raise BookError("This comic has no supported image pages.")
                return {
                    "kind": "comic",
                    "entries": [
                        {"name": n, "label": "Page " + str(i + 1)}
                        for i, n in enumerate(pages)
                    ],
                }
            data = zip_bytes(
                archive,
                entry,
                MAX_TEXT
                if posixpath.splitext(entry)[1].lower() in {".html", ".xhtml", ".htm"}
                else image_limit,
            )
            if fmt == "epub" and posixpath.splitext(entry)[1].lower() in {
                ".html",
                ".xhtml",
                ".htm",
            }:
                parser = ChapterParser(entry)
                parser.feed(data.decode("utf-8-sig", errors="replace"))
                parser.close()
                return {"kind": "chapter", "nodes": parser.root}
            return image_result(data, raw_images=raw_images)
    if fmt != "cbr":
        raise BookError("Unsupported reading format.")
    source.seek(0)
    if not source.read(8).startswith(b"Rar!\x1a\x07"):
        raise BookError("This is not a readable RAR comic.")
    source.seek(0)
    try:
        import libarchive
    except (ImportError, OSError, AttributeError) as exc:
        raise BookError(
            "CBR support needs libarchive. "
            "Use the TGDrive Docker image or install libarchive."
        ) from exc
    pages = []
    with libarchive.stream_reader(
        SafeRarSource(source), filter_name="none", block_size=65536
    ) as archive:
        for i, member in enumerate(archive):
            if source.failure:
                raise source.failure
            if i >= MAX_ENTRIES:
                raise BookError("This comic has too many resources.")
            name = str(member.pathname)
            safe_name(name)
            if not member.isfile or posixpath.splitext(name)[1].lower() not in IMAGES:
                continue
            if entry is None:
                if name in pages:
                    raise BookError("This comic contains duplicate page names.")
                pages.append(name)
            elif name == entry:
                if member.size is None or member.size > image_limit:
                    raise BookError("This page is too large to preview.")
                output = bytearray()
                for block in member.get_blocks():
                    output.extend(block)
                    if source.failure:
                        raise source.failure
                    if len(output) > image_limit:
                        raise BookError("This page is too large to preview.")
                data = bytes(output)
                return image_result(data, raw_images=raw_images)
    if source.failure:
        raise source.failure
    if entry is not None:
        raise BookError("This comic page is missing.")
    if not pages:
        raise BookError("This comic has no supported image pages.")
    return {
        "kind": "comic",
        "entries": [
            {"name": n, "label": "Page " + str(i + 1)}
            for i, n in enumerate(sorted(pages, key=natural))
        ],
    }


def run():
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (384 * BLOCK, 384 * BLOCK))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except ImportError:
        pass
    request = json.loads(sys.stdin.readline())

    def fetch(offset, length):
        print(json.dumps({"read": [offset, length]}), flush=True)
        reply = json.loads(sys.stdin.readline())
        return base64.b64decode(reply["data"], validate=True)

    try:
        # The larger budget applies only to a selected original raster page.
        # Indexes, text and thumbnail extraction retain their smaller limits.
        raw_images = bool(request.get("raw_images")) and not request.get("thumbnail")
        raster = (
            raw_images
            and posixpath.splitext(request.get("entry") or "")[1].lower() in IMAGES
        )
        source = RemoteFile(
            request["size"], fetch, maximum=MAX_PAGE_READ if raster else MAX_READ
        )
        if request.get("thumbnail") and request["format"] in {
            "epub",
            "cbz",
            "docx",
            "pptx",
            "xlsx",
            "odt",
            "odp",
            "ods",
        }:
            from tgdrive.media.thumbnail_worker import archive_cover_from_source

            cover = archive_cover_from_source(source, "." + request["format"])
            result = (
                {"kind": "missing"}
                if cover is None
                else {
                    "kind": "image",
                    "mime": image_type(cover[0]),
                    "data": base64.b64encode(cover[0]).decode(),
                }
            )
        else:
            result = read_book(
                source, request["format"], request.get("entry"), raw_images=raw_images
            )
        if result.get("kind") == "image" and "content" in result:
            content = result.pop("content")
            print(
                json.dumps({"image": {"mime": result["mime"], "length": len(content)}}),
                flush=True,
            )
            sys.stdout.buffer.write(content)
            sys.stdout.buffer.flush()
        else:
            print(json.dumps({"result": result}), flush=True)
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, BookError)
            else (
                "This book is damaged, encrypted or uses an unsupported feature. "
                "Download it to open in a dedicated reader."
            )
        )
        print(json.dumps({"error": message[:500]}), flush=True)


if __name__ == "__main__":
    run()
