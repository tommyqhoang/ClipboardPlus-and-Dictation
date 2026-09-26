# Validation evidence

## Automated checks

The cross-platform/installer update is checked with native macOS tests and an
isolated Linux container. The latest verified scope is:

- 67 unit/integration tests, including actual subprocess session control,
  live previews, cancellation, retained audio, clipboard failure recovery,
  HTTP multipart requests, redirect rejection, graphical workflows, and installer
  regression tests.
- 95% combined statement/branch coverage across the seven `lib` runtime modules
  (1,125 statements, 382 branch destinations): graphical app 98%, app service
  95%, dictation 91%, desktop adapters 95%, onboarding 98%, and rewriting/workflow
  100%.
  Coverage includes subprocesses. This is critical-core coverage,
  not shell/PowerShell-installer or whole-repository coverage.
- Ruff lint and formatting; strict mypy; ShellCheck and shfmt;
  Bash syntax and Python bytecode compilation.
- An isolated HTTP-only installation, installed CLI/settings/status invocation,
  and uninstallation that retained settings.
- Desktop installer round trips in temporary directories, launcher construction
  for all three operating systems, verified-download success/failure tests, and
  native/virtual walkthrough tests that verify no recording starts. Setup rejects
  non-GGML model files before recording, and desktop settings do not persist
  temporary environment overrides.
- Unix bootstrap orchestration and cleanup with substituted package/network
  commands. The Linux source fallback is tested for its release/commit pin and the
  Windows bootstrap for its version/digest pin. Actual package-manager installation
  was not performed on the host.
- Actual loopback HTTP requests test concise-draft creation, separate review/copy,
  original preservation, stale-draft rejection, no redirects or automatic retries,
  malformed/truncated responses, remote consent and secret non-persistence.
- The graphical status window is exercised natively on macOS and under a Linux
  virtual display with deterministic state/timer tests. Recovery errors remain
  visible, smaller windows expose scrolling controls, and displayed model text
  has control characters removed.

CI defines Linux/macOS/Windows checks and Windows PowerShell parsing. These local
results are not a completed GitHub Actions run or an actual Windows installation.

## Clipboard history and Clipboard+ sync

Evidence for the clipboard manager and account sync, kept apart because it has
different limits from the dictation evidence above.

- 465 tests pass (2 skipped) under a virtual X server with Python 3.11 and 3.14;
  383 (29 to 30 skipped because they need Tk, an X server or Pillow) pass without a
  display on Python 3.10 and 3.14. `ruff`, `ruff format`, `mypy --strict`, ShellCheck
  and shfmt are clean.
- Combined statement and branch coverage across `lib` is 93% (4,426 statements, 1,304
  branches): sync engine 98%, store 93%, service 94%, account client 94%, Clipboard
  tab and account card 96%, Linux watcher 85%, macOS watcher 88%, **Windows watcher
  62%** (its Win32 calls only run on Windows), tray 84%.
- The Linux watcher runs against a real X server (text, large INCR transfers, images,
  a password-manager copy that is never requested, an owner that disappears
  mid-request, rapid copies). The whole service process is started against a private
  X server: text and an image are stored, a password-manager copy is not, and it quits
  cleanly. The same check was repeated from an installed copy of the application
  (all modules resolve from the install folder, and uninstall leaves nothing behind
  but the kept history).
- On a real GNOME Wayland session the watcher captured text with accents and emoji
  and a PNG image; `wl-paste --watch` was correctly found unsupported there, so the
  X11 path was used.
- The sync engine has 42 offline tests against a real SQLite store and a fake account
  that models the service's duplicate rule (idempotent retries, interruptions, offline
  backoff, refused key, cursor overlap, conflicts, deletions, clear, size limits).
  Deliberate breakages of the engine (no tombstone guard, wrong conflict rule, no
  cursor overlap, unskipped refusals, no content match, no auth stop) are each caught
  by a test.
- The account client and engine were also run end to end against a local copy of the
  real Clipboard+ backend (Postgres in Docker, behind a temporary TLS proxy):
  registration, sign-in errors, key creation and verification, two devices plus an
  extension-style client, duplicate handling, deletions, favorites on and off,
  clear-everywhere and a refused key. That run found and fixed one bug the fakes
  could not (a linked item kept its own time key, so a later remote deletion missed
  it). The client's item key matches the server's own function byte for byte for
  shared fixtures that both repositories test.
- In the `clipboardplus` repository (committed there, not pushed or deployed): 161
  unit tests and 9 Docker-Postgres integration tests pass, including the new
  cross-client duplicate window for `POST /api/clipboard` and `/sync`.
- The window and account card were rendered under a virtual display and checked
  visually (not connected with an error, connected, reconnect needed).

Not verified, and to be treated as untested until it is:

- The macOS and Windows watchers, and copy-back of images on those systems, have never
  run on real hardware; they are tested against fakes and type-checked. The
  Windows-only smoke tests have not run.
- A real password manager on any platform. On Linux the skip is verified with a
  crafted X selection, because this `wl-copy` has no `--sensitive`.
- The production Clipboard+ service (the backend change is not deployed), the
  extension merge in a real browser (only its logic is unit tested), and Google
  Docs' in-page shortcuts against the default dictation shortcut.
- The two test failures that occur only inside `tests/Dockerfile` (the whisper.cpp
  source fallback, which needs cmake, and a GNOME-shortcut test that needs gsettings)
  are identical on the commit this work started from.

## Real transcription

Built whisper.cpp CLI and server from commit
`d09f61a708f3487afa956ff578e60eae5e7a233c` in an isolated Linux ARM64 container.
Used the upstream 11-second `samples/jfk.wav`, the `ggml-tiny.en.bin` model,
and four threads. Three requests were measured for each backend.

| Backend | Request times (seconds) | Median |
| --- | --- | --- |
| Local CLI | 0.481, 0.485, 0.485 | 0.485 |
| Resident local HTTP server | 0.372, 0.368, 0.368 | 0.368 |

The resident server's median was about 24% lower for this sample and environment.
Server startup was excluded; CLI model startup was included in every request.
This measures the benefit of reusing a loaded model, not a general hardware or
accuracy benchmark.

Both adapters returned the expected “ask not what your country can do for you”
speech. Their punctuation differed. The CLI result was:

> And so my fellow Americans ask not what your country can do for you, ask what you can do for your country.

Repeat with `tests/benchmark.py --model ... --cli ... --server ... --wav ...`.

## Remaining desktop verification

Physical ALSA/PipeWire microphone capture, GNOME shortcut registration, Wayland
clipboard delivery, notification rendering, accent/noise accuracy, actual VAD
model behavior, and paid third-party endpoints were not exercised. Desktop
binaries were substituted in lifecycle tests; real Whisper inference used
existing sample audio. No microphone was activated on the host.

macOS microphone permissions, Apple Shortcuts, Windows DirectShow capture,
clipboard/notifications and the Super+Shift+D shortcut need real desktop acceptance
testing. Windows ARM64 automatic dependency installation is not supported.
The one-command URLs only become usable after the implementation is published;
no release, push, signing or notarization was performed.

Concise rewriting was tested against an HTTP fixture, not a real text model or
paid provider. These checks prove transport/workflow behavior, not rewrite quality
or meaning preservation. Users must review generated drafts. The recording
indicator is inside the graphical app window, not a floating overlay or tray
application.
