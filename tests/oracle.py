"""Independent verifier: exiv2 (a C++ metadata library, via pyexiv2).

A tool fully independent of Fotodit's own parsers cross-checks that no
EXIF/IPTC/XMP/ICC/comment is left in the output. Used only by the tests;
not bundled with the app.
"""

from __future__ import annotations

import math

from PIL import ImageChops, ImageStat


def exiv2_metadata(data: bytes) -> dict:
    import pyexiv2

    out = {}
    with pyexiv2.ImageData(data) as img:
        for name, reader in (
            ("exif", img.read_exif),
            ("iptc", img.read_iptc),
            ("xmp", img.read_xmp),
            ("icc", img.read_icc),
            ("comment", img.read_comment),
        ):
            try:
                out[name] = reader()
            except Exception:  # the format does not support this field
                out[name] = None
    return out


# For TIFF, exiv2 also lists the image's structural tags as "Exif.Image.*"
# ("Exif.Thumbnail.*" for page 2); they are not metadata but required fields
# that describe the image itself.
_TIFF_STRUCTURE = {
    "NewSubfileType", "ImageWidth", "ImageLength", "BitsPerSample", "Compression",
    "PhotometricInterpretation", "StripOffsets", "SamplesPerPixel", "RowsPerStrip",
    "StripByteCounts", "PlanarConfiguration", "XResolution", "YResolution", "ResolutionUnit",
    "Predictor", "ExtraSamples", "SampleFormat", "ColorMap", "TileWidth", "TileLength",
    "TileOffsets", "TileByteCounts", "FillOrder", "YCbCrSubSampling", "InkSet",
}


def assert_exiv2_clean(data: bytes, allow_icc: bool = False) -> None:
    meta = exiv2_metadata(data)
    if allow_icc:  # a clean standard profile kept on purpose (a TIFF's is listed among its tags)
        meta.pop("icc", None)
        if meta.get("exif"):
            meta["exif"] = {k: v for k, v in meta["exif"].items() if k != "Exif.Image.InterColorProfile"}
    if data[:4] in (b"II*\x00", b"MM\x00*") and meta.get("exif"):
        meta["exif"] = {
            k: v for k, v in meta["exif"].items()
            if not (k.startswith(("Exif.Image.", "Exif.Thumbnail.")) and k.split(".")[-1] in _TIFF_STRUCTURE)
        }
    leftovers = {k: v for k, v in meta.items() if v}
    assert not leftovers, f"exiv2 found metadata: {leftovers}"


def psnr(a, b) -> float:
    a, b = a.convert("RGB"), b.convert("RGB")
    assert a.size == b.size, (a.size, b.size)
    diff = ImageChops.difference(a, b)
    mse = sum(ImageStat.Stat(diff.point(lambda v: v * v)).mean) / 3
    return float("inf") if mse == 0 else 10 * math.log10(255 * 255 / mse)
