# Third-party notices

Clipboard+ itself is MIT licensed (see `LICENSE`). The packaged installers
(DMG, Windows setup, AppImage) also ship the components below, each under its
own license.

## whisper.cpp (MIT)

Built from https://github.com/ggml-org/whisper.cpp at the release pinned in
`.github/workflows/release.yml` (`WHISPER_VERSION` / `WHISPER_COMMIT`); the
`whisper-cli` and `whisper-server` binaries are statically linked against ggml.

    MIT License
    Copyright (c) 2023-2026 The ggml authors
    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the "Software"),
    to deal in the Software without restriction, including without limitation
    the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following conditions: the
    above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED
    "AS IS", WITHOUT WARRANTY OF ANY KIND. See
    https://github.com/ggml-org/whisper.cpp/blob/master/LICENSE for the full text.

Whisper model weights (downloaded on first run, not bundled) are released by
OpenAI under the MIT license.

## FFmpeg (GPL)

The installers bundle an unmodified `ffmpeg` executable:

| Platform | Build | Upstream |
|---|---|---|
| Linux x86_64 | static build 7.0.2 | https://johnvansickle.com/ffmpeg/ |
| Windows x86_64 | gyan.dev "essentials" 9.0.2 | https://www.gyan.dev/ffmpeg/builds/ |
| macOS | Homebrew `ffmpeg` bottle plus its dylibs (dylibbundler) | https://formulae.brew.sh/formula/ffmpeg |

These builds are compiled with GPL-licensed components, so the ffmpeg binary
is distributed under the **GNU General Public License, version 2 or later**
(https://www.gnu.org/licenses/old-licenses/gpl-2.0.html). Clipboard+ runs it
as a separate process and does not link against it.

**Source offer.** The complete corresponding source for the bundled FFmpeg is
available from the FFmpeg project at https://ffmpeg.org/releases/ and, with
the exact build scripts and configuration used, from the upstream builders
listed above (Linux: https://johnvansickle.com/ffmpeg/ , Windows:
https://github.com/GyanD/codexffmpeg , macOS: https://github.com/Homebrew/homebrew-core).
On request to the maintainer (open an issue at
https://github.com/tommyqhoang/ClipboardPlus-and-Dictation/issues), we will
also provide the source for at least three years after the release you
downloaded. You may replace the bundled `ffmpeg` with your own build; Clipboard+
uses whichever `ffmpeg` sits next to its executables.

## Python runtime and libraries

- Python (PSF License) and PyInstaller (GPL-2.0 with a bootloader exception
  that permits bundling in non-GPL applications).
- pystray (LGPL-3.0) and python-xlib (LGPL-2.1+): used unmodified as
  importable Python packages; replaceable by editing the `_internal` folder
  of an installed onedir bundle.
- Pillow (MIT-CMU / HPND) and PyObjC (MIT).
- Sun Valley ttk theme (sv-ttk, MIT, Copyright (c) rdbende,
  https://github.com/rdbende/Sun-Valley-ttk-theme): the window's rounded widgets.
  Its sprite sheet and theme script are recolored to Clipboard+'s amber and shipped as
  `clipboardplus-theme.tcl` / `clipboardplus-theme.png` (built by `tools/build_theme.py`).
  The MIT license text follows.

  > MIT License. Copyright (c) rdbende. Permission is hereby granted, free of charge, to
  > any person obtaining a copy of this software and associated documentation files (the
  > "Software"), to deal in the Software without restriction, including without limitation
  > the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  > copies of the Software, and to permit persons to whom the Software is furnished to do
  > so, subject to the following conditions: the above copyright notice and this permission
  > notice shall be included in all copies or substantial portions of the Software. THE
  > SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
  > INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
  > PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
  > HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
  > CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE
  > OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
