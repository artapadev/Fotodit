"""Helpers that build metadata-rich image files for the tests.

Imitations of the structures real devices write are assembled at the byte level:
EXIF (GPS, serial numbers, MakerNote, IFD1 thumbnail), XMP and extended
XMP, a Photoshop IRB with IPTC, ICC (sRGB / Display P3 / CMYK), extra images
appended to the end of the file with MPF, Samsung SEFT and motion photo
video trailers, PNG text chunks, C2PA-like JUMBF, etc.

The Turkish text values (a person's name, captions, comments) are non-ASCII on
purpose: they exercise Unicode handling, and other tests compare some of them.
"""

from __future__ import annotations

import io
import random
import struct
import zlib

from PIL import Image, ImageCms, ImageDraw, ImageFilter
from PIL.PngImagePlugin import PngInfo

WIN_COLOR = r"C:\Windows\System32\spool\drivers\color"

# ------------------------------------------------------------------ image content


def _noise(w: int, h: int) -> Image.Image:
    """Deterministic noise (Pillow's effect_noise differs on every call)."""
    rng = random.Random(w * 100003 + h)
    return Image.frombytes("RGB", (w, h), bytes(rng.getrandbits(8) for _ in range(w * h * 3)))


def photo(w: int = 320, h: int = 240) -> Image.Image:
    """A photo-like test image whose orientation can be recognized.

    It has marker squares: red in the top-left corner, green top-right, blue bottom-left.
    """
    base = Image.effect_mandelbrot((w, h), (-2.2, -1.2, 1.0, 1.2), 40).convert("L")
    im = Image.merge("RGB", (base, Image.linear_gradient("L").resize((w, h)), Image.radial_gradient("L").resize((w, h))))
    im = Image.blend(im, _noise(w, h), 0.2).filter(ImageFilter.GaussianBlur(0.6))
    d = ImageDraw.Draw(im)
    s = max(8, min(w, h) // 6)
    d.rectangle((0, 0, s, s), fill=(255, 0, 0))
    d.rectangle((w - s - 1, 0, w - 1, s), fill=(0, 255, 0))
    d.rectangle((0, h - s - 1, s, h - 1), fill=(0, 0, 255))
    return im


def marker_color(im: Image.Image, corner: str) -> tuple[int, int, int]:
    im = im.convert("RGB")
    w, h = im.size
    s = max(8, min(w, h) // 6) // 2
    pos = {"tl": (s, s), "tr": (w - s - 1, s), "bl": (s, h - s - 1), "br": (w - s - 1, h - s - 1)}[corner]
    return im.getpixel(pos)


def dominant(rgb) -> str:
    r, g, b = rgb[:3]
    if r > 150 and g < 100 and b < 100:
        return "red"
    if g > 150 and r < 100 and b < 100:
        return "green"
    if b > 150 and r < 100 and g < 100:
        return "blue"
    return "other"


# --------------------------------------------------------------------- EXIF generation


from fotodit.core.tiffwriter import (  # noqa: E402  (tests use these as b.Ifd / b.build_tiff)
    Ifd,
    build_tiff,
)


def rich_exif(orientation: int = 1, thumbnail: bool = True, e: str = "<") -> bytes:
    """Rich iPhone-like EXIF (raw TIFF bytes, without the Exif prefix)."""
    ifd0 = (
        Ifd(e).ascii(0x010F, "Apple").ascii(0x0110, "iPhone 15 Pro").short(0x0112, orientation)
        .ascii(0x0131, "17.4.1").ascii(0x0132, "2024:05:12 14:30:22").ascii(0x013B, "Ayşe Yılmaz")
        .ascii(0x8298, "(c) Ayşe").ascii(0x013C, "AYSE-LAPTOP")
        .byte(0x9C9D, "Ayşe".encode("utf-16-le") + b"\x00\x00")
        .pointer(0x8769, "exif").pointer(0x8825, "gps")
    )
    exif = (
        Ifd(e).ascii(0x9003, "2024:05:12 14:30:22").ascii(0x9004, "2024:05:12 14:30:22")
        .ascii(0x9291, "123").ascii(0x9011, "+03:00").ascii(0xA431, "SN-123456789")
        .ascii(0xA435, "LENS-987654").ascii(0xA430, "Ayşe Yılmaz").ascii(0xA420, "0f9e8d7c6b5a")
        .undefined(0x927C, b"Apple iOS\x00\x00\x01MM" + b"\x00" * 40 + b"BURST-UUID-1234")
        .rationals(0x829A, [(1, 120)])
    )
    gps = (
        Ifd(e).byte(0x0000, b"\x02\x02\x00\x00").ascii(0x0001, "N").rationals(0x0002, [(41, 1), (0, 1), (3012, 100)])
        .ascii(0x0003, "E").rationals(0x0004, [(28, 1), (58, 1), (4200, 100)])
    )
    ifds = [("ifd0", ifd0), ("exif", exif), ("gps", gps)]
    blobs = {}
    next_ifd = {}
    if thumbnail:
        buf = io.BytesIO()
        photo(80, 60).save(buf, "JPEG", quality=70)
        ifd1 = Ifd(e).short(0x0103, 6).pointer(0x0201, "thumb").long(0x0202, len(buf.getvalue()))
        ifds.append(("ifd1", ifd1))
        blobs["thumb"] = buf.getvalue()
        next_ifd["ifd0"] = "ifd1"
    return build_tiff(ifds, blobs, e, next_ifd)


XMP = (
    b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    b'xmlns:exif="http://ns.adobe.com/exif/1.0/" xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/" '
    b'xmlns:tiff="http://ns.adobe.com/tiff/1.0/" '
    b'exif:GPSLatitude="41,0.5N" exif:GPSLongitude="28,58.7E" exif:DateTimeOriginal="2024-05-12T14:30:22" '
    b'tiff:Orientation="1" xmpMM:DocumentID="xmp.did:1234">'
    b"<dc:creator><rdf:Seq><rdf:li>Ay\xc5\x9fe Y\xc4\xb1lmaz</rdf:li></rdf:Seq></dc:creator>"
    b"<xmpMM:History>edited in Photoshop</xmpMM:History>"
    b"</rdf:Description></rdf:RDF></x:xmpmeta>"
    b'<?xpacket end="w"?>'
)


# ---------------------------------------------------------------------- ICC generation


def _s15(v: float) -> bytes:
    return struct.pack(">i", int(round(v * 65536)))


def make_icc(desc: str, rxyz, gxyz, bxyz, gamma: float | None = None) -> bytes:
    """A simple matrix/curve-based (v2) RGB ICC profile."""
    def xyz(v):
        return b"XYZ " + b"\x00" * 4 + b"".join(_s15(c) for c in v)

    def text_desc(t):
        raw = t.encode("ascii") + b"\x00"
        return b"desc" + b"\x00" * 4 + struct.pack(">I", len(raw)) + raw + b"\x00" * 8 + b"\x00" * 3 + b"\x00" * 67

    if gamma is None:  # sRGB curve
        vals = []
        for i in range(1024):
            v = i / 1023
            lin = v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
            vals.append(int(round(lin * 65535)))
        curve = b"curv" + b"\x00" * 4 + struct.pack(">I", 1024) + b"".join(struct.pack(">H", x) for x in vals)
    else:
        curve = b"curv" + b"\x00" * 4 + struct.pack(">I", 1) + struct.pack(">H", int(round(gamma * 256)))
    tags = [
        (b"desc", text_desc(desc)),
        (b"cprt", b"text" + b"\x00" * 4 + b"Copyright Test Corp\x00"),
        (b"wtpt", xyz((0.9642, 1.0, 0.8249))),
        (b"rXYZ", xyz(rxyz)), (b"gXYZ", xyz(gxyz)), (b"bXYZ", xyz(bxyz)),
        (b"rTRC", curve), (b"gTRC", curve), (b"bTRC", curve),
    ]
    offset = 128 + 4 + 12 * len(tags)
    table, body = b"", b""
    for sig, payload in tags:
        payload += b"\x00" * (-len(payload) % 4)
        table += sig + struct.pack(">II", offset + len(body), len(payload))
        body += payload
    size = offset + len(body)
    header = (
        struct.pack(">I", size) + b"lcms" + bytes([2, 0x10, 0, 0]) + b"mntr" + b"RGB " + b"XYZ "
        + b"\x00" * 12 + b"acsp" + b"APPL" + b"\x00" * 4 + b"APPL" + b"TEST" + b"\x00" * 8 + b"\x00" * 4
        + _s15(0.9642) + _s15(1.0) + _s15(0.8249) + b"TEST" + b"\x00" * 16 + b"\x00" * 28
    )
    assert len(header) == 128
    return header + struct.pack(">I", len(tags)) + table + body


P3_ICC = make_icc(
    "Display P3", (0.515102, 0.241196, -0.001053), (0.291965, 0.692236, 0.041882), (0.157153, 0.066568, 0.784073)
)
SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def cmyk_icc() -> bytes:
    with open(WIN_COLOR + r"\RSWOP.icm", "rb") as fh:
        return fh.read()


def srgb_convert(im: Image.Image, icc: bytes) -> Image.Image:
    """Reference: profile → sRGB conversion (same settings as Fotodit)."""
    src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    return ImageCms.profileToProfile(
        im, src, ImageCms.createProfile("sRGB"), renderingIntent=ImageCms.Intent.PERCEPTUAL,
        outputMode="RGB", flags=ImageCms.Flags.BLACKPOINTCOMPENSATION,
    )


# -------------------------------------------------------------------- JPEG generation


def seg(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def irb(resources: list[tuple[int, bytes]]) -> bytes:
    out = b"Photoshop 3.0\x00"
    for rid, data in resources:
        out += b"8BIM" + struct.pack(">H", rid) + b"\x00\x00" + struct.pack(">I", len(data)) + data
        if len(data) & 1:
            out += b"\x00"
    return out


def iptc() -> bytes:
    def ds(num: int, value: str) -> bytes:
        raw = value.encode("utf-8")
        return b"\x1c\x02" + bytes([num]) + struct.pack(">H", len(raw)) + raw
    return ds(80, "Ayşe Yılmaz") + ds(120, "Tatil fotoğrafı") + ds(25, "aile") + ds(90, "İstanbul")


def insert_after_soi(jpeg: bytes, *segments: bytes) -> bytes:
    """Inserts the segments right after SOI (and after the JFIF APP0, if present)."""
    pos = 2
    if jpeg[2:4] == b"\xff\xe0":
        pos = 4 + struct.unpack(">H", jpeg[4:6])[0]
    return jpeg[:pos] + b"".join(segments) + jpeg[pos:]


def jpeg(
    im: Image.Image | None = None,
    *,
    orientation: int = 1,
    icc: bytes | None = None,
    everything: bool = True,
    **save_kwargs,
) -> bytes:
    im = im or photo()
    buf = io.BytesIO()
    params = {"quality": 88, **save_kwargs}
    if icc:
        params["icc_profile"] = icc
    im.save(buf, "JPEG", exif=b"Exif\x00\x00" + rich_exif(orientation), **params)
    data = buf.getvalue()
    if not everything:
        return data
    thumb = io.BytesIO()
    photo(64, 48).save(thumb, "JPEG", quality=60)
    extra = [
        seg(0xE0, b"JFXX\x00\x10" + thumb.getvalue()),
        seg(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + XMP),
        seg(0xE1, b"http://ns.adobe.com/xmp/extension/\x00" + b"0" * 32 + struct.pack(">II", 20, 0) + b"<rdf:RDF>ext</rdf>"),
        seg(0xE2, b"MPF\x00" + b"MM\x00*\x00\x00\x00\x08" + b"\x00" * 16),
        seg(0xE2, b"FPXR\x00\x00\x01\x02\x03"),
        seg(0xEB, b"JP\x00\x01\x00\x00\x00\x01" + b"\x00\x00\x00\x20jumbc2pa manifest data here"),
        seg(0xEC, b"Ducky\x00\x01\x00\x04\x00\x00\x00\x50"),
        seg(0xED, irb([(0x0404, iptc()), (0x040C, b"\x00" * 28 + thumb.getvalue()), (0x03ED, b"\x00" * 16)])),
        seg(0xE7, b"Qualcomm Camera Debug\x00" + b"\x01" * 50),
        seg(0xFE, "Çekim: Ayşe'nin telefonu".encode()),
    ]
    data = insert_after_soi(data, *extra)
    secondary = io.BytesIO()
    photo(160, 120).save(secondary, "JPEG", quality=50)
    trailer = secondary.getvalue() + b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64 + b"SEFH" + b"\x00" * 20 + b"SEFT"
    return data + trailer


def jpeg_with_jfif_thumbnail() -> bytes:
    buf = io.BytesIO()
    photo().save(buf, "JPEG", quality=85)
    data = buf.getvalue()
    assert data[2:4] == b"\xff\xe0"
    length = struct.unpack(">H", data[4:6])[0]
    body = bytearray(data[6:4 + length])
    body[12], body[13] = 2, 2
    body += bytes(range(12))
    return data[:4] + struct.pack(">H", len(body) + 2) + bytes(body) + data[4 + length:]


# --------------------------------------------------------------------- PNG generation


def png_chunk(ctype: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)


def insert_png_chunks(data: bytes, *chunks: bytes, before: bytes = b"IDAT") -> bytes:
    idx = data.find(before) - 4
    return data[:idx] + b"".join(chunks) + data[idx:]


def png(im: Image.Image | None = None, *, icc: bytes | None = None, orientation: int = 1, everything: bool = True) -> bytes:
    im = im or photo()
    info = PngInfo()
    info.add_text("Author", "Ayşe Yılmaz")
    info.add_text("Description", "gizli açıklama", zip=True)
    info.add_itxt("XML:com.adobe.xmp", XMP.decode("utf-8"))
    info.add_text("parameters", "a photo of a cat, Steps: 20, Sampler: Euler, Seed: 42")
    buf = io.BytesIO()
    params = {"pnginfo": info, "exif": b"Exif\x00\x00" + rich_exif(orientation, thumbnail=False)}
    if icc:
        params["icc_profile"] = icc
    im.save(buf, "PNG", **params)
    data = buf.getvalue()
    if everything:
        data = insert_png_chunks(
            data,
            png_chunk(b"tIME", struct.pack(">HBBBBB", 2024, 5, 12, 14, 30, 22)),
            png_chunk(b"pHYs", struct.pack(">IIB", 5669, 5669, 1)),
            png_chunk(b"caBX", b"jumb c2pa"),
            png_chunk(b"iDOT", b"\x00" * 28),
            png_chunk(b"prVW", b"private preview"),
        )
        data += b"trailing-garbage-after-IEND"
    return data


# ---------------------------------------------------------------- other formats


def webp(im: Image.Image | None = None, *, lossless: bool = False, icc: bytes | None = None) -> bytes:
    im = im or photo()
    buf = io.BytesIO()
    params = {"exif": b"Exif\x00\x00" + rich_exif(1, thumbnail=False), "xmp": XMP, "lossless": lossless, "quality": 90}
    if icc:
        params["icc_profile"] = icc
    im.save(buf, "WEBP", **params)
    data = buf.getvalue()
    # Add an unknown chunk, plus junk after the RIFF data
    extra = b"ABCD" + struct.pack("<I", 6) + b"secret"
    body = data[12:] + extra
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body + b"TRAILER"


def animated_webp() -> bytes:
    frames = [photo(160, 120).rotate(a) for a in (0, 90, 180)]
    buf = io.BytesIO()
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=0,
                   exif=b"Exif\x00\x00" + rich_exif(1, thumbnail=False), xmp=XMP, lossless=True)
    return buf.getvalue()


def gif(animated: bool = True) -> bytes:
    frames = [photo(120, 90).quantize(64)] + ([photo(120, 90).rotate(90).quantize(64)] if animated else [])
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=animated, append_images=frames[1:], duration=150, loop=0,
                   comment="Ayşe tarafından çekildi".encode())
    data = buf.getvalue()
    # Raw XMP block with the 258-byte "magic trailer" from the XMP specification
    xmp_ext = b"\x21\xff\x0bXMP DataXMP" + XMP + b"\x01" + bytes(range(0xFF, -1, -1)) + b"\x00"
    ext_8bim = b"\x21\xff\x0bMGK8BIM0000" + b"\x04abcd" + b"\x00"
    packed = data[10]
    pos = 13 + (3 * (2 << (packed & 7)) if packed & 0x80 else 0)
    return data[:pos] + xmp_ext + ext_8bim + data[pos:] + b"junk-after-trailer"


def tiff(pages: int = 2, icc: bytes | None = None, compression: str = "tiff_lzw") -> bytes:
    from PIL.TiffImagePlugin import ImageFileDirectory_v2

    frames = [photo().rotate(90 * i, expand=True) for i in range(pages)]
    ifd = ImageFileDirectory_v2()
    ifd[270] = "Gizli açıklama"
    ifd[271] = "Canon"
    ifd[272] = "Canon EOS R5"
    ifd[305] = "Adobe Photoshop 25.0"
    ifd[306] = "2024:05:12 14:30:22"
    ifd[315] = "Ayşe Yılmaz"
    ifd[33432] = "(c) Ayşe"
    ifd[700] = XMP
    ifd.tagtype[700] = 1
    ifd[33723] = iptc()
    ifd.tagtype[33723] = 7
    buf = io.BytesIO()
    params = {"tiffinfo": ifd, "compression": compression, "save_all": pages > 1, "append_images": frames[1:]}
    if icc:
        params["icc_profile"] = icc
    frames[0].save(buf, "TIFF", **params)
    return buf.getvalue()


def heic(im: Image.Image | None = None, *, orientation: int = 1, icc: bytes | None = None, thumbnails=(96,)) -> bytes:
    from fotodit.core.imaging import ensure_plugins

    ensure_plugins()
    im = im or photo()
    buf = io.BytesIO()
    params = {"exif": b"Exif\x00\x00" + rich_exif(orientation, thumbnail=False), "xmp": XMP, "quality": 90,
              "thumbnails": list(thumbnails)}
    if icc:
        params["icc_profile"] = icc
    im.save(buf, "HEIF", **params)
    return buf.getvalue()


def avif(im: Image.Image | None = None, *, orientation: int = 1, icc: bytes | None = None) -> bytes:
    im = im or photo()
    buf = io.BytesIO()
    params = {"exif": b"Exif\x00\x00" + rich_exif(orientation, thumbnail=False), "xmp": XMP, "quality": 90}
    if icc:
        params["icc_profile"] = icc
    im.save(buf, "AVIF", **params)
    return buf.getvalue()


def bmp(im: Image.Image | None = None) -> bytes:
    buf = io.BytesIO()
    (im or photo()).save(buf, "BMP")
    return buf.getvalue()


def bmp_v5_with_profile(icc: bytes, linked: bool = False) -> bytes:
    """24-bit BMP V5 with an embedded (MBED) or linked (LINK) color profile."""
    im = photo(16, 8)
    w, h = im.size
    row = (w * 3 + 3) & ~3
    pixels = bytearray()
    for y in range(h - 1, -1, -1):
        line = bytearray()
        for x in range(w):
            r, g, b = im.getpixel((x, y))
            line += bytes((b, g, r))
        pixels += line.ljust(row, b"\x00")
    profile = (b"C:\\Users\\ayse\\profiles\\custom.icc\x00" if linked else icc)
    header_size = 124
    pixel_offset = 14 + header_size
    profile_offset = header_size + len(pixels)  # relative to the start of the DIB header
    cstype = 0x4C494E4B if linked else 0x4D424544
    dib = struct.pack(
        "<IiiHHIIiiII", header_size, w, h, 1, 24, 0, len(pixels), 2835, 2835, 0, 0
    ) + struct.pack("<IIII", 0, 0, 0, 0) + struct.pack("<I", cstype) + b"\x00" * 36 + struct.pack("<III", 0, 0, 0)
    dib += struct.pack("<IIII", 4, profile_offset, len(profile), 0)
    assert len(dib) == header_size, len(dib)
    body = dib + bytes(pixels) + profile
    return b"BM" + struct.pack("<IHHI", 14 + len(body), 0, 0, pixel_offset) + body
