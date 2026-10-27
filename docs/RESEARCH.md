# Fotodit — Research and Design Report

*Date: 28 September 2026, updated 25 October 2026 · Version 1.3.0*

This report summarizes the research done for Fotodit's three main goals and the decisions that were made:

1. Finding and removing **all** metadata in photo files,
2. Converting photos to JPG **without errors**,
3. Sorting and naming photos and grouping them into folders (categorizing them).

Added later: RAW support (§4b), folder selection for large archives (§6b), the large preview (§6c),
rotation/cropping (§6d) and **videos** (§6e: metadata removal, conversion to MP4, separate naming, playback).
Version 1.1 added the exposure of RAW photos (§4c), trimming videos without re-encoding (§6e, "Trimming") and a
light/dark theme (§6f). Version 1.2 added the exposure of other photos (§4c), keeping wide color spaces (§4d),
removing parts of a video (§6e, "Removing parts") and the open-source release (§11). Version 1.3 moved the
interface to Apple's Liquid Glass design as iOS 27 draws it, in black, white and grey with the Inter typeface
(§6f), and added keeping the capture date, date-based names, a warning
for file names that give something away, shrinking photos, finding duplicates, removing a video's sound, the
horizontal flip and a before/after view (§6g).

Most decisions are based not on assumptions but on **measurements**: the actual behavior of the libraries was
verified from their source code and through experiments; the results are given in this document with their figures.

Fotodit's user interface is in Turkish and English (§6f); UI labels (and output names such as folder names) are
quoted as the Turkish interface shows them, with an English gloss in parentheses the first time they appear in a
section.

---

## 1. Where is metadata stored in photos?

### 1.1 Types of metadata

| Type | Where it is stored | Potentially personal/sensitive content |
|---|---|---|
| **EXIF** | JPEG APP1 `Exif\0\0`, PNG `eXIf`, WebP `EXIF`, HEIC `Exif` item, TIFF tags | Camera make/model, capture date/time and **time zone** (OffsetTime), software, artist, copyright, **computer name**, camera owner, **body and lens serial numbers**, unique image ID, user comment (Stable Diffusion writes its prompts here), title/author/tag fields written by Windows Explorer (XP*) |
| **GPS** | GPS directory inside EXIF, XMP `exif:GPS*` | Latitude, longitude, altitude, **the direction the camera was facing**, speed, UTC time |
| **MakerNote** | Inside EXIF, manufacturer-specific | Serial numbers; on Apple devices the ID that links a Live Photo to its video, the burst ID, the time since boot (links photos taken in the same session), accelerometer data, front/rear camera |
| **Embedded thumbnails** | EXIF IFD1, JFIF/JFXX, Photoshop IRB, HEIC `thmb`, MakerNote previews | **The original image before cropping/retouching** (editors often do not update the thumbnail) |
| **XMP** | JPEG APP1 (+ extended XMP), PNG `iTXt`, WebP `XMP `, HEIC `mime` item, TIFF 700, GIF application extension | Edit history (which software, when), document IDs, **face regions and people's names**, keywords, Lightroom settings, crop information, in Google portrait photos the **depth map and the unblurred original image** (base64), drone flight data, AI source type |
| **IPTC-IIM / Photoshop IRB** | JPEG APP13 `Photoshop 3.0` (8BIM resources), TIFF 33723/34377 | Author, description, keywords, city/country, contact details, Photoshop thumbnail |
| **ICC color profile** | JPEG APP2, PNG `iCCP`, WebP `ICCP`, HEIC `colr`, TIFF 34675, BMP V5 | Profile description, manufacturer/model, copyright, creation date. It also determines the image's colors (see §3.3) |
| **Additional images** | JPEG MPF + end of file, HEIC auxiliary images (`auxl`), `tmap` | HDR gain map, depth map, portrait masks, large previews |
| **Trailer** | Data appended after EOI in JPEG or after IEND in PNG | **Motion photo video** (Google/Samsung), Samsung SEFT data (millisecond timestamps, **mobile country code**, a hash of the original file path, editing data) |
| **C2PA (Content Credentials)** | JPEG APP11 (JUMBF), PNG `caBX`, WebP `C2PA`, HEIC `uuid` box, GIF application extension | Signing device/app, action history and timestamps, pre-edit thumbnails, sometimes GPS. On some devices a device-specific certificate = **device identifier** |
| **Text chunks** | PNG `tEXt/zTXt/iTXt`, JPEG `COM`, GIF comment and plain-text extensions | Author, description, software, **AI generation prompts** (A1111 `parameters`, ComfyUI `prompt/workflow`) |
| **Timestamp** | PNG `tIME` | Last modification time |

### 1.2 Easily overlooked places

* **The old version of a cropped screenshot:** Some tools wrote the cropped image over the old file without
  truncating the file; the old pixels remained at the end of the file ("aCropalypse":
  CVE-2023-21036 — Pixel Markup, CVE-2023-28303 — Windows Snipping Tool).
  Fotodit checks both for trailers and for more data **inside** the compressed PNG stream than the image
  requires.
* **Embedded thumbnails** can show the uncropped version of the photo.
* **Windows Explorer** writes the title/tags/author into EXIF (XPTitle, XPAuthor …) when they are edited on
  a JPEG's "Details" tab.
* **File name:** names such as `IMG_20240512_143022`, `PXL_…`, `WhatsApp Image 2024-05-12 at 14.30.22` reveal the
  date and time. Fotodit's renaming feature takes care of this too.
* **File system:** creation/modification dates and the NTFS `Zone.Identifier` stream
  (for files downloaded from the internet, **the address of the page they were downloaded from**). When removing
  metadata, Fotodit writes every output as **a new file**; the old dates and this stream are not carried over.

### 1.3 Traces that cannot be removed from the file (known limits)

These are not metadata but part of the image itself; no "metadata remover" can remove them:

* **JPEG compression tables** can give away the camera/software (Kee, Johnson and Farid 2011: in 62% of the
  images, a signature unique to a single camera model). Lossless cleaning keeps these tables.
* **Sensor noise (PRNU)**: can match photos taken with the same camera.
* **Invisible watermarks**: SynthID (Google), Adobe TrustMark, Digimarc, Stable Diffusion watermarks.
* **The colors underneath transparent PNG pixels** and the data NovelAI hides in the alpha channel
  (converting to JPG destroys these, because transparency is composited onto a white background).

---

## 2. Comparison with `exiftool -all=`

The `-all=` behavior of ExifTool, regarded as the reference tool for metadata removal, was examined:
EXIF, GPS, MakerNote, XMP, IPTC, ICC, JFIF, MPF, all APPn segments and trailers are removed;
the **APP14 Adobe** segment is kept because it affects the colors. Two important differences:

* ExifTool **does not apply the orientation (Orientation) to the pixels**; once EXIF is removed, photos stored
  sideways are displayed sideways.
* When the ICC profile is removed, the colors of wide-gamut photos (such as iPhone Display P3) look washed out;
  ExifTool recommends a separate command for this.

Fotodit handles both automatically (see §3).

---

## 3. How does Fotodit remove metadata?

### 3.1 Principle: allowlist

A "remove known metadata" approach misses new/manufacturer-specific fields. Fotodit does the opposite:
**everything except the structures needed to decode the image and display it correctly is removed.**

| Format | Kept (the image itself) | Removed |
|---|---|---|
| JPEG | SOF, DHT, DQT, DRI, SOS + compressed data; JFIF header (with its thumbnail dropped); APP14 Adobe (needed for CMYK color conversion) | APP1–APP13, APP15, COM, everything after EOI |
| PNG | IHDR, PLTE, tRNS, IDAT, IEND; APNG animation; color interpretation (gAMA, cHRM, sRGB, sBIT, cICP, mDCV, cLLI) | All text chunks, eXIf, iCCP, tIME, pHYs, caBX, iDOT, unrecognized chunks, everything after IEND |
| WebP | VP8/VP8L, ALPH, ANIM/ANMF, VP8X (with its flags corrected) | ICCP, EXIF, XMP, C2PA, unrecognized chunks, everything after the RIFF data |
| GIF | Header, color tables, image blocks, frame duration/transparency, looping | Comments, plain text, other application extensions (XMP, ICC, IPTC, C2PA), trailing data |
| BMP | Header and pixels | Linked/embedded profile, trailer |
| TIFF | Structural tags and pages | All other tags (by rewriting) |
| HEIC/AVIF | Primary image | EXIF, XMP, thumbnails, depth/HDR/mask images, C2PA, motion photo video (by re-encoding) |
| RAW | — (cannot be cleaned in place) | Everything: by converting to JPG (see §4b) |

### 3.2 Lossless cleaning

In JPEG, PNG, WebP, GIF and BMP, metadata is removed **at the byte level**; because the compressed image data is
copied unchanged, **there is zero quality loss**, and the result is verified to be pixel-for-pixel identical.
This is why JPGs are not "re-saved".

### 3.3 Three cases that require re-encoding

1. **Orientation ≠ 1.** Phones store a portrait photo sideways and write "rotate 90°" into EXIF.
   Once EXIF is removed, the photo is displayed sideways. Fotodit applies the rotation to the pixels.
   To preserve quality, the source's **own compression tables** (transposed to match the rotation) are
   reused. Measurement (1600×1200 source, quality 90):

   | Method | Difference from the source (PSNR) | File size |
   |---|---|---|
   | Fotodit: same tables, transposed | **52.9 dB** (visually indistinguishable) | 0.1% difference |
   | Same tables, not transposed | 49.2 dB | 0.5% difference |
   | Re-saving at "quality 95" | 49.4 dB | **36% larger** |

   JPEGs with 4:2:2 subsampling are saved as 4:4:4 when rotated by 90°, so that no color resolution is lost.

2. **If the color profile is not sRGB.** Images without a profile are treated as sRGB everywhere. If the profile
   is not sRGB (Display P3, Adobe RGB, CMYK), the pixels are first converted to sRGB with LittleCMS.
   Whether a profile "is sRGB" is decided not by looking at its name but **by measurement**: a 729-color grid is
   converted through the profile; the largest difference found was ≤1 for sRGB variants, 96 for Display P3 and
   128 for Adobe RGB. Two cases that do not embed a profile are handled too:
   * **nclx** color codes in HEIC (e.g. Display P3 = 12, BT.2020 = 9): an equivalent ICC profile is computed.
   * The **"R03"** marker that cameras shooting in Adobe RGB put only into EXIF.
   CMYK example: (0,255,255,0) becomes (255,0,0) with the naive formula and (229,51,44) with the actual SWOP profile.

3. **HEIC, AVIF and TIFF.** In these formats metadata is stored as separate items/tags; instead of extracting it
   safely and losslessly, the image is cleanly re-encoded (HEIC/AVIF at 90% quality).
   If quality loss is not acceptable, **converting to JPG** is recommended; the UI shows this as a warning.

### 3.4 Metadata that Pillow carries over by itself (verified)

The assumption that "re-saving cleans it" is wrong. Verified in the Pillow 12.3 / pillow-heif 1.8 source code
and by experiment:

| Saving as | Carried over from the source automatically |
|---|---|
| JPEG | comment (COM) |
| PNG, AVIF | ICC profile |
| TIFF | ICC, XMP, IPTC, Photoshop tags |
| HEIC (pillow-heif) | EXIF, XMP, ICC, thumbnail setting |

For every re-encode, Fotodit uses a **clean image object** with an empty info dictionary and no plugin
attributes.

### 3.5 Three-layer verification

Before any output is written to disk:

1. **It is re-scanned:** the format-specific parser must find no metadata.
2. **Signature scan:** all bytes of the file are searched for long signatures such as `Exif\0\0II*`, `<x:xmpmeta`,
   `ICC_PROFILE`, `Photoshop 3.0`, `GPSLatitude` … (a second safety net against anything the parsers might
   miss).
3. **It is decoded from start to finish** and its dimensions are checked; on lossless paths the pixels are
   compared **one-to-one** with the source.

If any of these fails, the file is not touched at all. In addition, the tests use a C++ library that is completely
independent of Fotodit (**exiv2**) to cross-check that no EXIF/IPTC/XMP/ICC/comments remain in the outputs.

---

## 4. Error-free conversion to JPG

| Pitfall | What happens | Fotodit's solution |
|---|---|---|
| EXIF orientation | The photo comes out sideways/upside down | The orientation is applied to the pixels (all values 1–8 tested) |
| Double rotation in HEIC | libheif has already rotated the image; rotating it again according to EXIF would be wrong | The orientation reset by the pillow-heif plugin is trusted; for AVIF the rotation is applied from EXIF (both tested) |
| Transparency | When alpha is dropped, transparent areas come out **black** or in random colors | Composited onto a white background |
| Palette / 1-bit | Cannot be saved | Converted to a suitable mode |
| 16-bit grayscale | Pillow clips at 255 → the image turns **completely white** | Scaled correctly (/257) |
| CMYK | The naive formula distorts the colors | Converted to sRGB with the embedded CMYK profile |
| Wide color gamut | Colors look washed out | Converted to sRGB (perceptual + black point compensation); when converting with metadata kept, the profile is carried over unchanged |
| HDR (gain map) | The additional images do not fit into a JPEG | The SDR base image is correct and complete; a warning is shown for PQ/HLG (true HDR) images |
| Animation / multiple pages | — | The first frame (the primary image for HEIC) is used and a note is added to the report |
| Size limit | JPEGs larger than 65,500 pixels cannot be written | A clear error |
| Chroma subsampling | 4:2:0 blurs colored text in screenshots | PNG/GIF/BMP/TIFF/lossless WebP → 4:4:4; HEIC/AVIF/lossy WebP → 4:2:0, since they are 4:2:0 already |
| Corrupt / truncated file | A half-gray image | The error is reported, the file is not touched |
| Misleading extension | A PNG/WebP named ".jpg" | The format is determined from the first bytes |
| Security | For formats such as EPS, Pillow may run an external program | Only 9 decoders are allowed |
| Huge images | Running out of memory | 400 MP limit, clear error |

