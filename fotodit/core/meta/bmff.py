"""Box scanner for HEIF/HEIC/AVIF (ISO base media file format).

In these formats metadata is stored as separate items: the 'Exif' item, a
'mime' item for XMP, thumbnails ('thmb' reference), auxiliary images
(depth map, HDR gain map, portrait mattes: the 'auxC' property), the
ISO 21496-1 gain map ('tmap') and the ICC profile in the 'colr' box.

Metadata is not removed from HEIC/AVIF losslessly; the file is re-encoded
(or converted to JPG). This module is used both to show the user what was found
and to verify that the produced file really is clean.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ...i18n import t
from .model import FormatError, Kind, ScanResult

C2PA_UUID = bytes.fromhex("d8fec3d61b0e483c92975828877ec481")
XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")
IMAGE_TYPES = {b"hvc1", b"av01", b"grid", b"iden", b"iovl", b"jpeg", b"tmap", b"unci", b"vvc1", b"avc1"}


class BmffError(FormatError):
    pass


def iter_boxes(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", data, pos)[0]
        typ = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise BmffError(t("bmff.damaged"))
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise BmffError(t("bmff.truncated"))
        if typ == b"uuid":
            header += 16
        yield typ, pos + header, pos + size, pos
        pos += size


def _require(start: int, end: int, size: int) -> None:
    """The box payload data[start:end] must hold `size` bytes; shorter, the file is damaged."""
    if end - start < size:
        raise BmffError(t("bmff.damaged"))


@dataclass
class HeifStructure:
    items: dict[int, bytes] = field(default_factory=dict)  # id -> type
    mime_types: dict[int, str] = field(default_factory=dict)
    refs: list[tuple[bytes, int, list[int]]] = field(default_factory=list)
    aux_types: list[str] = field(default_factory=list)
    colr_icc: list[bytes] = field(default_factory=list)
    primary: int | None = None
    top_level: list[tuple[bytes, int, int]] = field(default_factory=list)
    uuids: list[bytes] = field(default_factory=list)


def parse(data: bytes) -> HeifStructure:
    st = HeifStructure()
    for typ, ps, pe, start in iter_boxes(data, 0, len(data)):
        st.top_level.append((typ, ps, pe))
        if typ == b"uuid":
            st.uuids.append(data[start + 8:start + 24])
        elif typ == b"meta":
            _parse_meta(data, ps + 4, pe, st)
    return st


def _parse_meta(data: bytes, start: int, end: int, st: HeifStructure) -> None:
    for typ, ps, pe, _ in iter_boxes(data, start, end):
        if typ == b"pitm":
            _require(ps, pe, 1)
            version = data[ps]
            _require(ps, pe, 6 if version == 0 else 8)
            st.primary = struct.unpack_from(">H" if version == 0 else ">I", data, ps + 4)[0]
        elif typ == b"iinf":
            _require(ps, pe, 1)
            version = data[ps]
            first = ps + 4 + (2 if version == 0 else 4)
            for ityp, ips, ipe, _ in iter_boxes(data, first, pe):
                if ityp == b"infe":
                    _parse_infe(data, ips, ipe, st)
        elif typ == b"iref":
            _require(ps, pe, 1)
            version = data[ps]
            id_fmt, id_size = (">H", 2) if version == 0 else (">I", 4)
            for rtyp, rps, rpe, _ in iter_boxes(data, ps + 4, pe):
                _require(rps, rpe, id_size + 2)
                src = struct.unpack_from(id_fmt, data, rps)[0]
                count = struct.unpack_from(">H", data, rps + id_size)[0]
                targets = [
                    struct.unpack_from(id_fmt, data, rps + id_size + 2 + i * id_size)[0]
                    for i in range(count)
                    if rps + id_size + 2 + (i + 1) * id_size <= rpe
                ]
                st.refs.append((rtyp, src, targets))
        elif typ == b"iprp":
            for ptyp, pps, ppe, _ in iter_boxes(data, ps, pe):
                if ptyp == b"ipco":
                    _parse_ipco(data, pps, ppe, st)


def _parse_infe(data: bytes, start: int, end: int, st: HeifStructure) -> None:
    _require(start, end, 1)
    version = data[start]
    if version < 2:
        return
    _require(start, end, 4 + (2 if version == 2 else 4) + 2 + 4)  # version/flags, item ID, protection, type
    p = start + 4
    if version == 2:
        item_id = struct.unpack_from(">H", data, p)[0]
        p += 2
    else:
        item_id = struct.unpack_from(">I", data, p)[0]
        p += 4
    p += 2  # item_protection_index
    item_type = data[p:p + 4]
    p += 4
    st.items[item_id] = item_type
    name_end = data.find(b"\x00", p, end)
    if item_type == b"mime" and name_end >= 0:
        ct_end = data.find(b"\x00", name_end + 1, end)
        content_type = data[name_end + 1:ct_end if ct_end >= 0 else end]
        st.mime_types[item_id] = content_type.decode("latin1", "replace")


def _parse_ipco(data: bytes, start: int, end: int, st: HeifStructure) -> None:
    for typ, ps, pe, _ in iter_boxes(data, start, end):
        if typ == b"colr":
            colour_type = data[ps:ps + 4]
            if colour_type in (b"prof", b"rICC"):
                st.colr_icc.append(data[ps + 4:pe])
        elif typ == b"auxC":
            urn_end = data.find(b"\x00", ps + 4, pe)
            st.aux_types.append(data[ps + 4:urn_end if urn_end >= 0 else pe].decode("latin1", "replace"))


def _aux_label(urn: str) -> str | None:
    u = urn.lower()
    if "alpha" in u or u.endswith("auxid:1"):
        return None  # alpha channel: part of the image, not metadata
    if "gainmap" in u or "hdr" in u:
        return t("bmff.gain_map")
    if "depth" in u or "disparity" in u or u.endswith("auxid:2"):
        return t("bmff.depth")
    if "matte" in u:
        return t("bmff.matte")
    return urn


def scan(data: bytes, res: ScanResult) -> None:
    st = parse(data)
    for item_id, item_type in st.items.items():
        if item_type == b"Exif":
            res.add(Kind.EXIF, "", 0)
        elif item_type == b"mime":
            ct = st.mime_types.get(item_id, "")
            if "xml" in ct or "xmp" in ct:
                res.add(Kind.XMP, "", 0)
            else:
                res.add(Kind.OTHER, ct or "mime", 0)
        elif item_type == b"uri ":
            res.add(Kind.OTHER, "uri", 0)
        elif item_type == b"tmap":
            res.add(Kind.EXTRA_IMAGE, t("bmff.tmap"), 0)
    thumbs = sum(len(t) for r, _, t in st.refs if r == b"thmb")
    if thumbs:
        res.add(Kind.THUMBNAIL, t("bmff.thumbs", n=thumbs), 0)
    for urn in st.aux_types:
        label = _aux_label(urn)
        if label:
            res.add(Kind.EXTRA_IMAGE, label, 0)
    for icc in st.colr_icc:
        from ..colors import icc_description

        res.summary.icc_description = icc_description(icc)
        res.add(Kind.ICC, res.summary.icc_description or "", len(icc))
    # Extra images unrelated to the primary image (burst shots, previews …)
    referenced = {t for _, _, targets in st.refs for t in targets}
    referenced |= {src for r, src, _ in st.refs if r in (b"thmb", b"auxl", b"cdsc")}
    extras = [
        i for i, t in st.items.items()
        if t in IMAGE_TYPES and i != st.primary and i not in referenced and t != b"tmap"
    ]
    if extras:
        res.add(Kind.EXTRA_IMAGE, t("bmff.extras", n=len(extras)), 0)
    for uid in st.uuids:
        if uid == C2PA_UUID:
            res.add(Kind.C2PA, "", 0)
        elif uid == XMP_UUID:
            res.add(Kind.XMP, "", 0)
        else:
            res.add(Kind.OTHER, t("bmff.uuid"), 0)
    for typ, ps, pe in st.top_level:
        if typ in (b"ftyp", b"meta", b"mdat", b"uuid"):
            continue
        size = pe - ps
        if typ == b"mpvd":
            res.add(Kind.TRAILER, t("bmff.motion"), size)
        elif typ == b"sefd":
            res.add(Kind.TRAILER, t("bmff.samsung"), size)
        elif typ in (b"free", b"skip"):
            if data[ps:pe].strip(b"\x00"):
                res.add(Kind.OTHER, t("bmff.free"), size)
        elif typ == b"moov":
            res.add(Kind.EXTRA_IMAGE, t("bmff.sequence"), size)
        else:
            res.add(Kind.OTHER, typ.decode("latin1", "replace"), size)
