"""Tiny stored RAR4/RAR5 fixtures assembled from the published RAR formats."""
import io
import stat
import struct
import zipfile
import zlib


def rar4(members, *, flags=0):
    def header(kind, flags, payload=b""):
        body = struct.pack("<BHH", kind, flags, len(payload) + 7) + payload
        return struct.pack("<H", zlib.crc32(body) & 0xffff) + body
    result = b"Rar!\x1a\x07\x00" + header(0x73, flags, b"\0" * 6)
    for name, data in members:
        encoded = name.encode()
        payload = struct.pack("<LLBLLBBHL", len(data), len(data), 3, zlib.crc32(data),
                              0, 20, 0x30, len(encoded), stat.S_IFREG | 0o644) + encoded
        result += header(0x74, 0x8000, payload) + data
    return result + header(0x7b, 0)


def vint(value):
    data = bytearray()
    while value > 127:
        data.append((value & 127) | 128)
        value >>= 7
    data.append(value)
    return bytes(data)


def rar5(members, *, flags=0):
    def header(payload):
        body = vint(len(payload)) + payload
        return struct.pack("<L", zlib.crc32(body)) + body
    result = b"Rar!\x1a\x07\x01\x00" + header(b"\x01\x00" + vint(flags))
    for name, data in members:
        encoded = name.encode()
        payload = (b"\x02\x02" + vint(len(data)) + b"\x04" + vint(len(data))
                   + vint(stat.S_IFREG | 0o644) + struct.pack("<L", zlib.crc32(data))
                   + b"\x00\x01" + vint(len(encoded)) + encoded)
        result += header(payload) + data
    return result + header(b"\x05\x00\x00")


def zip_archive(members, *, compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return output.getvalue()