**Quality:** Default 95% (the Pillow documentation advises against going above 95); 90%, 85% and 75% are also
available. Baseline JPEG is written — the widest compatibility; the Huffman tables are optimized (a lossless size
reduction).

**Converting with metadata removal turned off:** the EXIF bytes are carried over unchanged (they are not rewritten,
so that the MakerNote offsets do not break); only the orientation value is set to 1 in place; ICC and XMP are carried
over too; file dates are preserved.

---

## 4b. RAW files (DNG, CR2, CR3, NEF, ARW, RAF, ORF, RW2, PEF …)

**What is inside?** Besides the sensor's unprocessed data, RAW files carry the richest metadata: all EXIF fields,
GPS, body/lens serial numbers, manufacturer-specific notes (MakerNote), usually one or more **embedded JPEG
previews** (the version you see on the camera's screen), XMP in DNG, the original file name, and in some DNGs even
**the entire original RAW file** (OriginalRawFileData). Fotodit shows all of this in the info panel.

**Why can't they be cleaned in place?** The information needed to decode a RAW file (color matrices, black/white
levels, sensor pattern, lens/white balance data) is itself part of the metadata; removing it makes the file
unreadable, while keeping it leaves the cleaning incomplete. ExifTool also recommends not cleaning RAW files in
place. Fotodit therefore cleans RAW files **by converting them to JPG**; if only "Tüm metadata'yı sil" (remove all
metadata) is selected, RAW files are skipped with a clear warning (renaming/grouping into folders can still be done
without touching the RAW file).

**Detection.** Most RAW formats are TIFF in structure; Pillow takes them for TIFF and opens only the small preview
inside them (Pillow #6482) — this would lead to a silent failure such as a "160×120 JPG". Fotodit recognizes RAW
files by their content: ORF/RW2/RAF/CR3/CRW/X3F/MRW signatures, the CR2 marker, the DNG version tag, or CFA (Bayer)
sensor data in any IFD; the file extension is used as an additional hint. RAW files are never opened with Pillow.

**Two conversion methods** (shown in the UI only if the selection contains RAW files):

| Method | What it does | When |
|---|---|---|
| **"Kamera görünümü"** (camera view; default) | The **full-size JPEG** inside the RAW file, processed by the camera itself, is used: colors, picture style and lens corrections are as you saw them on the camera; metadata is removed at the byte level, **without recompression**. | If the preview is at least 90% of the RAW's size (mostly the case with Canon, Nikon, Pentax). Otherwise Fotodit automatically switches to the method below and says so in the report. |
| **"RAW verisinden işle"** (process from RAW data) | Processed at full resolution with LibRaw: the camera's as-shot white balance, AHD demosaicing, sRGB output, **sRGB tone curve** (LibRaw's default is the BT.709 curve), "clip" mode so that clipped highlights do not shift in color; 4:4:4 JPEG. | For those who want the same, consistent processing every time. The result may look "flatter" than the camera JPEG (camera styles and lens corrections are not applied). |

