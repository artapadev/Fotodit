"""Information embedded inside the video bitstream.

Even when the container's (MP4, MOV …) metadata is stripped, some information
remains in the video stream itself:

* **H.264 / HEVC SEI "user data unregistered" messages** (type 5): AVCHD and
  Sony XAVC cameras write an "MDPM" block here (capture date, time zone,
  camera model, exposure, GPS location). The x264 encoder puts its settings
  line here, and some cameras/apps their own data. These messages are removed
  when metadata is stripped; other SEI messages that affect decoding (HDR
  info, recovery point, timing …) are left untouched.
* **MPEG-1/2 "user data"** (start code 0x1B2): camera/recorder info,
  caption data.
* **MJPEG frames**: every frame is a JPEG and can carry APPn (EXIF, XMP …) and
  comment segments.
* **DV frames**: camcorders write the recording date and time into every frame
  (the subcode, VAUX and AAUX packs).
* **The stream header of camera AVIs**: EXIF in the 'strd' chunk, which FFmpeg hands
  over as the video track's codec configuration (`camera_extradata`).

The picture data itself does not change; only these blocks are removed from the packet.
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta

USER_DATA_UNREGISTERED = 5
MDPM_UUID = bytes.fromhex("17ee8c60f84d11d98cd60800200c9a66")
MDPM_MAGIC = MDPM_UUID + b"MDPM"

H264_SEI = 6
HEVC_PREFIX_SEI = 39
HEVC_SUFFIX_SEI = 40

_START3 = bytes([0, 0, 1])
_EPB = bytes([0, 0, 3])


# ------------------------------------------------------------------ NAL units


def nal_type(codec: str, nal: bytes) -> int:
    if not nal:
        return -1
    return (nal[0] >> 1) & 0x3F if codec == "hevc" else nal[0] & 0x1F


def _is_sei(codec: str, nal: bytes) -> bool:
    t = nal_type(codec, nal)
    return t in (HEVC_PREFIX_SEI, HEVC_SUFFIX_SEI) if codec == "hevc" else t == H264_SEI


def split_length_prefixed(data: bytes, size: int) -> list[bytes] | None:
    """AVCC/HVCC format (MP4/MOV/MKV): each NAL is preceded by a `size`-byte length."""
    nals, pos, n = [], 0, len(data)
    while pos < n:
        if pos + size > n:
            return None
        length = int.from_bytes(data[pos:pos + size], "big")
        pos += size
        if length == 0 or pos + length > n:
            return None
        nals.append(data[pos:pos + length])
        pos += length
    return nals


def join_length_prefixed(nals: list[bytes], size: int) -> bytes:
    return b"".join(len(n).to_bytes(size, "big") + n for n in nals)


def split_annexb(data: bytes) -> list[bytes]:
    """Annex B format (MPEG-TS, encoder output): NALs are separated by 00 00 01."""
    starts = []
    pos = data.find(_START3)
    while pos >= 0:
        starts.append(pos + 3)
        pos = data.find(_START3, pos + 3)
    nals = []
    for i, s in enumerate(starts):
        end = starts[i + 1] - 3 if i + 1 < len(starts) else len(data)
        nal = data[s:end]
        while nal.endswith(b"\x00") and i + 1 < len(starts):
            nal = nal[:-1]  # leading zero of the next start code (00 00 00 01)
        if nal:
            nals.append(nal)
    return nals


def join_annexb(nals: list[bytes]) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


def unescape(nal: bytes) -> bytes:
    """Removes emulation prevention bytes (00 00 03): NAL → RBSP."""
    if _EPB not in nal:
        return nal
    out = bytearray()
    zeros = 0
    for b in nal:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def escape(rbsp: bytes) -> bytes:
    """RBSP → NAL: inserts an emulation prevention byte into 00 00 0x (x ≤ 3) sequences."""
    out = bytearray()
    zeros = 0
    for b in rbsp:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


# ------------------------------------------------------------------------ SEI


def sei_messages(rbsp: bytes, start: int) -> list[tuple[int, bytes]] | None:
    """Messages of an SEI NAL (after its header): [(type, payload)]. None if malformed."""
    msgs = []
    pos, n = start, len(rbsp)
    while pos < n:
        if rbsp[pos] == 0x80 and not any(rbsp[pos + 1:]):
            break  # rbsp_trailing_bits
        values = []
        for _ in range(2):  # payloadType, payloadSize: numbers extended with 0xFF bytes
            v = 0
            while pos < n and rbsp[pos] == 0xFF:
                v += 255
                pos += 1
            if pos >= n:
                return None
            v += rbsp[pos]
            pos += 1
            values.append(v)
        ptype, size = values
        if pos + size > n:
            return None
        msgs.append((ptype, rbsp[pos:pos + size]))
        pos += size
    return msgs


def _ff_coded(value: int) -> bytes:
    return b"\xff" * (value // 255) + bytes([value % 255])


def build_sei(header: bytes, msgs: list[tuple[int, bytes]]) -> bytes:
    body = b"".join(_ff_coded(t) + _ff_coded(len(p)) + p for t, p in msgs)
    return escape(header + body + b"\x80")


class SeiScrubber:
    """Removes user data SEI messages from H.264/HEVC packets.

    `extradata`: the codec configuration (avcC/hvcC or Annex B); it determines the
    packet format. `collect`: keep the first few removed MDPM blocks (to read the
    date/GPS)."""

    def __init__(self, codec: str, extradata: bytes | None, collect: bool = False):
        self.codec = codec
        self.length_size = None
        ex = bytes(extradata or b"")
        if ex[:1] == b"\x01":
            if codec == "h264" and len(ex) >= 5:
                self.length_size = (ex[4] & 3) + 1
            elif codec == "hevc" and len(ex) >= 22:
                self.length_size = (ex[21] & 3) + 1
        self.removed = 0
        self.collect = collect
        self.mdpm: list[bytes] = []
        self.user_data: list[bytes] = []  # first bytes of unrecognized user data (for info)

    def _scrub_nal(self, nal: bytes) -> bytes | None:
        """The cleaned SEI NAL; None if it is all user data or malformed."""
        header = nal[:2] if self.codec == "hevc" else nal[:1]
        rbsp = unescape(nal)
        msgs = sei_messages(rbsp, len(header))
        if msgs is None:
            self.removed += 1
            return None
        kept = []
        for ptype, payload in msgs:
            if ptype == USER_DATA_UNREGISTERED:
                self.removed += 1
                if self.collect:
                    if payload.startswith(MDPM_MAGIC):
                        if len(self.mdpm) < 4:
                            self.mdpm.append(payload)
                    elif len(self.user_data) < 4:
                        self.user_data.append(payload[:120])
                continue
            kept.append((ptype, payload))
        if len(kept) == len(msgs):
            return nal
        return build_sei(header, kept) if kept else None

    def scrub_nals(self, nals: list[bytes]) -> list[bytes] | None:
        """None if nothing changed."""
        changed = False
        out = []
        for nal in nals:
            if _is_sei(self.codec, nal):
                cleaned = self._scrub_nal(nal)
                if cleaned is not nal:
                    changed = True
                    if cleaned is None:
                        continue
                    nal = cleaned
            out.append(nal)
        return out if changed else None

    def scrub(self, data: bytes) -> bytes | None:
        """The cleaned packet; None if nothing changed."""
        if self.length_size:
            nals = split_length_prefixed(data, self.length_size)
            if nals is None:
                return None  # an unparsable packet is left as is (the decoder cannot read it either)
            out = self.scrub_nals(nals)
            return None if out is None else join_length_prefixed(out, self.length_size)
        if _START3 not in data[:64]:
            return None
        out = self.scrub_nals(split_annexb(data))
        return None if out is None else join_annexb(out)


def scrub_extradata(codec: str, extradata: bytes | None) -> bytes | None:
    """Removes user data SEIs from the encoder's header data (hvcC or Annex B); x265 writes its
    version/settings line there. None if unchanged."""
    ex = bytes(extradata or b"")
    if not ex:
        return None
    if ex[0] == 1:  # avcC carries no SEI; hvcC can
        return scrub_hvcc(ex) if codec == "hevc" else None
    return SeiScrubber(codec, None).scrub(ex)


def scrub_hvcc(extradata: bytes) -> bytes | None:
    """Removes user data from the SEI arrays of an hvcC configuration (None if unchanged)."""
    ex = bytes(extradata)
    if len(ex) < 23 or ex[0] != 1:
        return None
    count = ex[22]
    pos = 23
    arrays = []
    for _ in range(count):
        if pos + 3 > len(ex):
            return None
        head = ex[pos:pos + 1]
        typ = ex[pos] & 0x3F
        n = struct.unpack_from(">H", ex, pos + 1)[0]
        pos += 3
        nals = []
        for _ in range(n):
            if pos + 2 > len(ex):
                return None
            size = struct.unpack_from(">H", ex, pos)[0]
            nals.append(ex[pos + 2:pos + 2 + size])
            pos += 2 + size
        arrays.append((head, typ, nals))
    scrubber = SeiScrubber("hevc", None)
    changed = False
    rebuilt = []
    for head, typ, nals in arrays:
        if typ in (HEVC_PREFIX_SEI, HEVC_SUFFIX_SEI):
            out = scrubber.scrub_nals(nals)
            if out is not None:
                changed = True
                nals = out
        if nals:
            rebuilt.append(head + struct.pack(">H", len(nals)) + b"".join(struct.pack(">H", len(x)) + x for x in nals))
        else:
            changed = True
    if not changed:
        return None
    return ex[:22] + bytes([len(rebuilt)]) + b"".join(rebuilt)


# ----------------------------------------------------------------------- MDPM


def mdpm_tags(payload: bytes) -> dict[int, bytes]:
    """Tags in an MDPM block: {tag: 4 bytes}."""
    if not payload.startswith(MDPM_MAGIC) or len(payload) < 21:
        return {}
    count = payload[20]
    tags = {}
    pos = 21
    for _ in range(count):
        if pos + 5 > len(payload):
            break
        tags.setdefault(payload[pos], payload[pos + 1:pos + 5])
        pos += 5
    return tags


def _bcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 0x0F)


def mdpm_datetime(tags: dict[int, bytes]) -> tuple[datetime, timedelta | None] | None:
    """Capture time (the camera's local time) and time zone offset."""
    a, b = tags.get(0x18), tags.get(0x19)
    if not a or not b:
        return None
    tz = a[0]
    try:
        when = datetime(_bcd(a[1]) * 100 + _bcd(a[2]), _bcd(a[3]), _bcd(b[0]), _bcd(b[1]), _bcd(b[2]), _bcd(b[3]))
    except ValueError:
        return None
    offset = None
    if tz not in (0x00, 0xFF):
        minutes = ((tz >> 1) & 0x0F) * 60 + (30 if tz & 0x01 else 0)
        offset = timedelta(minutes=-minutes if tz & 0x20 else minutes)
    return when, offset


def _rational16(v: bytes) -> float | None:
    num, den = struct.unpack(">HH", v)
    return num / den if den else None


def mdpm_gps(tags: dict[int, bytes]) -> tuple[float, float] | None:
    """GPS location in MDPM (degrees). 0xB1-0xB4 latitude, 0xB5-0xB8 longitude."""
    def coord(ref_tag: int, first: int, negative: bytes) -> float | None:
        parts = [tags.get(first + i) for i in range(3)]
        if not all(parts):
            return None
        values = [_rational16(p) for p in parts]
        if any(v is None for v in values):
            return None
        deg = values[0] + values[1] / 60 + values[2] / 3600
        ref = tags.get(ref_tag, b"")
        return -deg if ref[:1] == negative else deg

    lat = coord(0xB1, 0xB2, b"S")
    lon = coord(0xB5, 0xB6, b"W")
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    return lat, lon


def mdpm_has_gps(tags: dict[int, bytes]) -> bool:
    return any(0xB0 <= t <= 0xCF for t in tags)


def mdpm_camera(tags: dict[int, bytes]) -> str | None:
    """Camera maker from the MDPM maker code (0xE0: first two bytes)."""
    maker = tags.get(0xE0)
    if not maker:
        return None
    return {0x0103: "Panasonic", 0x0108: "Sony", 0x1011: "Canon", 0x1104: "JVC"}.get(
        struct.unpack(">H", maker[:2])[0])


# -------------------------------------------------------------- MPEG-2 / MJPEG


def strip_mpeg_user_data(data: bytes) -> bytes | None:
    """Removes user_data (00 00 01 B2 …) blocks from an MPEG-1/2 video packet."""
    marker = bytes([0, 0, 1, 0xB2])
    pos = data.find(marker)
    if pos < 0:
        return None
    out = bytearray()
    last = 0
    while pos >= 0:
        out += data[last:pos]
        end = data.find(_START3, pos + 4)
        last = end if end >= 0 else len(data)
        pos = data.find(marker, last)
    out += data[last:]
    return bytes(out)


def strip_mjpeg(data: bytes) -> bytes | None:
    """Removes APPn (EXIF, XMP …) and comment segments from an MJPEG frame. The JFIF and
    AVI1 (field order info) APP0 segments and Adobe APP14 (color transform) are kept;
    other APP0 segments such as JFXX (may carry a thumbnail) are removed."""
    from ..meta import jpeg as jpeg_meta

    try:
        layout = jpeg_meta.parse(data)
    except Exception:  # noqa: BLE001 - a broken frame is left as is
        return None
    out = bytearray(data[:2])
    changed = False
    for seg in layout.segments[1:]:
        m = seg.marker
        if 0xE0 <= m <= 0xEF or m == 0xFE:
            payload = layout.payload(seg)
            keep = (m == 0xE0 and payload[:5] in (b"JFIF" + bytes(1), b"AVI1" + payload[4:5])) or (
                m == 0xEE and payload.startswith(b"Adobe"))
            if not keep:
                changed = True
                continue
        out += data[seg.start:seg.end]
    if not changed:
        return None
    end = layout.segments[-1].end if layout.segments else len(data)
    out += data[end:]
    return bytes(out)


# ------------------------------------------------------- camera data in the AVI stream header

STRD_SIGNATURES = (b"AVIF", b"CASI", b"Zora")  # the camera data FFmpeg knows in an AVI 'strd' chunk
_AVID = bytes.fromhex("2c00000018000000")  # Avid's field information: the only configuration MJPEG uses


def camera_extradata(codec: str, extradata: bytes | None) -> bool:
    """Is a video track's codec configuration really camera data? Camera AVIs keep EXIF (date, model,
    sometimes the location) in the stream header ('strd' chunk); FFmpeg hands it over as the track's
    codec configuration, which a copy would write back into its own header. An MJPEG frame carries
    everything its decoder needs, so any configuration on it other than Avid's is such data."""
    ex = bytes(extradata or b"")
    if not ex:
        return False
    return ex[:4] in STRD_SIGNATURES or (codec == "mjpeg" and not ex.startswith(_AVID))


# ------------------------------------------------------------------------ DV

DV_DATES = (0x52, 0x53, 0x62, 0x63)  # recording date and time packs: AAUX (sound) and VAUX (picture)
_DV_BLOCK = 80


def _dv_packs(buf):
    """Offsets of the 5-byte packs of a DV frame: 6 in each subcode block, 15 in each VAUX block and 1
    in each audio block. A frame is a series of 80-byte blocks; the top 3 bits of a block's first byte
    say what it is (1: subcode, 2: VAUX, 3: audio)."""
    import numpy as np

    n = len(buf) // _DV_BLOCK
    kinds = buf[:n * _DV_BLOCK:_DV_BLOCK] >> 5
    starts = np.arange(n, dtype=np.int64) * _DV_BLOCK
    subcode = starts[kinds == 1][:, None] + 6 + 8 * np.arange(6)
    vaux = starts[kinds == 2][:, None] + 3 + 5 * np.arange(15)
    audio = starts[kinds == 3] + 3
    return np.concatenate([subcode.ravel(), vaux.ravel(), audio])


def _dv_date_packs(buf):
    import numpy as np

    offsets = _dv_packs(buf)
    return offsets[np.isin(buf[offsets], DV_DATES)]


def dv_has_dates(data: bytes) -> bool:
    """Does the DV frame carry its recording date or time?"""
    import numpy as np

    return bool(len(_dv_date_packs(np.frombuffer(data, np.uint8))))


def strip_dv(data: bytes) -> bytes | None:
    """Removes the recording date and time from a DV frame: those packs become "no information" packs
    (all bytes 0xFF), as a camcorder writes them when its clock is not set. The picture and the sound
    do not change. None if the frame has no date."""
    import numpy as np

    buf = np.frombuffer(data, np.uint8)
    found = _dv_date_packs(buf)
    if not len(found):
        return None
    out = buf.copy()
    out[found[:, None] + np.arange(5)] = 0xFF
    return out.tobytes()
