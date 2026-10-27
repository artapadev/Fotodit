"""Shared data model for metadata scan results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ...i18n import t


class FormatError(ValueError):
    """The file structure is broken/incomplete; the message, in the app's language, is shown to the user."""


class Kind(str, Enum):
    EXIF = "exif"
    GPS = "gps"
    MAKERNOTE = "makernote"
    XMP = "xmp"
    IPTC = "iptc"
    PHOTOSHOP = "photoshop"
    ICC = "icc"
    THUMBNAIL = "thumbnail"
    EXTRA_IMAGE = "extra_image"
    COMMENT = "comment"
    TEXT = "text"
    TIMESTAMP = "timestamp"
    C2PA = "c2pa"
    TRAILER = "trailer"
    OTHER = "other"
    # Videos
    TAGS = "tags"
    TRACK = "track"
    EMBEDDED = "embedded"
    SUBTITLE = "subtitle"
    CHAPTERS = "chapters"


LABELS: dict[Kind, str] = {  # the keys of their names
    Kind.EXIF: "meta.kind.exif",
    Kind.GPS: "meta.kind.gps",
    Kind.MAKERNOTE: "meta.kind.makernote",
    Kind.XMP: "meta.kind.xmp",
    Kind.IPTC: "meta.kind.iptc",
    Kind.PHOTOSHOP: "meta.kind.photoshop",
    Kind.ICC: "meta.kind.icc",
    Kind.THUMBNAIL: "meta.kind.thumbnail",
    Kind.EXTRA_IMAGE: "meta.kind.extra_image",
    Kind.COMMENT: "meta.kind.comment",
    Kind.TEXT: "meta.kind.text",
    Kind.TIMESTAMP: "meta.kind.timestamp",
    Kind.C2PA: "meta.kind.c2pa",
    Kind.TRAILER: "meta.kind.trailer",
    Kind.OTHER: "meta.kind.other",
    Kind.TAGS: "meta.kind.tags",
    Kind.TRACK: "meta.kind.track",
    Kind.EMBEDDED: "meta.kind.embedded",
    Kind.SUBTITLE: "meta.kind.subtitle",
    Kind.CHAPTERS: "meta.kind.chapters",
}

# Kinds that are especially sensitive as personal data (shown with a warning in the UI).
SENSITIVE = {Kind.GPS, Kind.MAKERNOTE, Kind.IPTC, Kind.C2PA, Kind.TRAILER}


@dataclass
class Finding:
    kind: Kind
    detail: str = ""
    size: int = 0

    @property
    def label(self) -> str:
        return t(LABELS[self.kind])

    @property
    def sensitive(self) -> bool:
        return self.kind in SENSITIVE


@dataclass
class ImageSummary:
    """Readable summary information shown to the user."""

    camera: str | None = None
    software: str | None = None
    taken: datetime | None = None
    gps: tuple[float, float] | None = None
    orientation: int | None = None
    icc_description: str | None = None


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    summary: ImageSummary = field(default_factory=ImageSummary)

    def add(self, kind: Kind, detail: str = "", size: int = 0) -> None:
        self.findings.append(Finding(kind, detail, size))

    @property
    def clean(self) -> bool:
        return not self.findings

    def kinds(self) -> set[Kind]:
        return {f.kind for f in self.findings}

    def has_gps(self) -> bool:
        return Kind.GPS in self.kinds()

    def describe(self) -> str:
        """One-line summary (used in verification errors)."""
        return ", ".join(
            f.label + (f" [{f.detail}]" if f.detail else "") for f in self.findings
        )
