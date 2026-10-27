"""MP4 / MOV / 3GP (ISO BMFF, QuickTime) box structure.

FFmpeg reads part of the metadata but does not expose all of it: camera makers'
own boxes (GoPro serial number, Canon/Nikon thumbnails and EXIF, Samsung's
trailer data …), timed metadata tracks (iPhone face detection, GoPro/Sony/DJI
GPS logs) and the dates in track headers are read here directly from the boxes.
Used both to show the user what the file contains and to verify that the
cleaned output really is clean.

The file is not read in full: only the headers of top-level boxes are read,
'moov' (the track definitions) and small boxes are loaded into memory; video/audio
data ('mdat') is skipped.
"""

from __future__ import annotations

import math
import os
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ...i18n import t
from ..meta.model import FormatError

MOOV_LIMIT = 512 * 1024 * 1024
SMALL_BOX = 4 * 1024 * 1024
EPOCH_1904 = datetime(1904, 1, 1)
XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")


class BoxError(FormatError):
    pass


@dataclass
class Track:
    handler: bytes = b""  # 'vide', 'soun', 'meta', 'text', 'sbtl', 'tmcd' …
    entry: bytes = b""  # type of the first sample description: 'avc1', 'hvc1', 'mp4a', 'gpmd', 'mebx' …
    handler_name: str = ""
    width: float = 0.0
    height: float = 0.0
    rotation: int = 0  # clockwise rotation for display (degrees)
    mirrored: bool = False
    matrix_ok: bool = True  # is the matrix only a rotation by a multiple of 90° / mirroring?
    created: int = 0  # seconds since 1904 (tkhd)
    modified: int = 0
    media_created: int = 0  # (mdhd)
    media_modified: int = 0
    udta: list[bytes] = field(default_factory=list)
    samples: int = 0


@dataclass
class Structure:
    top: list[tuple[bytes, int, int]] = field(default_factory=list)  # (type, offset, size)
    tracks: list[Track] = field(default_factory=list)
    udta: list[bytes] = field(default_factory=list)  # children of moov/udta and of a top-level udta
    keys: list[str] = field(default_factory=list)  # meta/keys (QuickTime 'mdta' keys)
    items: list[bytes] = field(default_factory=list)  # meta/ilst items
    uuids: list[bytes] = field(default_factory=list)
    xmp: bool = False
    created: int = 0  # mvhd
    modified: int = 0
    trailing: int = 0  # number of (non-box) bytes left after the last box
    brands: list[bytes] = field(default_factory=list)

    @property
    def video(self) -> Track | None:
        return next((t for t in self.tracks if t.handler == b"vide"), None)


