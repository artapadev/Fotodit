# Third-party components

Fotodit is free software: you can redistribute it and/or modify it under the terms of the GNU General
Public License, version 3 or (at your option) any later version. Its full text is in `LICENSE`.
Copyright (C) 2026 Artapa.

Fotodit is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

The Windows release (`Fotodit.exe` and its `_internal` folder) also contains the components below, each
under its own license. Their license texts are in the `licenses` folder: in the release, next to
`Fotodit.exe`; in the source code, at the top of the project. The texts that the Python packages ship
themselves are copied there during the build, into `licenses\python-packages\`. Every component is used
unmodified, as its project publishes it (the Python packages come from PyPI, at the versions pinned in
`requirements.txt`).

Because the release contains libraries under the GPL (x264, x265, and the FFmpeg build linked with
them), the release as a whole is distributed under the GPL, version 3.


## Getting the source code

- Fotodit: the source archive published with every release (`Fotodit-<version>-source.zip`), or the
  project's repository.
- The Python packages: their source distributions on PyPI (`https://pypi.org/project/<name>/<version>/`)
  and the project pages listed below.
- FFmpeg and the codec libraries in `_internal\av.libs`: built from their upstream sources by the PyAV
  project's build scripts, https://github.com/PyAV-Org/pyav-ffmpeg (the build for FFmpeg 8.1.2); each
  library's own source is listed below.
- If a source listed here stops being available, ask in Fotodit's repository and a copy of that exact
  source will be provided.


## Components

### Python 3.14.7
- License: PSF License Version 2, and the licenses of the parts Python's Windows build bundles (bzip2,
  libffi, Zstandard and others), all in `licenses\Python.txt`
- https://www.python.org

### Qt 6.12.0, PySide6 6.12.0 and Shiboken6 6.12.0 (the user interface)
- License: LGPL-3.0-only: `licenses\LGPL-3.0.txt` (it builds on the GPL, version 3: `LICENSE`)
- Qt contains third-party code of its own; Qt lists it at
  https://doc.qt.io/qt-6.12/licenses-used-in-qt.html
- Source: https://download.qt.io/official_releases/qt/6.12/6.12.0/ and
  https://code.qt.io/cgit/pyside/pyside-setup.git/
- The Qt libraries are separate DLLs (`_internal\PySide6\Qt6*.dll`) that can be replaced with
  compatible builds; the single-file version can be rebuilt from Fotodit's source with any compatible Qt.

### FFmpeg 9.0.1 inside Qt Multimedia (video playback)
- Files: `_internal\PySide6\avcodec-63.dll`, `avformat-63.dll`, `avutil-61.dll`, `swresample-7.dll`,
  `swscale-10.dll`
- License: LGPL-2.1-or-later: `licenses\LGPL-2.1.txt`
- https://ffmpeg.org

### PyAV 18.1.0 (reading and writing videos)
- License: BSD-3-Clause: `licenses\python-packages\av\LICENSE.txt`
- https://github.com/PyAV-Org/PyAV

### FFmpeg 8.1.2 and the libraries it uses (`_internal\av.libs`)
- FFmpeg is built under the LGPL-3.0-or-later; linked with x264 and x265, it is distributed under the
  GPL-3.0-or-later: `LICENSE`, `licenses\LGPL-3.0.txt`. Source: https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz
- x264 (H.264 encoder): GPL-2.0-or-later, `licenses\GPL-2.0.txt`. https://code.videolan.org/videolan/x264
- x265 4.2 (HEVC encoder): GPL-2.0-or-later, `licenses\GPL-2.0.txt`. https://bitbucket.org/multicoreware/x265_git
- dav1d 1.5.3 (AV1 decoder): BSD-2-Clause, `licenses\dav1d.txt`. https://code.videolan.org/videolan/dav1d
- SVT-AV1 4.1.0 (AV1 encoder): BSD-3-Clause-Clear and the Alliance for Open Media Patent License 1.0,
  `licenses\SVT-AV1.txt`. https://gitlab.com/AOMediaCodec/SVT-AV1
- libvpx 1.16.0 (VP8/VP9): BSD-3-Clause and a patent grant, `licenses\libvpx.txt`.
  https://chromium.googlesource.com/webm/libvpx
- Opus 1.6.1: BSD-3-Clause, `licenses\opus.txt`. https://opus-codec.org
- LAME 3.100 (MP3 encoder): LGPL-2.0-or-later, used under version 2.1, `licenses\LGPL-2.1.txt`.
  https://lame.sourceforge.io
