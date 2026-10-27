"""A small TIFF/EXIF writer: lays out IFDs and data blobs, resolving offsets by name. Used by the
self-test to generate synthetic DNGs and by tests to build rich EXIF structures."""

from __future__ import annotations

import struct

BYTE, ASCII, SHORT, LONG, RATIONAL, UNDEFINED, SRATIONAL = 1, 2, 3, 4, 5, 7, 10


class Ifd:
    def __init__(self, e: str = "<"):
        self.e = e
        self.entries: list[tuple[int, int, int, bytes | str]] = []

    def _add(self, tag: int, typ: int, count: int, payload) -> Ifd:
        self.entries.append((tag, typ, count, payload))
        return self

    def ascii(self, tag, text: str) -> Ifd:
        b = text.encode("utf-8") + b"\x00"
        return self._add(tag, ASCII, len(b), b)

    def short(self, tag, value: int) -> Ifd:
        return self._add(tag, SHORT, 1, struct.pack(self.e + "H", value))

    def shorts(self, tag, values) -> Ifd:
        return self._add(tag, SHORT, len(values), struct.pack(self.e + f"{len(values)}H", *values))

    def long(self, tag, value: int) -> Ifd:
        return self._add(tag, LONG, 1, struct.pack(self.e + "I", value))

    def rationals(self, tag, pairs) -> Ifd:
        b = b"".join(struct.pack(self.e + "II", n, d) for n, d in pairs)
        return self._add(tag, RATIONAL, len(pairs), b)

    def srationals(self, tag, pairs) -> Ifd:
        b = b"".join(struct.pack(self.e + "ii", n, d) for n, d in pairs)
        return self._add(tag, SRATIONAL, len(pairs), b)

    def undefined(self, tag, data: bytes) -> Ifd:
        return self._add(tag, UNDEFINED, len(data), data)

    def byte(self, tag, data: bytes) -> Ifd:
        return self._add(tag, BYTE, len(data), bytes(data))

    def pointer(self, tag, name: str) -> Ifd:
        """LONG offset; `name` is the name of an IFD or a data blob."""
        return self._add(tag, LONG, 1, name)

    def size(self) -> int:
        data = sum(len(p) + (len(p) & 1) for *_, p in self.entries if isinstance(p, bytes) and len(p) > 4)
        return 2 + 12 * len(self.entries) + 4 + data


def build_tiff(
    ifds: list[tuple[str, Ifd]],
    blobs: dict[str, bytes] | None = None,
    e: str = "<",
    next_ifd: dict[str, str] | None = None,
) -> bytes:
    """Lays out the IFDs in order, then the data blobs, and returns the TIFF bytes."""
    blobs = blobs or {}
    next_ifd = next_ifd or {}
    offsets: dict[str, int] = {}
    pos = 8
    for name, ifd in ifds:
        offsets[name] = pos
        pos += ifd.size()
    for name, blob in blobs.items():
        offsets[name] = pos
        pos += len(blob) + (len(blob) & 1)
    out = bytearray(b"II*\x00" if e == "<" else b"MM\x00*")
    out += struct.pack(e + "I", 8)
    for name, ifd in ifds:
        data_pos = offsets[name] + 2 + 12 * len(ifd.entries) + 4
        entries = bytearray(struct.pack(e + "H", len(ifd.entries)))
        data = bytearray()
        for tag, typ, count, payload in sorted(ifd.entries, key=lambda x: x[0]):
            if isinstance(payload, str):
                payload = struct.pack(e + "I", offsets[payload])
            if len(payload) <= 4:
                entries += struct.pack(e + "HHI", tag, typ, count) + payload.ljust(4, b"\x00")
            else:
                entries += struct.pack(e + "HHII", tag, typ, count, data_pos + len(data))
                data += payload + (b"\x00" if len(payload) & 1 else b"")
        entries += struct.pack(e + "I", offsets[next_ifd[name]] if name in next_ifd else 0)
        out += entries + data
    for blob in blobs.values():
        out += blob + (b"\x00" if len(blob) & 1 else b"")
    return bytes(out)