**Orientation.** LibRaw applies the RAW's orientation itself. On the camera-JPEG path the preview is stored
unrotated, so the RAW's orientation (LibRaw's "flip" value → its EXIF equivalent) is applied to the pixels; if the
preview carries its own EXIF orientation (Fuji RAF), that is trusted. All 8 orientation values were tested on both
paths. (These tests also caught a bug in which an `if method:` check skipped mirroring because the numeric value of
Pillow's "mirror" constant (`FLIP_LEFT_RIGHT`) is 0; it has been fixed.)

**Color.** When processing from RAW data, the output is sRGB directly. If the camera is in Adobe RGB mode (EXIF color
space or "R03"), the embedded JPEG is Adobe RGB too; it is converted to sRGB.

**Memory.** Processing a 45 MP RAW file uses hundreds of MB; at most two RAW files are processed at the same time.

**Verification.** Without downloading real camera files, both paths, all 8 orientations and conversion with metadata
kept were tested using synthetic DNGs that LibRaw processes like real RAW files (12-bit Bayer mosaic, DNG color tags,
EXIF/GPS/XMP, embedded preview); the metadata boxes of CR3 and RAF were also tested structurally. Make/model
compatibility is determined by LibRaw 0.22 (including cameras released up to 2025).

## 4c. Exposure (RAW and other photos)

Under a RAW photo the large preview shows a **"Pozlama"** (exposure) slider: −3 … +3 EV in steps of 0.1. It is an
edit like rotation (kept until processing starts, see §6d) and is applied when the RAW is converted to JPG.

**Why from the RAW data.** The camera's embedded JPEG is 8-bit and already tone-mapped: its clipped highlights cannot
be won back by darkening, and brightening it lifts its noise and banding. A RAW with an exposure set is therefore
always developed from its RAW data (also with "Kamera görünümü"), with the settings of "RAW verisinden işle" (the
camera's white balance, sRGB, clipped highlights kept neutral).

**How.** LibRaw develops the RAW to **linear 16-bit** values (`gamma=(1,1)`, `no_auto_bright`). A lookup table
(65,536 entries → 8 bits) then combines:

1. LibRaw's automatic brightening, reproduced: as in LibRaw, each channel's histogram (in steps of 8) is walked down
   from the top until more than 1% of the pixels lie above; the brightest channel's value becomes white;
2. the exposure, ×2^EV;
3. when brightening, a **soft shoulder**: above a knee (75–100% of white, depending on the amount) the highlights
   approach white along an exponential curve instead of clipping;
4. the sRGB curve with LibRaw's own 8-bit rounding.

At 0 EV the result is LibRaw's own development (on the test DNG: largest difference 1 level, mean 0.001), so there is
no jump between "no exposure" (LibRaw's path) and a small adjustment. Darkening brings back the brightest 1% that the
automatic brightening had clipped; areas clipped on the sensor turn gray rather than white (there is no highlight
reconstruction).

**Live preview.** The preview develops the RAW once, at half size, in the background, and reduces it to screen size
by averaging in linear light; each slider position is then one table lookup. Thumbnails of photos with an exposure
are made the same way, so preview, thumbnails and result agree. While the slider is dragged only the preview
changes; the edit is stored when it is released (one step for "geri al": undo).

**Measured (24 MP synthetic DNG, this computer):** LibRaw's 8-bit development 1.45 s; with an exposure 2.34 s
(linear development 1.56 s, white point 0.45 s, lookup 0.33 s); the half-size preview development 1.05 s, then
27 ms per slider step (1500×1000). The linear development of a 45 MP RAW takes ~270 MB; the lookup runs in strips of
~4 M values, within the two-RAWs-at-a-time limit.

**Other photos (1.2).** JPG, HEIC, PNG, WebP, TIFF, GIF and BMP get the same slider. Their pixels are already
tone-mapped 8-bit (or 16-bit) values, so the exposure is a lookup table on them: each value is made linear with the
sRGB curve, multiplied by 2^EV (with the same soft shoulder when brightening) and encoded again (`edits.exposure_curve`,
256 entries, 65,536 for 16-bit TIFF/PNG). Transparency is kept, a palette image (GIF) gets a new palette, CMYK is
adjusted through its inverted values. Unlike a RAW, detail that is pure white or black in the file cannot come back;
the slider's note says so. The preview applies the same table to the screen image (live, no development), the
thumbnails too. A photo with an exposure is re-encoded: JPEGs with their own quantization tables, like a rotation
(§6d); lossless formats stay lossless.

## 4d. Keeping wide color spaces

By default a photo with a color profile other than sRGB is converted to sRGB and the profile is removed (§3.3): a
profile is metadata too, and it can name the device or the software. The option **"Geniş renk alanını koru"** (keep
the wide color space) keeps the wide colors of iPhone (Display P3) and professional cameras (Adobe RGB, ProPhoto RGB)
instead: the pixels are not converted, and the source profile is replaced with a **clean standard profile** that
Fotodit generates itself (`iccgen`: the color space's primaries, white point and curve; no device or vendor text).

**Which profiles count as standard.** A profile is accepted only if Little CMS shows that it describes the same color
space: a grid of 729 colors (9 steps per channel) is converted from the source profile to each standard profile,
and the result must be identical within 2 levels at most and 0.25 on average. Any profile with the standard
primaries, white point and curve passes, whoever wrote it (tested: a Display P3 profile with Apple's colorants, the
nclx description in HEIC, Adobe RGB, ProPhoto RGB); a profile that is not one of these (a calibrated monitor profile,
a linear P3 …) is still converted to sRGB, and the result note says why. The verification accepts exactly
the clean profile that was embedded and nothing else (`ContentResult.profile`).

**Per format.** A JPEG keeps its compressed data untouched (only the profile segment is swapped: lossless); PNG,
WebP, HEIC/AVIF and TIFF (when every page has the same standard profile) are written with the clean profile;
converting to JPG embeds it in the JPEG. Tested on every format with a P3 source: the output's colors equal the
source's, not their sRGB conversion.

---

## 5. Safety in file operations

* **Prepare first, then apply:** all outputs are first written to temporary files and verified; the originals are
  touched only once all of them are ready. If the job is cancelled, no file changes.
* **Never overwrite:** if a file with the same name exists at the destination, " (2)" is appended to the name.
* **Reversibility:** in "Orijinal dosyalara uygula" (apply to the original files) mode the originals are moved to the
  Recycle Bin. When moving files one at a time, Qt's `QFile.moveToTrash` function is used: if a file cannot go to the
  Recycle Bin (network/USB drive), it **fails instead of deleting permanently**, and the original is kept. Because
  `QFile.moveToTrash` takes ~45 ms per file (15 minutes for 20,000 files), the originals are moved **in groups of
  100** with a single shell operation (`SHFileOperation`, ~12 ms per file) — but only on drives where the Recycle Bin
  has been verified to work: a local fixed disk (`GetDriveTypeW`), the volume's Recycle Bin enabled in the registry
  (`BitBucket\Volume\{GUID}\NukeOnDelete = 0`), and the "Do not move deleted files to the Recycle Bin" group policy
  not enabled. Even on this path, if a permanent deletion should become necessary, Windows asks first
  (`FOF_WANTNUKEWARNING`); files that could not be moved are then retried one by one with `QFile.moveToTrash`, and if
  that fails too, the original is not touched.
* **Two-phase renaming:** swaps such as "1.jpg ↔ 2.jpg" are done without collisions.
* **Locked files:** for temporary locks caused by antivirus software, OneDrive or the search indexer, the operation
  is retried at short intervals. Read-only files are not touched.

---

## 6. Sorting, naming and categorizing

* **Order of date sources:** EXIF capture date (including sub-seconds) → XMP → date in the file name
  (`IMG_20240512_143022`, `WhatsApp Image … at 14.30.22`, `Ekran görüntüsü 2024-05-12 143022` (a Turkish-locale
  Windows screenshot name) …) → the file's modification date. The preview shows the source of each date.
* **Sorting:** by capture date, file name (**exactly the same** as Windows Explorer's ordering: 1, 2, 10),
  modification date, size, or manually (drag and drop). Ascending/descending.
* **Undated files and ties:** the base order is the user's **selection order** (the order in which the files were
  selected or manually arranged; when a folder is selected, the name order in Explorer). When sorting by capture
  date, files for which no capture date is found (those with only a modification date) come **after the dated ones,
  in selection order**. Previously they were slotted in between by their modification date; but that date is often
  just the moment of copying or processing (e.g. photos whose metadata had been removed would be ordered by when
  processing finished, i.e. almost randomly). Files with the same date, the same size or the same modification time
  are also ordered by selection order rather than by name. A reverse direction such as "Yeniden eskiye" (newest
  first) only reverses the order of the dated files. "Seçilenler" (Selected) and the confirmation dialog show how
  many files are undated. When grouping into folders by date (years, months or days), files without a capture date
  go into a separate folder named "Tarihsiz" (undated); when sorting by capture date, they are numbered there in
  selection order.
* **Naming:** optional prefix + number (the starting value can be chosen; optional 01/001 padding), or a name
  from the capture date (§6g).
  When categorizing, numbering restarts in each folder. Files that cannot be opened are left out of the numbering
  (no gaps appear in the sequence).
* **Categorizing:** subfolders by month (`2024-05 Mayıs`, i.e. May 2024), year or day, or **"Kaynak klasör yapısını
  koru"** (keep the source folder structure; see §6b).
* In "Yeni klasöre kaydet" (save to a new folder) mode, photos selected from different folders are gathered in a
  single folder (unless the structure is kept); in "Orijinal dosyalara uygula" (apply to the original files) mode
  each photo stays in its own folder.

---

## 6b. Folder selection and large archives

In large archives, instead of opening folders one by one, the folder itself can be selected: the circle in the
top-left corner of a folder tile in the "Gözat" (Browse) grid (or Ctrl+click), the folder's checkbox in the tree on
the left, "Tümünü seç" (select all), and dragging a folder onto the window. **All subfolders** of the selected folder
are scanned in the background.

* **Same rules:** the subfolder scan sees the same files as the "Gözat" grid. Items with the hidden/system
  attribute, names starting with "." (Android's `.thumbnails` preview folder and `.trashed-…` trash files, non-photo
  files such as `._IMG_0001.jpg` that macOS leaves on USB drives) and `$RECYCLE.BIN` are skipped.
  **Folder links** (junctions/symbolic links) are not followed: no loops are entered and the same photo is never
  selected twice. OneDrive folders also carry reparse points but are not links; they are scanned
  (the distinction is made with `st_reparse_tag`).
* **Status indicator:** the folder tile and the tree checkbox show "all selected" (✓), "partly selected" (–) or
  "scanning" (…). For this, the visible folders are counted on a low-priority thread (Windows background I/O mode);
  the results are cached, subfolders whose count is already known are not entered again, and in very large folders
  counting stops at 150,000 entries (shown as, e.g., "10.000+", i.e. 10,000+). The tree does **not** trigger counting
  of the parents of selected folders (so that, e.g., the whole user folder is not scanned). Because files may be added
  to folders during the session, counts are refreshed after 5 minutes, when the folder becomes visible again;
  "Yenile" (refresh, F5) refreshes all of them.
* **The selection is a snapshot:** the photos found during the scan are selected; if a subfolder is deselected while
  the scan is running, that subfolder is removed from the result, and if the scan is stopped, nothing is selected.
  If more than 10,000 photos are found, the user is asked once ("Hayır" (No) undoes everything that selection added).
* **"Kaynak klasör yapısını koru"** (keep the source folder structure): each photo is saved under the name of the
  folder it was selected through (the root) plus its subfolder path (`Archive\2019\Holiday\1.jpg`). With nested
  selections the topmost folder is used; roots with the same name in different places are not mixed up
  (`Holiday`, `Holiday (2)`); individually selected photos go into a folder named after their own folder.
  Numbering starts from 1 in each folder. When photos from different folders are saved to a new folder without
  keeping the structure, the right-hand panel suggests this option.

**Scale: bottlenecks found and fixed** (i7-4700HQ; measured with synthetic archives containing 20,000 photos in 310
folders and 15,000 photos in 3,000 folders):

| Scenario | Before | After |
|---|---|---|
| Plan, "orijinallere uygula" (apply to originals), 20,000 photos | **133 s** (quadratic `in` lookup) | 0.2–0.4 s |
| Plan, 20,000 photos with the same names into a single folder (IMG_0001.jpg …) | 4.9 s (counting up from " (2)" for every name) | 0.2–0.3 s |
| Selecting a folder of 20,000 photos (scan + add) | — | 0.4 s |
| Deselecting the same folder | 1.1 s (parent-folder chain per photo) | 0.02 s |
| "Tümünü seç" in a folder with 3,000 subfolders | 7.1 s, the UI freezing for up to 1.2 s | 4.6 s, worst-case lag 0.12 s |
| "Seçilenler" (Selected) table open while file info is being read | freezes of up to 1.8 s | average 11 ms, p95 14 ms |
| Processing 20,000 photos (structure kept, to JPG) | — | 41 s (with small sample files; all clean) |

Causes and fixes on the UI side:

* While the threads that read photo info run pure Python (Pillow EXIF parsing), the UI thread waits for the Python
  lock (GIL); since painting the table made dozens of Python calls per cell, painting went up from 21 ms to 45 ms.
  Table cells are now computed once per row and cached, the readers are paused briefly while the plan is computed,
  and the lock switch interval was lowered from 5 ms to 1 ms.
* The changing number on the start button ("okunuyor… (12.345)", i.e. reading… (12,345)) kept changing the width of
  the right-hand panel, re-laying out the whole window four times per second and making the 20,000-row table repaint;
  the changing numbers were moved to places that do not affect the layout.
* For large selections the table is updated once when reading has finished, not while the info is being read; the
  plan is computed only while the "Seçilenler" tab is visible; if there is no sorting/grouping by date, processing can
  be started without waiting for the info to be read (files that cannot be opened are skipped during processing and
  reported).
* Scan results are applied in batches of ~0.1 s, folder states are repainted at most ~10 times per second, and the
  question "is this folder being scanned?" is answered via the parent-folder chain, independently of the number of
  jobs.

**Safety for large "in-place" jobs:** the confirmation dialog compares the temporary space needed on each drive
(HEIC→JPG grows by about 2.5×) with the free space. If the total size of the originals to be replaced exceeds the
capacity of that drive's Recycle Bin (`MaxCapacity` in the registry; otherwise ~5% of the drive), it warns: Windows
deletes the oldest items **permanently** to make room, so some of the originals might not be recoverable.

---

## 6c. Large preview

Selected photos can be reviewed in a full window before processing ("Büyük önizleme" (large preview) in
"Seçilenler" (Selected), double-clicking a row, or Space; in "Gözat" (Browse), double-clicking a photo). Del removes
the photo from the selection; pressing Del again puts it back **in its old place in the list and with its folder
root** (manual ordering and "klasör yapısını koru" (keep folder structure) are not disturbed). In "Gözat", the first
click of a double-click changes the selection; this change is undone, so a double-click does not change the
selection.

* **Accurate display:** the preview follows the same steps as conversion to JPG: the EXIF orientation is applied to
  the pixels, and the color profile (embedded ICC, HEIC `nclx`, the EXIF marker of cameras shooting in Adobe RGB,
  CMYK) is converted to sRGB. In other words, what you see on screen is what you will get after processing. In the
  tests the preview deviates on average by less than 1 unit from a reference conversion of the same pixels with
  LittleCMS (Display P3 JPEG and HEIC, CMYK JPEG); all orientation values 1–8 are verified. Transparency is preserved
  (shown over a dark background); for animations the first frame is shown.
* **Speed:** the image is prepared in the background **at screen resolution**; for JPEG, libjpeg's 1/2, 1/4 and 1/8
  scaled decoding is used. (A bug found while measuring: when a square target box was given, the short side limited
  the scale and a 12 MP photo was decoded at full size; fixed so that the target keeps the aspect ratio.) The previous
  and next photos are prepared in advance; meanwhile the thumbnail is shown immediately, blurred, at the photo's real
  size. Prepared images are kept in a 300 MB cache. When the 100% view is needed, the image is prepared at full
  resolution (at most 8192 pixels, so that huge panoramas do not fill up the memory).
* **RAW:** the camera's embedded preview is shown (the same as "Kamera görünümü" (camera view)). If only a small
  image is embedded, the RAW data is processed at half size with LibRaw. The result of the "RAW verisinden işle"
  (process from RAW data) option is not reproduced exactly in the preview.
* **Info panel** (I): the same as "Dosya bilgisi" (file info) in the main window; all metadata found is listed.

| Preparing the preview (i7-4700HQ) | Screen size (1920) | 100% |
|---|---|---|
| 12 MP JPEG (Display P3, orientation 6) | 0.22 s | 0.44 s |
| 12 MP HEIC (single tile — worst case; iPhone's tiled HEICs are decoded with 4 threads) | 1.7 s | 1.75 s |
| 24 MP DNG (embedded full-size preview) | 0.21 s | 0.26 s |
| 4K PNG screenshot | 0.43 s | 0.33 s |

---

## 6d. Rotation, straightening and cropping

Editing is done in the large preview (R / L: 90°, C: crop and straighten) and can be applied to several rows at once
in "Seçilenler" (Selected) (↺ ↻, Ctrl+L / Ctrl+R). **Edits are not written to the file immediately:** they are kept
in memory and applied together with the other options when processing starts; edited photos are processed even if
no other operation is selected (the others are copied as they are). An edited photo that is not selected is added to
the selection. For photos processed with "Orijinallere uygula" (apply to originals), the edit is discarded (it is now
part of the file itself); for photos saved to a new folder the edit is kept, because the originals have not changed.
If the file is modified in another program in the meantime (its modification time differs), the edit is considered
invalid. If Fotodit is closed while there are unapplied edits, the user is asked.

**Model and order** (`fotodit/core/edits.py`): an edit is defined relative to the photo as *displayed* (with the EXIF
orientation applied): (1) 90° rotation — the pixels are only rearranged; (2) straightening — rotation by −45°…+45°
around the center, the canvas size does not change; (3) crop — the box is stored in the 0–1 range relative to the
rotated canvas, so that it covers the same region in the thumbnail, in the screen-sized preview and at full
resolution. When rotated by 90°, the box rotates together with the image; the straightening angle does not change
(rotations about the center commute). The same edit also carries the two non-geometric edits of version 1.1, the
RAW exposure (§4c) and the video trim (§6e); rotating or cropping keeps them, and "Kaldır" (remove) clears all of
them.

**No empty corners when straightening:** the crop box is always kept inside the rotated image. A corner is inside if
its inversely rotated coordinates satisfy |u| ≤ W/2, |v| ≤ H/2; for a box with a given center and aspect ratio, each
condition is linear in the half-height, so the largest box that fits is computed in closed form (side/√2 for a square
rotated by 45° — verified in a test). If there is no box, the largest box with the photo's aspect ratio is used
(automatic crop); if the user's box does not fit, it shrinks while keeping its center and aspect ratio, and returns to
its previous size when the tilt is reduced. When the box is dragged in the crop tool, it stops at the boundary and
keeps sliding along it: since the valid positions form a convex set, the boundary is found by binary search. (In a
random-drag test the box once exceeded the boundary by more than 10⁻⁶ pixels: a box resting against the boundary
drifted outside because of a rounding error in the next operation. Fixed by using a much tighter tolerance in the
search than in the acceptance check; the test now tries dragging, aspect-ratio, angle and 90° changes over 1,200 steps
at 4 angles.) When the edit is applied to the pixels, **only the inside of the box** is sampled bicubically through
the affine transform output pixel → canvas → inverse rotation → source (no intermediate image); transparency is
sampled premultiplied (no color bleeding at the edges), and 16-bit images stay 16-bit. The on-screen (Qt) and
in-file (Pillow) results are rotated in the same direction: the difference between them is ~38 dB, versus ~26 dB had
one of them been rotated the opposite way (checked in a test).

**Quality — JPEG:** re-encoding uses the source's own quantization tables (transposed for 90°/270°; 4:2:2 becomes
4:4:4 when rotated, so that no information is lost). For a crop without straightening, the top-left corner of the box
is **aligned to the source's block (MCU) grid** (by computing where it lies after orientation and rotation; the box
shifts by at most 15 pixels, and its size does not change). This way the re-encoded 8×8 blocks coincide with those of
the source, and coefficients quantized with the same tables stay almost identical; with an unaligned crop, every
block is re-quantized. jpegtran's lossless crop works under the same constraint.

| 12 MP JPEG (quality 92), i7-4700HQ | Time | Difference from the source (PSNR) | Size |
|---|---|---|---|
| Rotate 90° right | 0.37 s | 50.0 dB | 0.95× |
| 50% crop, aligned to the block grid | 0.17 s | 50.3 dB | 0.25× |
| (for comparison: the same crop, unaligned) | | 38.5 dB | |
| 90° + crop | 0.26 s | 49.7 dB | 0.46× |
| 3° straightening (resampling) | 1.2 s | 38.7 dB | 0.81× |
| HEIC → JPG + 90° | 1.9 s | | |

**Other formats:** for PNG, lossless WebP, TIFF and BMP, 90° rotation and cropping are completely lossless
(pixel-for-pixel identical in the tests). Lossy WebP (95%), HEIC and AVIF (90%) are recompressed — the confirmation
dialog warns about this. For GIF, APNG and animated WebP the edit is applied to every frame, and for multi-page TIFF to
every page. A **RAW** file itself cannot be rotated or cropped: the edit is applied only when converting to JPG (the
camera's embedded JPEG is re-encoded with its own tables); if conversion is off, the plan skips the file and gives
the reason.

**With metadata removal turned off**, an edited photo keeps its metadata, but the metadata is adapted to the edit: the
EXIF orientation is set to 1, the EXIF thumbnail (IFD1) **has its bytes zeroed and is unlinked** (EXIF is not
rewritten, so that the MakerNote offsets do not break), PixelXDimension/PixelYDimension are updated to the new size,
the orientation in XMP is corrected, and the thumbnails in the Photoshop block are removed (IPTC stays). Additional
images derived from the old image (MPF, a second image at the end of the file, the HDR gain map), the JFXX thumbnail
and the C2PA content signature (which is invalid for the edited image anyway) are removed. Keeping the old thumbnail
would both make Explorer show a wrong (unrotated/uncropped) thumbnail and **leave the cropped-out region in the
file**. HEIC's `nclx` color information is carried over as an equivalent ICC profile.

**Preview and thumbnails:** in the preview the image itself stays unchanged; the edit is applied as a transform while
painting, so rotation and cropping show up instantly (a 4K image in a 1900×1000 area: 5–11 ms/frame). A cropped photo
is prepared at up to 4× the size, so that the cropped region looks sharp too. For thumbnails, a rotation is derived
instantly from the existing thumbnail; for a crop, a cropped version of the thumbnail is shown first, followed by a
sharp version decoded at a larger size.

---

## 6e. Videos

Request: videos should be selectable too; their metadata should be removed as for photos, they should be convertible
to **MP4**, rotatable and croppable, **named separately from photos** in bulk selections, and **playable** inside the
program.

### Choosing the infrastructure

| Option | Assessment |
|---|---|
| **PyAV 18.1 (FFmpeg 8.1.2) + Qt Multimedia (PySide6-Addons 6.11.2)** ✅ | PyAV uses FFmpeg's libraries in-process: packet-by-packet reading/writing (which allows byte-level work such as SEI cleaning), frame decoding, filter graphs, encoding. The Windows wheel comes with x264, x265, dav1d, SVT-AV1, libvpx, Opus, LAME and AMR (27.6 MB). Qt Multimedia provides ready-made playback with audio and A/V sync (with its own FFmpeg 7.1.5, LGPL). |
| The installed `ffmpeg.exe` (winget, 9.0.1 "full") | Solid for conversion but 222 MB; packet-level cleaning is not possible from the command line, and there is no player. |
| Windows Media Foundation (ctypes) | Needs no download but is very fragile via COM; HEVC requires a paid extension (not present on this computer), and MKV/WebM/AVI support is lacking. |
| Writing the player with PyAV (waveOut for audio) | Possible, but many things such as A/V sync, seeking and audio device changes would have to be rewritten. |

The downloads were made with the user's approval: `av-18.1.0-cp311-abi3-win_amd64.whl` (27.6 MB) and
`pyside6_addons-6.11.2-cp310-abi3-win_amd64.whl` (168.2 MB; only the playback part goes into the package).
Because Python 3.13 was removed from the computer and 3.14.7 was installed during the work, the virtual environment
was rebuilt with 3.14 (same package versions; only 5 packages built specifically for 3.14, ~29 MB, were downloaded
again).

### Where is metadata stored in videos?

| Source | Where | Content |
|---|---|---|
| iPhone (MOV) | `moov/meta` 'mdta' keys | `com.apple.quicktime.location.ISO6709` (location, altitude), make/model/software, `creationdate` (with time zone) |
| iPhone | 'mebx' timed metadata tracks | face detection, Live Photo, scene information |
| Android | `udta/©xyz`, `loci` | location; header date (UTC) |
| GoPro | 'gpmd' data track, `udta` (FIRM, CAME, GPMF, MUID) | 18 GPS readings per second, speed, sensors; camera ID |
| DJI | 'djmd'/'dbgi' tracks, subtitle track | flight log, GPS in every frame |
| Sony | 'rtmd' track; in XAVC, H.264 SEI "MDPM" | shooting log, GPS |
| AVCHD camcorders (MTS/M2TS) | **inside the video stream**: H.264 SEI "user data unregistered", MDPM block | capture date + time zone, model, exposure, **GPS** |
| Canon / Nikon / Panasonic / Fujifilm | `udta` boxes (CNTH, CNCV, NCDT, PANA, FFMV …) | thumbnail with EXIF, serial no., location |
| Samsung | `udta/smta`, SEF block at the end of the file | model, shooting information |
| Adobe / Windows | 'uuid' XMP box, `udta/XMP_`, `Xtra` | edit history, author, tags |
| Encoders | x264's SEI settings string | software and settings |
| MJPEG AVI (older cameras) | every frame is a JPEG: APPn segments | EXIF |
| MPEG-1/2 | user_data (0x1B2) | recorder information |
| MKV / AVI / WMV | Tags, Attachments; INFO list; ASF description objects | title, author, date, cover art |

`ffmpeg -map_metadata -1 -c copy` leaves some of these behind: track tags (e.g. "Pixel 8 Camera"),
data/subtitle tracks copied with `-map 0`, chapters, the "Lavf…" software tag and SEI user data in the video stream
(including the GPS in MDPM).

### How does Fotodit remove metadata from videos?

The container is **written from scratch** (`fotodit/core/video/process.py`): only the main video track and the audio
tracks are transferred, with their packets unchanged (no quality loss, at disk speed). None of the tags are written,
and neither is FFmpeg's own signature (`fflags=+bitexact`); the dates in the file header (mvhd/tkhd/mdhd) are zeroed.
Data tracks, cover art, attachments and chapters are left behind (with a "Kaldırıldı: …" (Removed: …) note on the
results screen). Subtitles are kept (see below). At the packet level (`fotodit/core/video/bitstream.py`):

* H.264/HEVC: the SEI messages in every packet are parsed (removing and re-inserting the emulation prevention bytes),
  and **only "user data unregistered" (type 5)** is removed; messages that affect decoding, such as HDR, recovery
  point and timing, stay. The SEI arrays in the HEVC configuration (hvcC) are cleaned too. When re-encoding, the
  encoder's own string (x264's SEI in the first packet, x265's in the configuration) is also removed before it is
  written to the file.
* MJPEG: the APP1–15 and comment segments in the frames are removed (JFIF/AVI1 and Adobe APP14 stay).
* MPEG-1/2: user_data blocks are removed.

The orientation information (portrait recording) is preserved. **Verification:** the output is reopened and the
following are checked: track types/counts, duration (±3%), decoding at the start and at the end, the packet count of
each track (exactly as written), orientation; if metadata was removed, also the container and track tags, all boxes
other than 'mdat' in MP4/MOV (Apple/Android key signatures, XMP, EXIF, MDPM, ISO 6709), the SEI user data in the
first 60 frames and the lines of the subtitles that were kept. The first frame of a re-encoded video is compared
with the frame that went into the encoder (to guard against a faulty encoder). If there is a problem, the file is
not touched.

### Subtitles

Subtitles are mostly the user's own content (movie subtitles, added captions); they are **kept** when metadata is
removed, and only their language (`tur`, `eng` …) and default/forced flags remain (other tags such as the title are
removed). But some devices use the subtitle track as a log: DJI drones write
`F/2.8, SS 500, ISO 100, EV 0, GPS (28.97, 41.00, 19), D 12.3m …` or `[latitude: …] [longitude: …]` for every
frame, and dashcams write the date/time, speed and location. Fotodit recognizes these tracks by looking at their
first 30 lines (within the first 32 MB of the file) (`fotodit/core/video/subtitles.py`): if at least half of the lines
contain coordinates, the words GPS/latitude/longitude, NMEA sentences or camera settings, the track is a **log** and
is removed when metadata is removed ("Kaldırıldı: altyazı izindeki konum ve kamera ayarları kaydı" (Removed: location
and camera-settings log in the subtitle track)); the info panel shows it as location data. For tracks that contain
only dates/speeds, at least 3 lines are required. The word "GPS" appearing once in a movie subtitle does not get the
track removed. Previously every subtitle track in an MP4 triggered a GPS warning because it "might carry location
data"; now the content of the track is examined.

When converting to MP4, SRT, ASS and WebVTT subtitles in MKV/WebM are converted to MP4's subtitle format (mov_text)
(PyAV 18's subtitle decoder + the mov_text encoder; italic/bold are preserved, and ASS's own styles are read from its
header). DVD subtitles (bitmaps) are written to MP4 as they are; Blu-ray (PGS) subtitles do not fit into MP4 and are
dropped with a note.

### Conversion to MP4

| | "En uyumlu: H.264" (most compatible; default) | "Hızlı ve kayıpsız: kodek korunur" (fast and lossless: codec kept) |
|---|---|---|
| Video copied as is | 8-bit 4:2:0 H.264 | H.264, HEVC (with the `hvc1` tag Apple expects), AV1, VP9, MPEG-4 |
| Video re-encoded | HEVC, AV1, VP9, MJPEG, WMV/VC-1, MPEG-2, 10-bit/4:2:2 H.264 … | MJPEG, WMV/VC-1, MPEG-2 … |
| Audio copied as is | AAC, MP3 | AAC, MP3, ALAC, AC-3, E-AC-3, Opus, FLAC |
| Audio encoded to AAC | everything else (PCM, AC-3, AMR, WMA, Opus, FLAC …) | PCM, AMR, WMA … |

Re-encoding: x264, **CRF 18** (visually indistinguishable), the "fast" preset up to 720p30 and "veryfast" above that;
audio AAC 192 kb/s (mono 128, 5.1 384). Timestamps are preserved (variable frame rate is not broken). Interlaced camera
videos are converted to full frames with bwdif. MTS/M2TS (AVCHD) with H.264 becomes MP4 without re-encoding.

**HDR (iPhone 12+):** if the colors are passed through as they are when 10-bit HLG/PQ video is converted to H.264
(8-bit SDR), the image looks washed out and gray. The FFmpeg build has no `zscale`/`libplacebo` to do this; the
conversion is baked into a 3D table (65³, `lut3d` with tetrahedral interpolation) (`fotodit/core/video/tonemap.py`): HLG
inverse OETF + BT.2100 system gamma (1000 cd/m²) or PQ EOTF → BT.2020-to-BT.709 matrix → luminance mapping with an
exponential shoulder applied to the brightest channel (BT.2408 reference white 203 cd/m² → 96% signal in SDR) →
BT.1886 gamma. Test: HLG reference white comes out at 245/255.

**GPU encoding** (`fotodit/core/video/hwenc.py`, the "Ekran kartıyla kodla" (encode with the graphics card) option):
NVIDIA NVENC, Intel Quick Sync, AMD AMF and Windows Media Foundation are tried in that order; the first time (once per
session), an 8-frame 320×240 test encode is made, decoded back, and checked for a correct image (left half dark, right
half light). If during processing the encoder cannot be opened, reports an error, or the first frame differs clearly
from the source's (similarity below 25 dB on a 64×48 thumbnail; above 40 dB for a correct encode), the video is
re-encoded on the CPU (x264) and this is mentioned in the result note; an encoder that produced broken output is not
used again in that session. The settings were chosen by measurement to keep the quality close to x264 CRF 18. On this
computer (Intel HD 4600 + NVIDIA GTX 850M), with synthetic 1080p footage:

| Encoder | Frames/s | Bit rate | PSNR |
|---|---|---|---|
| x264 veryfast CRF 18 (CPU) | 69 | 5.9 Mbit/s | 39.1 dB |
| NVENC p5 hq, CQ 23 (chosen) | 115 | 9.5 Mbit/s | 39.4 dB |
| Media Foundation, quality 80 (Intel's encoder) | 90 | 6.3 Mbit/s | 39.3 dB |

Intel Quick Sync (oneVPL) does not open because it does not support this generation of Intel (Haswell); Intel's
encoder is reached through Media Foundation instead. End to end (decoding + filters + encoding): a 20 s 1080p HEVC →
H.264 conversion takes 9.8 s with x264 and 4.2 s with NVENC (file 41 → 31 MB); a 10 s 4K HDR → H.264 conversion goes
from 31.7 to 18.5 s (a smaller gain, because tone mapping stays on the CPU).

**HEVC:** the x265 in the PyAV wheel was built without assembly optimizations (0.4 frames per second at 4K), and
Windows' HEVC encoder requires a paid extension. If the GPU has a 10-bit HEVC encoder (NVIDIA Pascal and later, Intel
7th generation and later, AMD Polaris and later), HDR videos that are cropped/straightened while "kodek korunur" (codec
kept) is selected are encoded **as HDR** (10-bit HEVC, with HLG/PQ color information and the `hvc1` tag). Since the
GPUs in this computer cannot encode HEVC (the GTX 850M is first-generation Maxwell), this path was tested here with
x265; if the GPU has no such encoder, HDR video is tone-mapped to H.264 as before.

### Rotation and cropping

Editing is relative to the video as **displayed** (with its orientation applied); the same tools as for photos are
used. **Only 90° rotation** is **lossless** in MP4/MOV/3GP: only the orientation information (the tkhd matrix)
changes, just as phones do for portrait recordings. Cropping and straightening require re-encoding (FFmpeg's
`transpose`/`rotate`/`crop` filters; the box is aligned to the even pixel dimensions that H.264 requires). For
containers that cannot carry orientation information, such as AVI, MKV and MTS, the edit is applied only when
converting to MP4; if conversion is off, the file is skipped and the reason is shown (the same as the RAW rule).

### Trimming

The bar under a video has **"Buradan başlasın"** (start from here) and **"Burada bitsin"** (end here): the kept part
is stored as an edit (start/end in seconds) and drawn on the seek slider.

**Without re-encoding.** A copied stream can only start at a keyframe. Fotodit first reads the video packets
(without decoding; for MP4/MOV/3GP from a keyframe a little before the start, using the file's index) and finds the
first frame at or after the start and the last keyframe before it. The packets from that keyframe on are copied, and
every timestamp is moved back so that the first kept frame is at 0; the frames before it get negative times.
FFmpeg's MP4 muxer then writes an **edit list** (`elst`) whose media time skips them: players decode from the
keyframe but show exactly from the first kept frame (this is what `ffmpeg -ss … -c copy` produces as well). At the
end, the packets decoded before the end point are kept (FFmpeg's rule, `dts < end`): with B-frames, an anchor frame
shown after the end point can be needed by the B-frames before it. In the experiment those frames showed past the
end, with a skipped frame (… 73, 74, 75, 77), so Fotodit shortens every track's edit list, the track durations and
the movie duration in place to the end point (`boxes.end_presentation`): the presentation then ends exactly at the
last kept frame (in the test: frames 32–74, nothing before or after). Audio packets that overlap the window are kept
(the first one partly, hidden by the edit list), subtitle cues and chapters are cut to the window and shifted, and
data samples must lie inside it.

**What stays in the file.** The frames from the keyframe before the start (up to one keyframe interval: usually 1–2 s
for phones, up to a few seconds for some cameras) and the few frames decoded before the end stay in the copied file,
hidden by the edit list: a player shows nothing of them, but a tool that ignores edit lists can extract them. For a
privacy tool this is stated plainly: the trim buttons' tooltip says so, and the result notes say how many seconds
stay hidden before the start. To remove a part for good, "Bölüm çıkar" re-encodes (below).

**Re-encoding.** When the video is re-encoded anyway (cropping, straightening, conversion), the decoded frames
outside the window are dropped (frame-exact), and audio converted to AAC is cut sample-exactly.

**Formats and players.** Edit lists exist only in MP4/MOV/3GP: in AVI, MKV, MTS … trimming is applied while
converting to MP4 (the same rule as for rotation). FFmpeg (and so Fotodit's own preview), browsers, VLC and QuickTime
honor edit lists; a player that ignores them would show the frames from the keyframe on and a few frames past the
end. The duration check of the verification uses the kept duration.

### Removing parts

**"Bölüm çıkar"** (remove a part) is pressed at the start of the part and again at its end ("Bölüm burada bitsin");
while it is being marked, the part is drawn red on the seek slider, and Esc drops it. The edit stores the removed
parts (sorted, overlapping or touching ones merged, parts shorter than 50 ms ignored); a kept piece shorter than
0.3 s next to a removed part goes along with it, and removed parts outside the trim are cut to it.

**Always re-encoded.** An edit list with several entries could skip the removed parts without re-encoding, but many
players and editors do not support more than one entry, and the removed frames would still be in the file. So
removing a part re-encodes the video (H.264, as for cropping) and its audio (AAC): the decoder's frames and samples in
the removed parts are dropped, and the rest is joined with every timestamp moved back by the time removed before it.
A part removed at the very start or end is removed the same way (with nothing hidden), unlike a trim.

**Joining.** Audio is cut sample-exactly at the removed parts' bounds and the encoder writes the kept samples one
after another, so audio and video lose exactly the same time. When the video's time base is coarse (AVI counts in
whole frames) the frames on either side of a cut can round to the same timestamp; the later one is then moved by one
tick. Subtitle cues that overlap a removed part get shorter (the parts of a cue on either side of it join up), and
chapters are cut and moved the same way. The note in the result lists the kept parts. Tested with B-frames and
keyframes every 12 frames: each output frame identified by its content (two removed parts plus a trim, frames 6–11,
36–59 and 72–95 exactly), audio length, subtitle timings, chapters, an AVI with a cut that does not fall on frame
boundaries.

### Date (sorting, naming, grouping into folders)

1. The iPhone's `creationdate` tag is written with the time zone: the local time at the place of recording is taken
   as it is.
2. The AVCHD MDPM date is local time.
3. The MP4/MOV header date is UTC according to the standard (phones write it that way). If the file name contains a
   date (`VID_20240712_183010`) that differs from the UTC time by an exact time-zone offset, the local time from the
   file name is used; otherwise the header time converted to the computer's time zone. GoPro and cameras write local
   time into the header; for them no conversion is done. AVI dates are local, MKV/WebM/WMV dates are UTC.

### Separate naming

"Sırala ve yeniden adlandır" (sort and rename) has a separate **prefix** and **first number** for photos and for
videos; the two kinds are numbered separately in each folder (`Holiday_1.jpg, Holiday_2.jpg …` and
`Video_1.mp4, Video_2.mp4 …`), while the sort criterion is shared. A converted video gets the `.mp4` extension.

### Playback

Qt Multimedia's FFmpeg backend opens the video; frames are taken from a `QVideoSink` and painted in the preview's own
image area. This way rotation/straightening/cropping are visible during playback too, and crop mode works on the
paused frame. Three important findings from testing:

* `QVideoFrame::toImage()` **applies the video's orientation itself** (the surface transformation); the only thing it
  does not apply is the frame's additional rotation. The first version applied the orientation a second time. This
  was noticed in a screenshot of the real exe; the test now checks the frame's corner colors.
* Qt's hardware decoding was turned off (`QT_FFMPEG_DECODING_HW_DEVICE_TYPES=,`): the Intel driver took ~3.5 s to get
  ready for HEVC on every video, and since the frames are painted on the CPU anyway, they also had to be copied back
  from the GPU.
* Converting a 4K 10-bit HDR frame to an image takes ~40 ms: the conversion is done on a separate thread, and frames
  are dropped if it cannot keep up (the UI stays smooth).

**Fallback player** (`fotodit/ui/softplayer.py`): for AV1, Qt's FFmpeg only has a decoder that requires the GPU
(there is no dav1d software decoder; hardware decoding is turned off as well, and the GPUs in this computer cannot
decode AV1). AV1 videos are therefore played with PyAV (dav1d): video and audio are decoded on separate threads, with
the file opened twice (so that the audio/video interleaving in the file does not make the queues grow); the audio is
written to a `QAudioSink` and the clock is derived from the amount of audio played, with frames shown according to
that clock (late frames are dropped; if the audio ends before the video, the clock continues from the system clock).
Seeking, pausing, volume and orientation work as in Qt. If Qt cannot open some other video, or if no frame arrives
although the video position has advanced by 2 s, playback also switches to the fallback player from where it was;
the user is notified only if the fallback player cannot play it either. The fallback player downscales frames to at
most 1920×1080 and does not tone-map HDR (it is for previewing). The thumbnail is a frame taken from the beginning of
the video (at most 1 s in, 10% for short videos); in MPEG-TS, since a seek may land beyond the only keyframe, the file
is read from the start if necessary.

### Measurements (this computer)

| Operation | Time |
|---|---|
| 4K HDR HEVC (21 MB, 10 s): metadata removal / MP4 keeping the codec / 90° rotation | 0.3–0.5 s (lossless) |
| 1080p HEVC → H.264 (20 s) | 0.49× the video's duration (0.21× with the GPU) |
| 1080p crop (re-encoding) | 0.45× |
| 4K HDR HEVC → H.264 + tone mapping | 3.2× (1.85× with the GPU) |
| Thumbnail (720p–1080p / 4K HDR) | 10–20 ms / 60 ms |
| Playback in the preview | 1080p HEVC 30 fps; 4K HDR ~17 fps; AV1 (fallback player) at the video's own frame rate |

---

## 6f. The interface: Liquid Glass, light and dark

Version 1.3 moved the interface to Apple's Liquid Glass, in the form iOS 27 gives it (2026): thin edges and a calm
body rather than strong optics. Where glass goes and how it behaves come from Apple's Human Interface Guidelines and
the WWDC 2025 and 2026 sessions. Apple publishes no figures for the material, and WWDC 2026 gave none either; the
figures below come from pixel measurements of iOS 27 on a device and in the simulator (over black, white and grey)
and from Apple's iOS 27 design kit as third parties transcribed it (sources in §12).

**Where glass goes.** Glass is the layer of controls that floats above the content, never the content itself: the
sidebar and the inspector (edge to edge and without a shadow, as in macOS 27), the toolbar capsules, the
segmented control, the bars of the large preview and the sheets. The grid, the table and the photos stay plain.
Glass never shows glass: a surface leaves other glass out when it samples what lies beneath it. Following Apple's
toolbar rules, neighbouring buttons share one capsule, text buttons and symbol buttons get separate capsules, and
the one primary action ("N fotoğrafı işle", process N photos) is prominent: solid ink, near black in light mode and
near white in dark mode. When a bar does not fit, its buttons first lose their text (the first one last), then
whole groups step aside; their commands stay in the context menus.

**How it is drawn.** Qt Widgets has no backdrop filter, so a glass surface draws itself (`ui/glass.py`):

1. *Backdrop.* What lies under the surface, plus a margin, is rendered into an image: the parent's background and
   the widgets beneath it (`QWidget.render`), or the large preview's own picture of the photo.
2. *Frost, two paths.* A wide blur (σ about a quarter of a bar's height, computed on a reduced copy) mixed with
   about 30% of the backdrop left sharp, as measured on iOS 27: what lies behind reads as a soft cloud with a trace
   of its detail. A single medium blur, the usual imitation, leaves recognizable blobs.
3. *Lens.* The outer half of the corner radius is the edge of a plano-convex lens with a convex squircle profile,
   h(x) = T·(1 − (1 − x)⁴)^¼. For each pixel of that band, a ray from the eye is refracted into the glass (Snell's
   law, index 1.5) and followed through its thickness; the pixel shows the backdrop where the ray lands, so content
   near the edge is pulled inward. Through the frost this reads as the soft bend iOS 27 shows (the measured contrast
   through the band is 0.13 to 0.16), not as a magnifying glass. The displacements depend only on the shape, so
   they are computed once per size and cached.
4. *Veil.* The measured iOS 27 values: (243, 243, 243) at 55% on buttons and bars in light mode, (77, 77, 77) at
   42% in dark mode; the sidebar and the inspector are denser (78%). Glass over photos darkens itself over bright
   images, up to a dark veil of 72%, which keeps white symbols at about 4.5:1.
5. *Edge, with no line all around.* A specular highlight comes from straight above and is added to whatever lies
   beneath (not blended): 0.25 in the outermost pixel, half of that 0.8 pt in and a tenth 1.8 pt in, as bright on
   the bottom edge as on the top, and fading toward the sides with (1 − θ/90°)^1.33, θ being the angle of the
   edge's normal from the vertical. A thin dark contour lies just outside the shape (0.5 pt) and darkens what is
   next to it: in light mode from 8% at the top and bottom to 31% at the sides, in dark mode from 0 to 61%, with
   (1 − |cos θ|)^0.75. Over white the contour draws the shape, over black the highlights do.
6. *Shadow.* Buttons and bars cast almost none (3 to 5% in light mode, at most 6% in dark mode); only the sheets
   keep a large, soft shadow.

The first version of the 1.3 glass looked fake for exactly the reasons the measurements name: a bright line ran
all around the shape, a darker line lay inside the edge, the highlight sat a little inside the edge like a bead,
the blur was a single medium one, and the shadows were heavy. None of these is left.

**Speed.** A surface reuses its last image while its backdrop is unchanged (a hover on one of its buttons, for
example). When the backdrop changes, redrawing takes 5.0 ms for a 320 × 40 toolbar capsule and 9.7 ms for a
620 × 40 one at 125% display scaling on this computer (median of 40 frames): the blur and the sharp copy are
computed only for the surface itself, not for the margin around it. Scrolling "Gözat" moves the thumbnails under
four glass bars, which used to render the same thumbnails four times per frame; they now share one rendering per
frame, and the grid draws each tile once and then copies it. One scroll step takes 27 ms (35 ms in the first 1.3
build, 57 ms with the new glass before these changes), and 11 ms with "Saydamlığı azalt" (median of 75 steps of
4 pixels in a 1440 × 880 window).

**Uniform bar on scroll.** As in iOS 27, once the content scrolls beneath the floating bars, a frosted strip fades in
behind them in 0.25 s (white at 85% in light mode, black at 55% in dark mode, with a hairline of 5% or 10% under
it), so file names never run under the bars' text.

**Glass over photos.** The large preview is a dark stage. Behind the photo lies Apple's background extension: a
blurred, darkened copy of the photo fills the window, so the bars float over the picture's own colors. While a
video plays, the controls step aside after 2.6 s without mouse movement and come back when the mouse moves.

**Controls.** The selection of the segmented control slides between its segments; the knob of a switch and the
thumb of a slider move on springs, and a slider's thumb turns into a glass lens while it is dragged. A pressed
glass button swells by a pixel and lights up from under the pointer. Sheets (the confirmation, the progress and
results, About) are glass cards over the dimmed window, with the default action as the prominent button (Enter)
and Esc to cancel. Short messages appear in a glass capsule above the bottom bar. Pop-up buttons show their
chevron and number boxes their step arrows. A choice too long for its box ends in "…", and the list opens wide
enough to show every choice in full; a column title that does not fit ends the same way. The mouse wheel over a
pop-up button or a number box scrolls the panel rather than changing the setting under the pointer (as on macOS),
and a number box takes the wheel only once it has been clicked.

**Motion.** Springs use Apple's two parameters, response and damping ratio (with a mass of 1: stiffness =
(2π / response)², damping = 4π · ratio / response). Hover and press feedback is critically damped; the sliding
selection, a switch's knob and a slider's lens settle with a slight overshoot (damping ratio 0.82 to 0.9). Every
animation starts from the value on screen, so it can be interrupted at any moment. When Windows' animation effects
are turned off, everything moves without animation.

**Icons.** SF Symbols may only be used on Apple's platforms. Fotodit uses 48 icons of Hugeicons' free set (MIT, the
"stroke rounded" style: one stroke width and round ends everywhere, close to SF Symbols' regular weight and to Inter's
rounded shapes), taken unchanged from the npm package `@hugeicons/core-free-icons` 4.3.5 into
`resources/icons/hugeicons.json`. `ui/icons.py` reads their SVG path data (every command, arcs included) into Qt
paths and draws them in the theme's colors, so they stay sharp at every size and display scale; the play and pause
glyphs are drawn filled, like SF Symbols' ".fill" variants. The set covers every icon the app needs (Lucide, the
fallback, was not needed); 14 old drawn icons that no screen used any more were dropped. Animate UI's animated
icons were the starting point, but their license is MIT with the Commons Clause, which conflicts with the GPL,
so the motion is Fotodit's own: under the pointer an arrow nudges the way it points, a rotate or
refresh arrow turns, a plus or a cross spins a quarter, a magnifier or the full-screen arrows grow, the flip icon
turns over, the rest grow by 8%. The button's hover spring drives the motion (critically damped, so it follows the
pointer and turns back midway without bouncing), and with Windows' animation effects off the icons stay still.

**Typeface.** SF Pro may only be used on Apple's platforms too. Fotodit ships Inter 4.1 (SIL Open Font License), a
typeface drawn for screens, with a tall x-height and open shapes, in its hinted static faces (Regular, Medium,
SemiBold, Bold) at 9.5 pt; the variable version runs into a Qt 6.11 bug (QTBUG-149199). At 125% and 150%
display scaling Qt hints only small text and leaves larger text unhinted, which blurs titles; Fotodit asks for
vertical hinting at every size there, which keeps lines on the pixel grid without crowding the letters. Windows gives
lists, trees, tables, menus and tooltips fonts of their own (Segoe UI 9), which come back with every style sheet
change, so Fotodit sets them to Inter after each one. Digits are Inter's own; counters, timers and the date column
of "Seçilenler" use tabular digits so they do not shift (Inter's tabular forms widen hyphens and would spread out
file names such as `C--Users`). Contextual alternates are off: they would turn the "->" of a file name into an
arrow. Names on the tiles keep their weight when selected, so they never jump or get shortened on a click.
Headings (the panels' titles, the operations, column and tab names, sheet titles) and buttons are set in Inter
Bold and in title case: every word capitalized except conjunctions and particles ("Sırala ve Yeniden Adlandır",
"Liste Boşaltılsın mı?"; in English "Group into Folders"), the Turkish way with a dotted capital İ ("Videoyu
İşle"). Menu items, check boxes, radio buttons and sentences keep sentence case.

**Languages.** The interface is in Turkish and English. Every text a person reads lives in two dictionaries,
`fotodit/i18n/tr.json` and `en.json`, and reaches the screen through `t(key, …)`: placeholders fill in names and
numbers, and English picks "1 photo" or "2 photos" by the count (Turkish needs one form). Numbers, decimals,
percentages, sizes and dates are written each language's way (12.345 / 12,345; %95 / 95%; 12.05.2024 /
2024-05-12), and so are the folder names Fotodit creates (`2024-05 Mayıs` / `2024-05 May`, `Tarihsiz` /
`Undated`). At first the language follows Windows' display language (Turkish when it is Turkish, English
otherwise); "Görünüm → Dil" chooses one for good. The texts are set as the windows are built, so a new language
shows after a restart, which Fotodit offers and does itself (the one-file build starts the new copy as an
independent instance, `PYINSTALLER_RESET_ENVIRONMENT=1`, so it does not reuse the unpacked files of the copy that is
closing). Qt's own texts (the buttons of the Windows file dialogs) follow: its Turkish translation is loaded only
for Turkish. The tests check that both dictionaries hold the same keys and placeholders, that every key the code
names exists and none is left unused, that no Turkish text and no word written straight into a label is left in
the code, and that the whole window in English shows no Turkish.

**Color: black, white and grey.** The interface has no accent color. Ink (near black in light mode, near white in
dark mode) marks what is on or chosen: the primary button, switches that are on, check marks, selected thumbnails;
greys carry everything else, and selected rows get a grey fill. Only warnings (orange) and errors (red) keep a
color, together with a symbol, so that color is never the only sign. The large preview keeps its dark stage in both
variants (photos are judged on a dark background).

**Looks and accessibility.** Like iOS (a choice of clear or tinted since 26.1, a slider in iOS 27), "Görünüm"
(appearance) offers three looks for the glass: "Cam:
Daha şeffaf" (clearer: a thinner veil, twice the sharp share, less blur), "Varsayılan" (default) and "Daha buzlu"
(frostier: a veil of at least 74%, no sharp copy, more blur). "Saydamlığı azalt" (reduce transparency) turns every
glass surface into a solid one with the same edge, like Apple's setting of the same name. It is Fotodit's own
setting rather than Windows' "Transparency effects": many Windows 10 machines have those turned off for the
taskbar's sake, which would hide the material entirely. Focus rings appear after keyboard navigation (Tab), not
after a click.

**Light and dark.** "Görünüm": "Windows ayarına uy" (follow the Windows setting; the default), "Açık" (light) or
"Koyu" (dark); the choices are remembered. The colors are named tokens (ACCENT, TEXT, MUTED, SEPARATOR …) with a
value for each variant. Qt's palette is built from them completely, including the disabled group and the roles
Fusion uses for frames and scroll bars, so that nothing falls back to the system's colors; the style sheet and the
indicators Fotodit draws itself (check boxes, radio buttons, the chevrons of lists, trees, pop-up buttons and
number boxes) use them too. Code that paints reads the tokens when it paints; widgets that keep a color (the cached
cell colors of "Seçilenler", the HTML of the info panel, the glass images, the drawn tiles) listen to a change
signal, so switching needs no restart.
`QStyleHints.setColorScheme` tells Windows the scheme, which also turns the window title bars dark (Qt 6.8+); with
"Windows ayarına uy" a change of the Windows setting is followed while the app runs.

Contrast on the window and panel backgrounds (WCAG ratio, the lower of the two; light / dark): text 16.2 / 13.5,
secondary text 5.8 / 6.4, warnings 5.6 / 7.4, errors 5.8 / 6.0, success and edits (grey) 10.2 / 9.9; the label of
the primary button on its ink 18.1 / 16.5. On a selected row: secondary text 4.8 / 4.9, warnings 4.6 / 5.7, errors
4.8 / 4.7.

**Use.** In "Gözat" a folder opens on a double-click; a single click only highlights it, so a click meant for a photo
never takes you into another folder (the tree on the left still opens a folder with one click on its name, as in
Explorer). The path bar shows where you are as steps, `Resimler › 2024 › Tatil`, with a known place (Masaüstü,
Resimler …) standing for its folder and drives named as Explorer names them; a click on a step goes up to it, a click
on the empty part or Ctrl+L turns the bar into a field to type a path into, and a path that does not exist stays in
the field to be corrected. In the operations panel a switched-off operation hides its settings, and the longer
explanations moved into tooltips, so the panel fits a narrow window without cutting off its switches.

---

## 6g. Version 1.3 features

Two market scans (October 2026) compared Fotodit with privacy tools (ExifCleaner, mat2, Scrambled Exif,
BatchPurifier …), renamers and organizers (Advanced Renamer, PowerRename, digiKam, XnView MP …), duplicate finders
and video tools (HandBrake, LosslessCut, Shutter Encoder …), and collected what their users ask for in issue
trackers and forums. The features below answer the requests that fit Fotodit and need no new package.

**Keeping the capture date ("Çekim tarihini koru").** Removing everything also removes the date, so galleries
(Google Photos, iCloud) put a cleaned photo on the day it was processed; ExifCleaner's users asked for dates for
years (issue #250) and ExifTool's forum has "delete all but the date" threads. The option is off by default,
because a timestamp can tie a photo to an event. When it is on:
* a photo keeps one EXIF field, DateTimeOriginal, in a new 70-byte EXIF block: written at the byte level into
  JPEG, PNG, WebP and TIFF, handed to the encoder for HEIC and AVIF. OffsetTime (which hints at the region) and
  SubSecTime (which links the shots of a burst) are never written;
* an MP4, MOV or 3GP video gets the date in its header times (converted to UTC with the computer's time zone, as
  the format requires), with no tag box;
* the output file's modified time is set to the capture date;
* GIF and BMP cannot carry EXIF and other video containers no header date, so they keep the date only as the file
  date, and the result says so. A file without a capture date gets no date at all (no stand-in date, the same rule
  as for sorting).

Verification changes from "no metadata" to "exactly this one field": a second EXIF block, another field, a
different date or anything else makes the file fail.

**Date-based names.** PowerRename only gained capture-date tokens in 2025, and users ended up in Bulk Rename
Utility for it. "Ad biçimi" (name format) offers "Çekim tarihi ve saati" (`2024-05-12_14-30-22`) and "Çekim günü ve
numara" (`2024-05-12_001`, counting from 1 each day) next to prefix + number. Files from the same second get `_2`,
`_3` only when the whole name, extension included, would clash, so a RAW and its JPEG keep matching names; files
without a date become `Tarihsiz_1` … Because such a name carries the date that stripping removes, the confirmation
warns when both are on.

**File names that give something away.** A phone's file name often holds the capture time (`IMG_20240512_143022`,
`PXL_…`) or names the device or app (`DJI_`, `GOPR`, `WhatsApp Image …`, `Screenshot`); Scrambled Exif's users
raised it (issue #25). When stripping is on and renaming is off, Seçilenler counts such names in its bottom line
(the tooltip of each row says why) and the confirmation warns, suggesting "Sırala ve yeniden adlandır".

**Shrinking photos ("Fotoğrafları küçült").** Smaller files for sharing are Scrambled Exif's most-voted request
(issue #11) and an open PowerToys request ("cap the longest side"). The long edge is limited to a chosen size
(1080 to 3840 pixels, 2048 by default); smaller photos keep their usual path, so a JPG within the limit stays
untouched, and nothing is enlarged. Resizing happens after the orientation and the color conversion, with
Lanczos; palette and 1-bit images are converted first (Pillow would otherwise fall back to nearest neighbour), and
JPEG's fast reduced decoding is used when nothing is cropped or straightened. A JPG is re-encoded with the
source's own tables; a RAW can be shrunk only while it is converted to JPG. With the metadata kept, the EXIF
dimensions follow the new size.

**Finding duplicates ("Kopyaları bul").** Merged backups and Takeout exports repeat files, and a cleaned copy
next to its original defeats a byte comparison. Fotodit groups two kinds: identical files, and the same picture
with different metadata, found by comparing the image data that remains after stripping (for JPEG, the
compressed data from Fotodit's own parser, so a losslessly cleaned copy matches its original exactly). Only files
whose image data starts the same are read in full: 404 JPEGs (350 MB) take 0.21 s once Windows has them in its
cache, about 4 s the first time. The first file of a group stays; the others are marked and highlighted so that
"Listeden çıkar" (remove from the list) drops them. Nothing is deleted. Near-duplicates (bursts, resized or
recompressed copies) are not looked for: bursts look alike, and an automatic choice would throw away good shots.

**Removing a video's sound ("Sesi kaldır").** A standard control in phone and system editors (Google Photos'
mute, Clipchamp), and background voices are personal data too. The audio streams are simply not copied, so the
video stays a lossless copy; it works in every container, and verification checks that no audio is left.

**Horizontal flip ("Yatay çevir").** Requested for selfies with reversed text (Google Photos added it in 2024;
LosslessCut's users ask for it). The flip is the first step of an edit, before the quarter turns, the straightening
and the crop. Flipping a result that is already rotated or cropped is again such an edit: flipping after a crop
equals cropping the mirrored box out of the flipped image, and flipping after a rotation by θ equals rotating by
−θ after the flip, so the quarter turns and the tilt change sign and the crop box is mirrored. A JPEG keeps its
own tables and its crop stays aligned to the block grid (a flipped crop measured 43.8 dB against the source,
36.1 dB unaligned). A video is always re-encoded with FFmpeg's `hflip`: a mirrored MP4 display matrix would be
lossless, but players handle it unevenly.

**Before and after.** Microsoft Photos, Apple Photos and Google Photos all show the original while a key or the
image is held. In the large preview, holding O (or the compare button) shows the file as it is, without the
rotation, crop or exposure.

---

## 7. Technology choice

| Option | Assessment |
|---|---|
| **Python 3.14 + Pillow 12.3 + pillow-heif 1.8 + rawpy 0.27 (LibRaw 0.22) + PyAV 18.1 (FFmpeg 8.1) + PySide6 (Qt 6.11, including Multimedia) + PyInstaller 6.22** ✅ | Pillow offers JPEG (libjpeg-turbo), PNG, WebP, **AVIF**, TIFF and **LittleCMS** color management in a single package. pillow-heif provides HEIC reading/writing, rawpy/LibRaw RAW decoding, PyAV video reading/writing. Qt provides a fast thumbnail grid, video playback and a native Windows look. (The project started on Python 3.13; it moved to 3.14 when 3.13 was removed from the computer.) |
| C# / .NET + Magick.NET | Powerful, but the .NET SDK was not installed; ImageMagick's metadata behavior is harder to control. |
| Tkinter | The package would be small, but a grid with thousands of thumbnails and a modern look are hard to achieve. |
| Electron | 150 MB+ package, unnecessary bulk. |

**Packaging:** The folder distribution (`dist\Fotodit\Fotodit.exe`, 235 MB) starts in ~1 s. The single file
(`Fotodit.exe`, 96 MB) extracts itself to a temporary folder on every start, so it starts a few seconds later and is
more prone to antivirus false positives; the folder distribution is therefore recommended. Unused Qt components (the
20 MB software OpenGL, network/TLS plugins, OpenSSL, translations into other languages) are left out of the package.
RAW support adds ~30 MB; most of this is the OpenBLAS library that the core of numpy (a rawpy dependency) links
against (it cannot be removed). Video support adds ~110 MB: PyAV's FFmpeg and its encoders (63 MB; none of them can be
removed because FFmpeg links against them) plus Qt Multimedia and its own FFmpeg (~40 MB).
`Fotodit.exe --self-test report.txt` verifies, in the packaged application, the 8 formats, both RAW paths, the
exposure of a JPEG, keeping Display P3, the horizontal flip, keeping the capture date, the size limit and, for
video, metadata removal, conversion to MP4, the thumbnail, a lossless trim, removing a part, Qt's playback plugin
and AV1 playback with the fallback player; it checks that the license files are in the bundle and reports the GPU
encoder it found.

**Licenses:** see §11. In short: the release bundles GPL libraries (x264, x265), so Fotodit is GPL-3.0-or-later.

---

## 8. Performance (on this computer: Intel i7-4700HQ, 16 GB)

12 MP (4032×3024, iPhone size) photos, 4 threads, including verification:

| Operation | Per photo |
|---|---|
| JPEG lossless cleaning | 0.14 s |
| JPEG rotation + cleaning | 0.19 s |
| HEIC → JPG + cleaning | 0.61 s |
| Cleaning HEIC as HEIC | 4.6 s (with the x265 "medium" preset; it was 10.6 s with the default) |
| RAW (24 MP) → JPG, "Kamera görünümü" (camera view) | 0.7 s |
| RAW (24 MP) → JPG, "RAW verisinden işle" (process from RAW data) | 2.4 s |

Example: converting 500 phone photos to JPG and cleaning them takes about 5 minutes. Video measurements are in §6e.

---

## 9. Testing and verification summary

* **678 automated tests:** 8 formats × cleaning/conversion; both RAW paths × 8 orientations; RAW detection
  (9 signatures + TIFF-based formats), CR3/RAF metadata structures; all orientation values 1–8; P3/Adobe RGB/CMYK/
  nclx/R03 color conversions; progressive JPEG, restart markers, 4:2:2/4:4:4; APNG, animated WebP/GIF, multi-page TIFF;
  aCropalypse-style hidden PNG data; corrupt/truncated files; misleading extensions;
  sorting/naming/collisions/two-phase renaming; cancellation; read-only files; timestamps.
  Folder selection: scan order and skip rules (hidden, ".", "$", junctions), the count limit and cancellation, keeping
  the folder structure (nested roots, folders with the same name), per-folder numbering when a parent folder is
  selected with "orijinallere uygula" (apply to originals), the plan staying linear-time with 20,000 photos,
  select/deselect states (removing a subfolder while scanning, stopping, confirmation for large selections), clicking
  the tile circle, the tree checkbox, end-to-end processing in the main window, per-file error isolation in the
  batched Recycle Bin move. Large preview: orientation (1–8), P3/CMYK color accuracy, size limit, transparency, RAW
  preview, navigation, removing from the selection and putting back in the old place/with the old root, 100%, opening
  from "Gözat" (Browse) and "Seçilenler" (Selected) (a double-click does not change the selection), post-processing
  names refreshed after a change in the preview.
  Rotation/cropping: direction and losslessness of 90° rotations, the crop changing with the rotation, the largest box
  (side/√2 at 45°), no empty corners when straightening and a tilted horizon becoming level, 7 pixel modes,
  premultiplied transparency, removing the EXIF thumbnail and updating the dimensions (in both byte orders), reuse of
  the tables and block-grid-aligned cropping for JPEG, editing with metadata kept (checked with exiv2), 8 formats and
  both RAW paths, every frame of animated files, the plan (RAW skipped) and execution (new folder / apply to
  originals); in the UI, the edit store, thumbnails, a random 1,200-step test of the crop tool, rotate/undo/crop mode
  in the preview (mouse dragging, aspect ratio, straightening, Enter/Esc/double-click), bulk rotation in
  "Seçilenler", the "Gözat" context menu and the warning on closing. The outputs are verified independently with
  exiv2.
  Videos: recognition of 8 containers; SEI parsing/cleaning (emulation prevention bytes, keeping other SEI types),
  MDPM date/time zone/GPS/manufacturer, removing EXIF from MJPEG frames; iPhone (date with time zone, location,
  device, portrait orientation), Android (UTC → local time from the file name), the GoPro rule, MDPM in MTS, MKV
  orientation, HDR, data/subtitle tracks; metadata removal keeping the frames identical while removing Apple keys,
  location, the x264 string, MDPM, GoPro telemetry, subtitles and chapters; conversion with metadata kept;
  HEVC → H.264 (orientation applied to the pixels), `hvc1` when keeping the codec, MP4 from MJPEG/WMV/VP9/3GP, HDR
  tone mapping (reference white), lossless 90° rotation and its addition to the existing orientation, cropping and
  straightening (even dimensions, no empty corners), the editing rule for MKV, progress and cancellation, corrupt
  files; in the plan, separate naming, the `.mp4` extension, grouping by month, in-place processing, copying files that
  are already MP4; in the UI, the options, tiles (▶ duration), "Seçilenler" notes, playback in the preview (frame
  content and orientation), rotation, pausing in crop mode. Added later: undated files and ties keeping their
  selection order (ascending/descending, manually changed order, by size, files whose info has not been read yet),
  the undated-file note in "Seçilenler"; photo/video counts on folder tiles (including subfolders and subfolders whose
  count is already known); detection of subtitle log tracks (DJI's two formats, dashcams, "GPS" appearing in a movie
  subtitle), keeping real subtitles with their language when removing metadata while deleting the log track,
  converting SRT/ASS in MKV to MP4 subtitles (in both modes); falling back to the CPU when the GPU encoder cannot be
  opened or produces broken output, the first-frame comparison, preserving HDR with HEVC, encoding with the real
  NVENC on this computer, saving the option and showing the encoder found; AV1 in the fallback player (first frame,
  portrait orientation, playback, seeking while paused, end of file, releasing the file) and switching to the
  fallback automatically when Qt cannot decode.
  Version 1.1: the exposure model and its texts; 0 EV matching LibRaw's development, ±1 EV, the curve, the preview
  development, the exposure applied when converting (also with the camera JPEG available and together with a
  rotation), RAW exposure needing JPG conversion, exposure-only edits leaving other photos untouched, thumbnails;
  trimming without re-encoding, with B-frames and a keyframe every 12 frames, each output frame identified by its
  content (start in the middle of a GOP, start only, end only, a late start reached by seeking), frame-exact
  trimming when re-encoding and sample-exact AAC from PCM, subtitles and chapters cut to the window, tags kept, a
  MOV staying MOV, the plan, an empty window; in the UI the exposure slider (live preview from the RAW data, crop
  keeping the exposure, undo, reset, no slider for other files), the trim buttons (end before start, undo, removing
  the trim) and switching the theme (palette, style sheet, cached colors, the remembered setting).
  Version 1.2: the exposure curve and every pixel mode (alpha, palettes with transparency, CMYK, 16-bit), the
  exposure on 7 formats (brighter and darker, clean output, with a rotation, when converting to JPG), the preview and
  the thumbnails of a JPEG with an exposure; recognizing the standard wide profiles (P3 as an ICC profile and as
  nclx, Adobe RGB, ProPhoto; sRGB, CMYK, a linear P3 and a broken profile not), keeping them on 5 formats and when
  converting to JPG (clean profile only, colors unconverted, a JPEG still lossless), the keep-option in the panel;
  the removed-parts model (merging, segments, texts, tidying), the time mapping across removed parts, removing parts
  frame-exactly with their audio, subtitles and chapters, a coarse time base, the plan (edge parts re-encoded too,
  nothing left), the "Bölüm çıkar" button (marking forwards and backwards, Esc, undo, a trim start inside a removed
  part); the license files (the GPL text, every text named in the notices), the spec's bundle list, the release zips
  (contents, checksums, refusing a build without a license text) and the About window.
  Version 1.3: keeping the capture date on every format (the one-field EXIF block, checked with exiv2; the six
  MP4/MOV header times; the file date; verification refusing a second EXIF block, another field, a wrong date or
  extra metadata), the three name formats (same-second suffixes, RAW + JPEG pairs, undated files, a 20,000-file
  plan), the file-name check, the size limit on every format and pixel mode (never enlarging, multi-page TIFF,
  16-bit, palettes, RAW only when converting), duplicates (identical files, a cleaned copy and its original, the
  same file under two spellings, cancelling), removing the sound (stream copy, every container, verification), the
  horizontal flip (450 random edits compared pixel by pixel with flipping the result, every photo path, RAW, the
  block grid, video with and without a quarter turn) and, in the UI, the flip in the preview, in crop mode, in
  "Seçilenler" and "Gözat", the folder tile's circle and the theme colors. The redesign added the double-click into
  folders, the path bar (its steps, typing a path, a path that does not exist), the glass looks, tabular digits only
  where numbers change, and the operations panel fitting its narrowest width.
  A code review of 1.3, file by file (the core, the metadata readers, the video code, the main window and the
  preview) and then in the narrowest window in both languages, found 68 defects; each is fixed with a test that
  fails without its fix (most in `tests/*/test_review_*.py` and `tests/ui/test_narrow_ui.py`). Among them: a damaged
  TIFF that kept the info panel busy for hours, data hidden inside the frames of an animated WebP or behind a kept
  chunk surviving cleaning, PNG and TIFF reads that could take gigabytes of memory, a RAW of a camera LibRaw does
  not know being skipped even for renaming, folders left behind by a cancelled job, a video whose extra audio track
  FFmpeg cannot decode (iPhone's spatial audio) failing to process, mirrored videos shown upside down, and the mouse
  wheel changing a setting while the panel scrolled.
* **UI smoke test** (`tests/ui/smoke_run.py`, light or `--dark`, Turkish or `--en`): opens the window, navigates,
  tries "Tümünü seç" (select all) with subfolders and folder selection via the tile circle and the tree checkbox,
  opens and navigates the large preview, crops a photo with a tilted horizon to 4:3 and straightens it, rotates a
  photo, sets +1 EV on a RAW, trims a video and removes a part of another, renames and processes while keeping the
  folder structure (checking the dimensions of the edited outputs and the trimmed video's duration), tries
  "orijinallere uygula" mode, and takes screenshots of every screen, the confirmation and the results.
* **Package self-test** (`--self-test`): verifies that all decoders in the exe, the RAW and JPEG exposure, keeping
  Display P3, FFmpeg/x264, a lossless trim (edit lists), removing a part, Qt's video playback plugin and the fallback
  player (AV1) work and that the license files are bundled; reports the GPU encoder.
* **The real exe** is driven with UI Automation (without using the mouse/keyboard, with a separate data folder): video
  tiles in "Gözat", "Seçilenler" notes, playback in the preview, rotation during playback, crop mode for video, and
  end-to-end processing from the UI (output to a separate working folder; H.264, no location, 1172×660).

---

## 10. Known limitations and roadmap

* **RAW:** cleaned only by converting to JPG (see §4b). When processing from RAW data, camera styles and lens
  corrections are not applied. The separate **.xmp sidecar files** that programs such as Lightroom/Capture One write
  next to the RAW file (edits, keywords, location) are separate files and are therefore not processed; delete them
  separately before sharing. New cameras that LibRaw does not recognize are reported as "desteklenmiyor" (not
  supported) when their RAW files are to be converted; copying, renaming and grouping them still work.
* **JPEG XL, PSD** are not supported.
* Cleaning HEIC/AVIF in their own format requires recompression (converting to JPG is recommended).
* Color conversion is done at 8 bits; HDR gain maps are not carried over to the output (the SDR image is complete).
* In "Orijinallere uygula" (apply to originals) mode the originals remain in the Recycle Bin; for the metadata to be
  gone from the disk as well, the Recycle Bin must be emptied (the application reminds you of this on the results
  screen).
* A Live Photo's separate video file (MOV) can now be selected and processed as a video (it is not paired with the
  photo).
* **Videos:** HEVC is encoded only if the GPU has a 10-bit HEVC encoder (this computer's does not); otherwise
  cropped/straightened HDR videos become SDR H.264. Detecting subtitle log tracks is a heuristic: a mixed track in
  which fewer than half of the lines contain location/camera information is kept (but the verification re-examines
  the lines in the output). PGS (Blu-ray) subtitles cannot be carried over to MP4. The fallback player (AV1) does not
  tone-map HDR. 4K HDR plays at ~17 fps in the preview.
* **Editing:** straightening requires resampling (bicubic; ~39 dB for JPEG). To preserve quality, JPEG crops are
  aligned to the block grid, so the output may be shifted by up to 15 pixels relative to the box in the preview.
  For JPEGs whose dimensions are not multiples of the block size (e.g. a height of 3000 pixels with 4:2:0), the block
  grid shifts when they are rotated by 90°; the quality is still high, but not "nearly lossless". Edits are lost when
  the application closes (if they have not been processed, the user is asked when closing). There is no color
  adjustment; the RAW exposure gives the neutral LibRaw development, without the camera's picture style.
  A trimmed copy relies on the MP4 edit list and keeps the frames since the keyframe before its start hidden in the
  file (see §6e); removing a part instead re-encodes.
* **Kept capture date:** only the date and time, without the time zone; a video's header time is written in UTC
  using this computer's time zone, so a video shot in another time zone may show a shifted time. GIF, BMP and
  videos other than MP4/MOV/3GP keep the date only as the file date, which copying or zipping may lose.
* **Duplicates** are only identical pictures: resized or recompressed copies and similar shots are not found.
* **Glass** is drawn on the CPU; a large capsule over a changing backdrop (a playing video) costs a few
  milliseconds per frame. "Saydamlığı azalt" turns it into a solid surface.
* **Folder selection is a snapshot:** photos added to a folder after it was selected are not included until the
  folder is selected again. Photos that are "online-only" in OneDrive are downloaded to the computer when they are
  selected, while their info is being read (this is needed for processing anyway).
* Possible additions: code signing (distribution without the SmartScreen warning, see §11), a lossless trim
  without hidden frames (re-encoding only the first keyframe interval and copying the rest), more languages for the
  user interface.

---

## 11. Open source and distribution

**License: GPL-3.0-or-later.** The license is not a free choice here. The PyAV wheel's FFmpeg is built under the
LGPL 3 (`--enable-version3`), but it is linked with **x264 and x265 (GPL 2 or later)** and with **opencore-amr
(Apache 2.0)**; pillow-heif's wheel also bundles x265. Distributing those binaries together with Fotodit makes one
combined work under the GPL, and Apache-2.0 code is compatible only with version 3 of the GPL: so the release, and
Fotodit's own code, are licensed under the **GPL, version 3 or later** (copyright © 2026 Artapa). The other
components are compatible with that: Qt/PySide6 (LGPL 3), Qt's own FFmpeg (LGPL 2.1+), LibRaw (LGPL 2.1), libheif and
libde265 (LGPL 3), BSD/MIT codecs, Python (PSF), the GCC runtime (GPL 3 with the runtime exception), PyInstaller's
bootloader (GPL 2+ with an exception for any program). Building without the GPL codecs (an LGPL release) would mean
building FFmpeg ourselves and losing H.264/HEVC encoding.

**Notices.** `THIRD-PARTY-NOTICES.md` lists every bundled component with its version, license, the place of its
license text and its source; `licenses/` holds the texts (the GNU texts byte-identical to the FSF's, the BSD/MIT
notices downloaded from each project's repository at the bundled version); the license files the Python packages
ship are copied during the build. All of it is inside the exe as well (the bundle's "legal" folder), where the
**About window** ("Hakkında", the version button at the bottom of the left panel, or F1) shows the copyright, the GPL notice without
warranty, the license and the notices: the "Appropriate Legal Notices" the GPL asks an interactive program to show.

**Source.** Each release is packed by `packaging/package_release.py`: the two program zips (with LICENSE.txt,
THIRD-PARTY-NOTICES.txt and the licenses next to the exe), a source zip of Fotodit (its Corresponding Source) and
SHA-256 checksums; it refuses to pack a build that lacks a license text named in the notices. The third-party
sources are named in the notices (upstream releases at the bundled versions; FFmpeg and its codecs through PyAV's
build scripts); attaching copies of those archives to each release is the safest way to keep them available.

**Signing.** An unsigned exe makes SmartScreen warn until the file earns a reputation. `packaging\build.ps1 -Sign` signs with a
certificate from the user's certificate store (signtool, SHA-256, RFC 3161 timestamp) and self-tests the signed exe;
for an open-source project the SignPath Foundation offers free signing from a CI build. No certificate was available,
so this path is untested.

---

## 12. References

**Format specifications and reference tools**
* ExifTool tag tables and FAQ: <https://exiftool.org/TagNames/JPEG.html>, <https://exiftool.org/TagNames/EXIF.html>,
  <https://exiftool.org/TagNames/GPS.html>, <https://exiftool.org/TagNames/Apple.html>, <https://exiftool.org/TagNames/Samsung.html>,
  <https://exiftool.org/TagNames/MPF.html>, <https://exiftool.org/TagNames/PNG.html>, <https://exiftool.org/TagNames/XMP.html>,
  <https://exiftool.org/faq.html>
* W3C PNG, Third Edition: <https://www.w3.org/TR/png-3/>
* WebP RIFF container: <https://developers.google.com/speed/webp/docs/riff_container>
* Ultra HDR: <https://developer.android.com/media/platform/hdr-image-format> · Motion photo:
  <https://developer.android.com/media/platform/motion-photo-format>
* C2PA 2.2: <https://spec.c2pa.org/specifications/specifications/2.2/specs/C2PA_Specification.html>
* XMP Specification Part 3: <https://github.com/adobe/XMP-Toolkit-SDK/blob/main/docs/XMPSpecificationPart3.pdf>
* BMP V5 header: <https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-bitmapv5header>
* IPTC digital source type: <https://cv.iptc.org/newscodes/digitalsourcetype/>
* LibRaw documentation: <https://www.libraw.org/docs> · rawpy: <https://letmaik.github.io/rawpy/> ·
  Adobe DNG specification: <https://helpx.adobe.com/camera-raw/digital-negative.html>
* ExifTool RAW tags: <https://exiftool.org/TagNames/DNG.html>, <https://exiftool.org/TagNames/Canon.html>,
  <https://exiftool.org/TagNames/FujiFilm.html>

**Video**
* ExifTool QuickTime tags (timed GPS tracks, QuickTimeUTC): <https://exiftool.org/TagNames/QuickTime.html> ·
  H264/MDPM: <https://github.com/exiftool/exiftool/blob/master/lib/Image/ExifTool/H264.pm>
* Apple location metadata: <https://developer.apple.com/documentation/quicktime-file-format/location_metadata>
* Location in iOS/Android videos: <https://blog.addpipe.com/geolocation-metadata-ios-android-video-files/>
* What metadata removal with ffmpeg leaves behind: <https://aimetadatacleaner.com/blog/ffmpeg-exiftool-remove-metadata-what-they-miss>
* AVCHD: <https://en.wikipedia.org/wiki/AVCHD> · GoPro and UTC:
  <https://community.gopro.com/s/question/0D53b00008BtZOLCA3/storing-local-time-rather-than-utc-in-video-exif-data>
* PyAV's FFmpeg builds: <https://github.com/PyAV-Org/pyav-ffmpeg>
* Qt Multimedia FFmpeg configuration: <https://doc.qt.io/qt-6/advanced-ffmpeg-configuration.html> ·
  QVideoFrame: <https://doc.qt.io/qt-6/qvideoframe.html>
* ITU-R BT.2100 (HLG/PQ), BT.2408 (HDR reference white 203 cd/m²), BT.2087 (BT.2020 → BT.709)

**Design (Liquid Glass)**
* Apple Human Interface Guidelines: <https://developer.apple.com/design/human-interface-guidelines/materials>,
  <https://developer.apple.com/design/human-interface-guidelines/toolbars>,
  <https://developer.apple.com/design/human-interface-guidelines/sidebars>,
  <https://developer.apple.com/design/human-interface-guidelines/sheets>,
  <https://developer.apple.com/design/human-interface-guidelines/color>,
  <https://developer.apple.com/design/human-interface-guidelines/accessibility>
* Liquid Glass overview and adoption: <https://developer.apple.com/documentation/technologyoverviews/liquid-glass>,
  <https://developer.apple.com/documentation/technologyoverviews/adopting-liquid-glass>
* WWDC25 "Meet Liquid Glass" <https://developer.apple.com/videos/play/wwdc2025/219/>, "Get to know the new design
  system" <https://developer.apple.com/videos/play/wwdc2025/356/>, "Build an AppKit app with the new design"
  <https://developer.apple.com/videos/play/wwdc2025/310/>; the 2026 revision:
  <https://developer.apple.com/videos/play/wwdc2026/101/>, <https://developer.apple.com/videos/play/wwdc2026/102/>,
  <https://developer.apple.com/videos/play/wwdc2026/269/>
* iOS 27's glass measured on a device and in the simulator:
  <https://github.com/shayann07/liquidglass/blob/main/docs/research/ios27-measurements-raw.md>,
  <https://github.com/shayann07/liquidglass/blob/main/docs/research/measured-model.md>; Apple's iOS 27 design kit
  as transcribed tokens: <https://github.com/seunghan91/ios27-design-system>; what changed in iOS 27:
  <https://www.macrumors.com/2026/06/10/how-liquid-glass-is-changing-in-ios-27/>,
  <https://www.ryanthomson.net/articles/liquid-glass-ios-27-first-impressions>
* The looks: <https://www.macrumors.com/2025/10/20/ios-26-1-liquid-glass-toggle/>,
  <https://9to5mac.com/2026/06/12/ios-27-fixes-liquid-glass-and-not-just-with-a-slider/>
* Springs: <https://developer.apple.com/videos/play/wwdc2023/10158/>,
  <https://developer.apple.com/documentation/swiftui/spring/init(duration:bounce:)>
* The optics (convex squircle rim, Snell's law, index 1.5): <https://kube.io/blog/liquid-glass-css-svg/>,
  <https://github.com/dashersw/liquid-glass-js>
* The typeface: Inter <https://rsms.me/inter>, <https://github.com/rsms/inter/releases/tag/v4.1>; Qt's hinting
  preference <https://doc.qt.io/qt-6/qfont.html#HintingPreference-enum>; the variable-font bug
  <https://qt-project.atlassian.net/browse/QTBUG-149199>
* The icons: Hugeicons <https://hugeicons.com>, the free set's package
  <https://www.npmjs.com/package/@hugeicons/core-free-icons/v/4.3.5>; Animate UI's icons and their license
  <https://animate-ui.com/docs/icons/hugeicons>
* Restarting a PyInstaller app as an independent instance:
  <https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html>

**User requests behind the 1.3 features**
* ExifCleaner, keeping dates: <https://github.com/szTheory/exifcleaner/issues/250> · Scrambled Exif, smaller files
  and file names: <https://gitlab.com/juanitobananas/scrambled-exif/-/issues/11>,
  <https://gitlab.com/juanitobananas/scrambled-exif/-/issues/25>
* PowerToys, capping the longest side and capture-date names: <https://github.com/microsoft/PowerToys/issues/36702>,
  <https://learn.microsoft.com/en-us/windows/powertoys/powerrename>
* Duplicates after migrating backups: <https://ente.com/help/photos/faq/migration>
* Flipping video: <https://github.com/mifi/lossless-cut/discussions/2186>, mirrored matrices:
  <https://github.com/mifi/lossless-cut/issues/643>

**Security and forensics**
* CVE-2023-21036: <https://nvd.nist.gov/vuln/detail/CVE-2023-21036> · CVE-2023-28303: <https://nvd.nist.gov/vuln/detail/CVE-2023-28303>
* Kee, Johnson, Farid — JPEG signatures: <https://people.csail.mit.edu/kimo/publications/jpeg/tifs11a.pdf>
* Lukáš, Fridrich, Goljan — sensor noise: <https://dl.acm.org/doi/10.1109/TIFS.2006.873602>
* Zone.Identifier on downloaded files: <https://textslashplain.com/2016/04/04/downloads-and-the-mark-of-the-web/>

**Libraries**
* Pillow formats: <https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html> ·
  16-bit clipping issue: <https://github.com/python-pillow/Pillow/issues/3011> ·
  RAW preview issue: <https://github.com/python-pillow/Pillow/issues/6482>
* pillow-heif orientation behavior: <https://pillow-heif.readthedocs.io/en/latest/workaround-orientation.html>
* QFile::moveToTrash: <https://doc.qt.io/qt-6/qfile.html> · SHFILEOPSTRUCTW:
  <https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-shfileopstructw>
* PyInstaller operating mode: <https://pyinstaller.org/en/stable/operating-mode.html> ·
  Qt supported platforms: <https://doc.qt.io/qt-6/supported-platforms.html>