- opencore-amr (AMR-NB and AMR-WB): Apache-2.0, `licenses\Apache-2.0.txt` and
  `licenses\opencore-amr-NOTICE.txt`. https://sourceforge.net/projects/opencore-amr/
- libwebp, libwebpmux and libsharpyuv: BSD-3-Clause and a patent grant, `licenses\libwebp.txt`.
  https://chromium.googlesource.com/webm/libwebp
- Intel Video Processing Library (libvpl): MIT, `licenses\libvpl.txt`. https://github.com/intel/libvpl
- NVIDIA codec headers (compiled into FFmpeg): MIT, `licenses\nv-codec-headers.txt`.
  https://github.com/FFmpeg/nv-codec-headers
- AMD AMF headers (compiled into FFmpeg): MIT, `licenses\AMF.txt`.
  https://github.com/GPUOpen-LibrariesAndSDKs/AMF
- zlib 1.3.2: Zlib license, `licenses\zlib.txt`. https://zlib.net
- GNU libiconv: LGPL-2.1-or-later, `licenses\LGPL-2.1.txt`. https://www.gnu.org/software/libiconv/
- GCC runtime (`libgcc_s_seh-1.dll`, `libstdc++-6.dll`): GPL-3.0-or-later with the GCC Runtime Library
  Exception 3.1, `LICENSE` and `licenses\GCC-runtime-library-exception-3.1.txt`. https://gcc.gnu.org
- mingw-w64 winpthreads (`libwinpthread-1.dll`): MIT and others, `licenses\mingw-w64-winpthreads.txt`.
  https://www.mingw-w64.org

### Pillow 12.3.0 (images)
- License: MIT-CMU. Pillow bundles brotli, FreeType, HarfBuzz, Little CMS, libavif, libjpeg-turbo,
  libpng, libwebp, OpenJPEG, LibTIFF, XZ and zlib-ng; the same file holds their licenses:
  `licenses\python-packages\pillow\LICENSE`
- https://python-pillow.org

### pillow-heif 1.8.0 (HEIC and HEIF)
- License: BSD-3-Clause: `licenses\python-packages\pillow_heif\`
- It bundles libheif 1.23.4 (LGPL-3.0, `licenses\LGPL-3.0.txt`), libde265 1.1.3 (LGPL-3.0), x265 4.2
  (GPL-2.0-or-later) and the MinGW runtime listed above.
- https://github.com/bigcat88/pillow_heif, https://github.com/strukturag/libheif/tree/v1.23.4,
  https://github.com/strukturag/libde265/tree/v1.1.3

### rawpy 0.27.1 and LibRaw 0.22.1 (RAW photos)
- rawpy: MIT. LibRaw: LGPL-2.1 or CDDL-1.0, used under the LGPL-2.1. Texts:
  `licenses\python-packages\rawpy\`
- https://github.com/letmaik/rawpy, https://www.libraw.org

### NumPy 2.5.3
- License: BSD-3-Clause and others. NumPy bundles OpenBLAS (scipy-openblas) with parts of LAPACK and
  the GCC runtime they need: `licenses\python-packages\numpy\`
- https://numpy.org

### Inter 4.1 (the typeface)
- The hinted static faces Regular, Medium, SemiBold and Bold (`_internal\fotodit\resources\fonts\`),
  unmodified, as the Inter project publishes them.
- License: SIL Open Font License 1.1: `licenses\Inter.txt`. The fonts stay under that license.
- https://rsms.me/inter, source: https://github.com/rsms/inter/releases/tag/v4.1

### Hugeicons free icons 4.3.5 (the icons)
- 48 icons of the free "stroke rounded" set, from the npm package `@hugeicons/core-free-icons` 4.3.5
  (`_internal\fotodit\resources\icons\hugeicons.json`); their vector data is unchanged. Fotodit draws
  them itself and moves them a little under the pointer.
- License: MIT, Copyright (c) 2025 Hugeicons: `licenses\Hugeicons.txt`
- https://hugeicons.com, source: https://www.npmjs.com/package/@hugeicons/core-free-icons/v/4.3.5

### PyInstaller 6.22.3 (starts `Fotodit.exe`)
- The bootloader: GPL-2.0-or-later with the bootloader exception; the run-time hooks: Apache-2.0:
  `licenses\python-packages\pyinstaller\COPYING.txt`
- https://pyinstaller.org

### Microsoft Visual C++ runtime
- `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, `MSVCP140*.dll`, `VCOMP140.DLL`: redistributable files of
  Microsoft Visual Studio, distributed under Microsoft's terms for them.
- https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files
