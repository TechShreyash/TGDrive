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
MAX_TEXT = 2 * BLOCK
MAX_ENTRIES = 10000
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
    def __init__(self, size, fetch):
        self.size, self.fetch, self.pos = size, fetch, 0
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
        if amount > MAX_READ:
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
                if self.used + count > MAX_READ:
                    raise BookError(
                        "This archive needs more than 32 MiB to read this page. "
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
    if (
        entry.file_size > maximum
        or entry.compress_type not in {0, 8}
        or entry.file_size > max(1, entry.compress_size) * 1000
    ):
        raise BookError("This book resource exceeds the reader limits.")
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


def read_book(source, fmt, entry=None):
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
                else MAX_ENTRY,
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
            mime = image_type(data)
            return {
                "kind": "image",
                "mime": mime,
                "data": base64.b64encode(data).decode(),
            }
    if fmt != "cbr":
        raise BookError("Unsupported reading format.")
    source.seek(0)
    if not source.read(8).startswith(b"Rar!\x1a\x07"):
        raise BookError("This is not a readable RAR comic.")
    source.seek(0)
    try:
        import libarchive
    except (ImportError, OSError) as exc:
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
                if member.size is None or member.size > MAX_ENTRY:
                    raise BookError("This page is too large to preview.")
                output = bytearray()
                for block in member.get_blocks():
                    output.extend(block)
                    if source.failure:
                        raise source.failure
                    if len(output) > MAX_ENTRY:
                        raise BookError("This page is too large to preview.")
                data = bytes(output)
                return {
                    "kind": "image",
                    "mime": image_type(data),
                    "data": base64.b64encode(data).decode(),
                }
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
        result = read_book(
            RemoteFile(request["size"], fetch), request["format"], request.get("entry")
        )
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
