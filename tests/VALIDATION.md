# Validation evidence

## Automated checks

The cross-platform/installer update is checked with native macOS tests and an
isolated Linux container. The latest verified scope is:

- 65 unit/integration tests, including actual subprocess session control,
  live previews, cancellation, retained audio, clipboard failure recovery,
  HTTP multipart requests, redirect rejection, graphical workflows, and installer
  regression tests.
- 95% combined statement/branch coverage across the seven `lib` runtime modules
  (1,077 statements, 368 branch destinations): graphical app 99%, app service
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
  native/virtual walkthrough tests that verify no recording starts.
- Unix bootstrap orchestration and cleanup with substituted package/network
  commands. Actual package-manager installation was not performed on the host.
- Actual loopback HTTP requests test concise-draft creation, separate review/copy,
  original preservation, stale-draft rejection, no redirects or automatic retries,
  malformed/truncated responses, remote consent and secret non-persistence.
- The graphical status window is exercised natively on macOS and under a Linux
  virtual display with deterministic state/timer tests. Displayed model text has
  control characters removed.

CI defines Linux/macOS/Windows checks and Windows PowerShell parsing. These local
results are not a completed GitHub Actions run or an actual Windows installation.

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
clipboard/notifications and the Ctrl+Alt+D shortcut need real desktop acceptance
testing. Windows ARM64 automatic dependency installation is not supported.
The one-command URLs only become usable after the implementation is published;
no release, push, signing or notarization was performed.

Concise rewriting was tested against an HTTP fixture, not a real text model or
paid provider. These checks prove transport/workflow behavior, not rewrite quality
or meaning preservation. Users must review generated drafts. The recording
indicator is inside the graphical app window, not a floating overlay or tray
application.