def _boxes(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", data, pos)[0]
        typ = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return  # broken/truncated box: whatever could be read is enough
        if typ == b"uuid":
            header += 16
        yield typ, pos + header, pos + size, pos
        pos += size


def matrix_rotation(m: tuple[int, ...]) -> tuple[int, bool, bool]:
    """tkhd matrix [a b u; c d v; x y w] → (clockwise rotation, mirrored?, exact multiple of 90°?).
    A portrait iPhone video has a=0, b=1, c=-1, d=0: 90° clockwise. A mirrored matrix is a horizontal
    flip followed by the rotation: QuickTime's "Flip Horizontal" (a=-1, d=1) turns nothing."""
    a, b, c, d = (v / 65536 for v in (m[0], m[1], m[3], m[4]))
    det = a * d - b * c
    exact = all(abs(v - round(v)) < 1e-3 for v in (a, b, c, d)) and abs(abs(det) - 1) < 1e-3
    if det < 0:  # the flip (x → -x) taken out first
        a, b = -a, -b
    rotation = int(round(math.degrees(math.atan2(b, a)) / 90.0)) * 90 % 360
    return rotation, det < 0, exact


def _times(data: bytes, ps: int, pe: int) -> tuple[int, int]:
    """(creation, modification) time of an mvhd/tkhd/mdhd box: seconds since 1904, 32 or 64 bits."""
    if data[ps] == 1:
        return struct.unpack_from(">QQ", data, ps + 4) if ps + 20 <= pe else (0, 0)
    return struct.unpack_from(">II", data, ps + 4) if ps + 12 <= pe else (0, 0)


def _parse_tkhd(data: bytes, ps: int, pe: int, t: Track) -> None:
    if ps + 4 > pe:
        return
    version = data[ps]
    if version == 1:
        if ps + 96 > pe:
            return
        mpos = ps + 52
    else:
        if ps + 84 > pe:
            return
        mpos = ps + 40
    t.created, t.modified = _times(data, ps, pe)
    matrix = struct.unpack_from(">9i", data, mpos)
    t.rotation, t.mirrored, t.matrix_ok = matrix_rotation(matrix)
    w, h = struct.unpack_from(">II", data, mpos + 36)
    t.width, t.height = w / 65536, h / 65536


def _parse_hdlr(data: bytes, ps: int, pe: int) -> tuple[bytes, str]:
    if ps + 24 > pe:
        return b"", ""
    handler = data[ps + 8:ps + 12]
    raw = data[ps + 24:pe]
    if raw[:1] and raw[0] == len(raw) - 1:  # QuickTime: Pascal string
        raw = raw[1:]
    return handler, raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


def _meta_start(data: bytes, ps: int, pe: int) -> int:
    """The 'meta' box has version/flags fields in ISO files but not in QuickTime."""
    if data[ps + 4:ps + 8] in (b"hdlr", b"keys", b"ilst", b"xml ", b"iinf"):
        return ps
    return ps + 4


def _parse_meta(data: bytes, ps: int, pe: int, st: Structure) -> None:
    for typ, cs, ce, _ in _boxes(data, _meta_start(data, ps, pe), pe):
        if typ == b"keys" and cs + 8 <= ce:
            count = struct.unpack_from(">I", data, cs + 4)[0]
            pos = cs + 8
            for _ in range(count):
                if pos + 8 > ce:
                    break
                size = struct.unpack_from(">I", data, pos)[0]
                if size < 8 or pos + size > ce:
                    break
                st.keys.append(data[pos + 8:pos + size].decode("utf-8", "replace"))
                pos += size
        elif typ == b"ilst":
            st.items.extend(t for t, *_ in _boxes(data, cs, ce))
        elif typ in (b"xml ", b"XMP_"):
            st.xmp = True


def _parse_udta(data: bytes, ps: int, pe: int, st: Structure, into: list[bytes]) -> None:
    for typ, cs, ce, _ in _boxes(data, ps, pe):
        into.append(typ)
        if typ == b"meta":
            _parse_meta(data, cs, ce, st)
        elif typ == b"XMP_":
            st.xmp = True


def _parse_trak(data: bytes, ps: int, pe: int, st: Structure) -> None:
    t = Track()
    for typ, cs, ce, _ in _boxes(data, ps, pe):
        if typ == b"tkhd":
            _parse_tkhd(data, cs, ce, t)
        elif typ == b"udta":
            _parse_udta(data, cs, ce, st, t.udta)
        elif typ == b"meta":
            _parse_meta(data, cs, ce, st)
        elif typ == b"mdia":
            for mt, ms, me, _ in _boxes(data, cs, ce):
                if mt == b"hdlr":
                    t.handler, t.handler_name = _parse_hdlr(data, ms, me)
                elif mt == b"mdhd" and ms + 12 <= me:
                    t.media_created, t.media_modified = _times(data, ms, me)
                elif mt == b"minf":
                    _parse_minf(data, ms, me, t)
    st.tracks.append(t)


def _parse_minf(data: bytes, ps: int, pe: int, t: Track) -> None:
    for typ, cs, ce, _ in _boxes(data, ps, pe):
        if typ != b"stbl":
            continue
        for st_typ, ss, se, _ in _boxes(data, cs, ce):
            if st_typ == b"stsd" and ss + 16 <= se:
                t.entry = data[ss + 12:ss + 16]
            elif st_typ == b"stsz" and ss + 12 <= se:
                t.samples = struct.unpack_from(">I", data, ss + 8)[0]
            elif st_typ == b"stz2" and ss + 12 <= se:
                t.samples = struct.unpack_from(">I", data, ss + 8)[0]


def parse_moov(data: bytes, st: Structure) -> None:
    for typ, cs, ce, start in _boxes(data, 0, len(data)):
        if typ == b"mvhd" and cs + 12 <= ce:
            st.created, st.modified = _times(data, cs, ce)
        elif typ == b"trak":
            _parse_trak(data, cs, ce, st)
        elif typ == b"udta":
            _parse_udta(data, cs, ce, st, st.udta)
        elif typ == b"meta":
            _parse_meta(data, cs, ce, st)
        elif typ == b"uuid":
            uid = data[start + 8:start + 24]
            st.uuids.append(uid)
            st.xmp = st.xmp or uid == XMP_UUID


def read_structure(path: str) -> Structure:
    """The file's box structure. BoxError if it is not MP4/MOV or 'moov' is missing."""
    st = Structure()
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        pos = 0
        moov = None
        while pos + 8 <= size:
            fh.seek(pos)
            head = fh.read(16)
            if len(head) < 8:
                break
            box = struct.unpack_from(">I", head, 0)[0]
            typ = head[4:8]
            header = 8
            if box == 1 and len(head) >= 16:
                box = struct.unpack_from(">Q", head, 8)[0]
                header = 16
            elif box == 0:
                box = size - pos
            if box < header or not _plausible(typ) or pos + box > size:
                st.trailing = size - pos
                break
            st.top.append((typ, pos, box))
            if typ == b"moov":
                if box > MOOV_LIMIT:
                    raise BoxError(t("boxes.moov_too_big"))
                fh.seek(pos + header)
                moov = fh.read(box - header)
            elif typ == b"ftyp":
                fh.seek(pos + header)
                body = fh.read(min(box - header, 256))
                st.brands = [body[:4]] + [body[i:i + 4] for i in range(8, len(body) - 3, 4)]
            elif typ in (b"uuid", b"udta", b"meta") and box <= SMALL_BOX:
                fh.seek(pos)
                blob = fh.read(box)
                if typ == b"uuid":
                    uid = blob[8:24]
                    st.uuids.append(uid)
                    st.xmp = st.xmp or uid == XMP_UUID
                elif typ == b"udta":
                    _parse_udta(blob, header, len(blob), st, st.udta)
                else:
                    _parse_meta(blob, header, len(blob), st)
            pos += box
    if moov is None:
        raise BoxError(t("boxes.no_moov_damaged"))
    parse_moov(moov, st)
    return st


def _plausible(typ: bytes) -> bool:
    """Is the box type four printable characters (or one of QuickTime's ©-prefixed types)?"""
    return all(32 <= b < 127 or b == 0xA9 for b in typ)


def end_presentation(path: str, seconds: float) -> None:
    """Makes the file's presentation end after `seconds`: every track's edit list ('elst') and
    duration, and the movie's duration, are shortened in place (the sizes of the boxes do not
    change). Samples past the end stay in the file; players do not show them. Tracks without an
    edit list are left as they are."""
    with open(path, "r+b") as fh:
        size = os.fstat(fh.fileno()).st_size
        pos = 0
        while pos + 8 <= size:
            fh.seek(pos)
            head = fh.read(16)
            box, typ, header = struct.unpack_from(">I", head, 0)[0], head[4:8], 8
            if box == 1:
                box, header = struct.unpack_from(">Q", head, 8)[0], 16
            elif box == 0:
                box = size - pos
            if box < header:
                break
            if typ == b"moov":
                fh.seek(pos + header)
                moov = bytearray(fh.read(box - header))
                _end_moov(moov, seconds)
                fh.seek(pos + header)
                fh.write(moov)
                return
            pos += box
    raise BoxError(t("boxes.no_moov"))


def _end_moov(moov: bytearray, seconds: float) -> None:
    mvhd = next(((cs, ce) for typ, cs, ce, _ in _boxes(moov, 0, len(moov)) if typ == b"mvhd"), None)
    if mvhd is None:
        return
    version = moov[mvhd[0]]
    scale_at = mvhd[0] + (20 if version == 1 else 12)
    timescale = struct.unpack_from(">I", moov, scale_at)[0]
    limit = math.floor(seconds * timescale + 1e-6)  # rounded down: a frame exactly at the end is not shown
    longest = 0
    for typ, cs, ce, _ in _boxes(moov, 0, len(moov)):
        if typ != b"trak":
            continue
        longest = max(longest, _end_trak(moov, cs, ce, limit))
    _put_duration(moov, scale_at + 4, version, longest)


def _end_trak(moov: bytearray, ps: int, pe: int, limit: int) -> int:
    """Shortens one track; returns its (new) duration in the movie's timescale."""
    tkhd = elst = None
    for typ, cs, ce, _ in _boxes(moov, ps, pe):
        if typ == b"tkhd":
            tkhd = cs
        elif typ == b"edts":
            elst = next((es for et, es, _, _ in _boxes(moov, cs, ce) if et == b"elst"), None)
    if tkhd is None:
        return 0
    tk_version = moov[tkhd]
    duration_at = tkhd + (28 if tk_version == 1 else 20)
    duration = _get_duration(moov, duration_at, tk_version)
    if elst is None:
        return duration
    version = moov[elst]
    count = struct.unpack_from(">I", moov, elst + 4)[0]
    entry = 20 if version == 1 else 12
    elapsed = 0
    for i in range(count):
        at = elst + 8 + i * entry
        length = _get_duration(moov, at, version)
        length = max(0, min(length, limit - elapsed))
        _put_duration(moov, at, version, length)
        elapsed += length
    duration = min(duration, elapsed)
    _put_duration(moov, duration_at, tk_version, duration)
    return duration


def _get_duration(data: bytearray, at: int, version: int) -> int:
    return struct.unpack_from(">Q" if version == 1 else ">I", data, at)[0]


def _put_duration(data: bytearray, at: int, version: int, value: int) -> None:
    struct.pack_into(">Q" if version == 1 else ">I", data, at, value)


def qt_datetime(seconds: int) -> datetime | None:
    """QuickTime time (seconds since 1904, should be UTC). None if 0 or implausible."""
    if seconds <= 0:
        return None
    try:
        when = EPOCH_1904 + timedelta(seconds=seconds)
    except OverflowError:
        return None
    return when if 1971 <= when.year <= 2100 else None


# Known maker boxes: {type: the key of its description}; the description says when a box may hold a
# location.
MAKER_BOXES: dict[bytes, str] = {
    b"FIRM": "boxes.maker.gopro_firmware",
    b"CAME": "boxes.maker.gopro_camera_id",
    b"MUID": "boxes.maker.gopro_file_id",
    b"GUMI": "boxes.maker.gopro_file_id",
    b"GPMF": "boxes.maker.gopro_settings",
    b"LENS": "boxes.maker.gopro_lens",
    b"SETT": "boxes.maker.gopro_shooting",
    b"HMMT": "boxes.maker.gopro_highlights",
    b"BCID": "boxes.maker.gopro_id",
    b"CNCV": "boxes.maker.canon_codec",
    b"CNMN": "boxes.maker.canon_model",
    b"CNFV": "boxes.maker.canon_firmware",
    b"CNTH": "boxes.maker.canon_thumbnail",
    b"CNDA": "boxes.maker.canon_data",
    b"NCDT": "boxes.maker.nikon_data",
    b"NCTH": "boxes.maker.nikon_thumbnail",
    b"PANA": "boxes.maker.panasonic",
    b"PENT": "boxes.maker.pentax",
    b"FFMV": "boxes.maker.fujifilm",
    b"MVTG": "boxes.maker.fujifilm",
    b"LEIC": "boxes.maker.leica",
    b"DcMD": "boxes.maker.kodak",
    b"smta": "boxes.maker.samsung_info",
    b"SDLN": "boxes.maker.samsung",
    b"QVMI": "boxes.maker.casio",
    b"TAGS": "boxes.maker.other",
    b"GPS ": "boxes.maker.gps",
    b"Xtra": "boxes.maker.windows",
    b"XMP_": "boxes.maker.xmp",
}

LOCATION_BOXES = {b"\xa9xyz", b"loci", b"GPS ", b"gps "}
# Timed metadata tracks: {sample entry: the key of its description}; LOCATION_TIMED log a location.
TIMED_METADATA: dict[bytes, str] = {
    b"gpmd": "boxes.timed.gopro",
    b"camm": "boxes.timed.camm",
    b"rtmd": "boxes.timed.sony",
    b"djmd": "boxes.timed.dji",
    b"dbgi": "boxes.timed.dji_debug",
    b"mebx": "boxes.timed.mebx",
    b"tmcd": "boxes.timed.timecode",
}
LOCATION_TIMED = {b"gpmd", b"camm", b"rtmd", b"djmd"}
