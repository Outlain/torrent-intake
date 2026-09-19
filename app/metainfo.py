"""Bounded .torrent metadata validation; never extract files or fetch its URLs."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib


MAX_TORRENT_BYTES = 32 * 1024 * 1024
MAX_METAINFO_NODES = 300_000
MAX_METAINFO_DEPTH = 64


@dataclass(frozen=True)
class TorrentMetainfo:
    data: bytes
    magnet_uri: str
    name: str
    size_bytes: int


class _Decoder:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0
        self.nodes = 0
        self.info_bytes = b""

    def read(self, depth: int = 0):
        self.nodes += 1
        if depth > MAX_METAINFO_DEPTH or self.nodes > MAX_METAINFO_NODES:
            raise ValueError("Torrent metadata is too deeply nested or has too many entries")
        if self.offset >= len(self.data):
            raise ValueError("Truncated torrent metadata")
        token = self.data[self.offset:self.offset + 1]
        if token in (b"d", b"l"):
            self.offset += 1
            result = {} if token == b"d" else []
            previous = None
            while self.offset < len(self.data) and self.data[self.offset:self.offset + 1] != b"e":
                if token == b"l":
                    result.append(self.read(depth + 1))
                else:
                    key = self.read(depth + 1)
                    if not isinstance(key, bytes) or (previous is not None and key <= previous):
                        raise ValueError("Torrent dictionary keys must be unique, ordered byte strings")
                    previous = key
                    start = self.offset
                    result[key] = self.read(depth + 1)
                    if depth == 0 and key == b"info":
                        self.info_bytes = self.data[start:self.offset]
            if self.offset >= len(self.data):
                raise ValueError("Truncated torrent metadata")
            self.offset += 1
            return result
        if token == b"i":
            end = self.data.find(b"e", self.offset + 1, self.offset + 23)
            if end < 0:
                raise ValueError("Invalid or oversized torrent integer")
            raw = self.data[self.offset + 1:end]
            if not raw or not raw.lstrip(b"-").isdigit():
                raise ValueError("Invalid torrent integer")
            value = int(raw)
            if str(value).encode() != raw or not -(2**63) <= value < 2**63:
                raise ValueError("Invalid or oversized torrent integer")
            self.offset = end + 1
            return value
        if b"0" <= token <= b"9":
            end = self.data.find(b":", self.offset, self.offset + 12)
            if end < 0:
                raise ValueError("Invalid torrent byte string")
            raw = self.data[self.offset:end]
            if not raw.isdigit() or (len(raw) > 1 and raw.startswith(b"0")):
                raise ValueError("Invalid torrent byte string length")
            length = int(raw)
            self.offset = end + 1
            if length > len(self.data) - self.offset:
                raise ValueError("Truncated torrent byte string")
            value = self.data[self.offset:self.offset + length]
            self.offset += length
            return value
        raise ValueError("Invalid bencoded torrent metadata")


def _component(value) -> bytes:
    if (not isinstance(value, bytes) or not value or value in (b".", b"..")
            or any(char in value for char in (b"/", b"\\", b"\0"))
            or (len(value) >= 2 and value[1:2] == b":")):
        raise ValueError("Torrent contains an unsafe file or directory name")
    return value


def _file_size(entry: dict) -> int:
    attributes = entry.get(b"attr", b"")
    if not isinstance(attributes, bytes) or b"l" in attributes or b"symlink path" in entry:
        raise ValueError("Symlink entries are not supported in torrent uploads")
    length = entry.get(b"length")
    if not isinstance(length, int) or length < 0:
        raise ValueError("Torrent file length must be a nonnegative integer")
    return length


def parse_torrent(data: bytes) -> TorrentMetainfo:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_TORRENT_BYTES:
        raise ValueError("Upload a nonempty .torrent metadata file of at most 32 MiB")
    decoder = _Decoder(data)
    metadata = decoder.read()
    if decoder.offset != len(data) or not isinstance(metadata, dict):
        raise ValueError("Expected one complete .torrent dictionary, without trailing data")
    info = metadata.get(b"info")
    if not isinstance(info, dict) or not decoder.info_bytes:
        raise ValueError("Torrent metadata is missing its info dictionary")
    name = _component(info.get(b"name"))
    if b"name.utf-8" in info:
        _component(info[b"name.utf-8"])
    piece_length = info.get(b"piece length")
    if not isinstance(piece_length, int) or piece_length <= 0:
        raise ValueError("Torrent piece length must be a positive integer")
    if info.get(b"private", 0) not in (0, 1):
        raise ValueError("Invalid private-torrent flag")
    version = info.get(b"meta version")
    if version not in (None, 2):
        raise ValueError("Unsupported torrent metadata version")
    paths: set[tuple[bytes, ...]] = set()

    def add_path(path: tuple[bytes, ...]) -> None:
        if path in paths:
            raise ValueError("Torrent contains duplicate file paths")
        paths.add(path)

    def check_paths() -> None:
        for path in paths:
            if any(path[:index] in paths for index in range(1, len(path))):
                raise ValueError("Torrent file paths overlap a directory path")

    size = 0
    if b"pieces" in info:
        if (b"files" in info) == (b"length" in info):
            raise ValueError("Torrent must contain either one file length or a file list")
        if b"length" in info:
            size = _file_size(info)
        else:
            files = info[b"files"]
            if not isinstance(files, list) or not files:
                raise ValueError("Torrent file list must not be empty")
            for entry in files:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid torrent file entry")
                size += _file_size(entry)
                path = entry.get(b"path")
                if not isinstance(path, list) or not path:
                    raise ValueError("Torrent file path must not be empty")
                add_path(tuple(_component(part) for part in path))
                if b"path.utf-8" in entry:
                    alternate = entry[b"path.utf-8"]
                    if not isinstance(alternate, list) or not alternate:
                        raise ValueError("Invalid alternate torrent file path")
                    for part in alternate:
                        _component(part)
        pieces = info[b"pieces"]
        if not isinstance(pieces, bytes) or len(pieces) != ((size + piece_length - 1) // piece_length) * 20:
            raise ValueError("Torrent piece hashes do not match its declared content size")
        magnet = "magnet:?xt=urn:btih:" + hashlib.sha1(decoder.info_bytes).hexdigest()
        check_paths()
    elif version != 2:
        raise ValueError("Torrent metadata is missing its piece hashes")

    if version == 2:
        if piece_length < 16384 or piece_length & (piece_length - 1):
            raise ValueError("Invalid v2 torrent piece length")
        tree = info.get(b"file tree")
        if not isinstance(tree, dict) or not tree:
            raise ValueError("V2 torrent metadata is missing its file tree")
        paths.clear()

        def visit(branch: dict, path: tuple[bytes, ...] = ()) -> int:
            total = 0
            for key, child in branch.items():
                if not isinstance(child, dict):
                    raise ValueError("Invalid v2 torrent file tree")
                if key == b"":
                    if not path or len(branch) != 1:
                        raise ValueError("Invalid v2 torrent file leaf")
                    add_path(path)
                    length = _file_size(child)
                    if length and (not isinstance(child.get(b"pieces root"), bytes) or len(child[b"pieces root"]) != 32):
                        raise ValueError("Invalid v2 torrent file hash")
                    total += length
                else:
                    if not child:
                        raise ValueError("Empty v2 torrent directory")
                    total += visit(child, (*path, _component(key)))
            return total

        v2_size = visit(tree)
        v2_magnet = "xt=urn:btmh:1220" + hashlib.sha256(decoder.info_bytes).hexdigest()
        if b"pieces" in info:
            magnet += "&" + v2_magnet
        else:
            magnet, size = "magnet:?" + v2_magnet, v2_size
    if size >= 2**63:
        raise ValueError("Torrent content size is too large")
    check_paths()
    # The magnet is an identity reference only. Always submit the original bytes,
    # including private flags, tracker tiers, web seeds and v2 piece layers.
    return TorrentMetainfo(data, magnet, name.decode("utf-8", errors="replace"), size)
