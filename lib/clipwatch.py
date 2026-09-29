"""What every platform's clipboard watcher provides.

A watcher blocks until the clipboard changes and returns what was copied. Each
operating system has its own module (`clipwatch_linux`, `clipwatch_macos`,
`clipwatch_windows`); the clipboard service only sees this interface.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import clipstore
import desktop


class Unavailable(Exception):
    """Clipboard watching is not possible here (the message says why)."""


@dataclass(frozen=True)
class Clip:
    """One clipboard change. A concealed clip (a password manager's) carries no content."""

    text: str = ""
    image_png: bytes = b""
    concealed: bool = False
    source_app: str = ""  # The copying app when the platform can tell (bundle id, process, class).


@runtime_checkable
class Watcher(Protocol):
    def next_change(self, timeout: float) -> Clip | None:
        """Wait up to `timeout` seconds; None when nothing usable was copied."""
        ...

    def close(self) -> None: ...


# A copied secret is treated like a password manager's: never kept, never synced.
# Only recognisable formats (a whole copied token, or a block that is unmistakably a
# secret), so ordinary text is kept.
_SECRET = re.compile(
    r"(?:sk-|sk_live_|sk_test_|rk_live_|pk_live_|whsec_|gsk_|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_"
    r"|glpat-|cp_live_|xox[abprsoe]-|xapp-|AIza|ya29\.|sk-ant-|hf_|npm_|SG\.|shpat_)"
    r"[A-Za-z0-9_\-.]{16,}"
    r"|(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}"
)
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE|PGP PRIVATE KEY BLOCK)-----")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{0,}")
_EMBEDDED = re.compile(  # A secret inside longer text: only shapes that cannot be prose.
    r"(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bsk-ant-[A-Za-z0-9_-]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{20,}|\bsk_live_[A-Za-z0-9]{16,}|\bAIza[A-Za-z0-9_-]{35}\b"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}"
)
_ONE_TIME_CODE = re.compile(r"\d{6,8}")
_CARD = re.compile(r"\d(?:[ -]?\d){12,18}")
_TOKENISH = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")

# Apps whose copies are passwords or vault entries (matched against the copying app's
# bundle id, process name or window class, lower-cased, as a substring).
PASSWORD_MANAGERS = (
    "1password",
    "agilebits",
    "bitwarden",
    "keepassxc",
    "keepass",
    "lastpass",
    "dashlane",
    "keychain access",
    "com.apple.keychainaccess",
    "com.apple.passwords",
    "enpass",
    "nordpass",
    "proton pass",
    "proton.pass",
    "roboform",
    "keeper",
    "gnome-keyring",
    "seahorse",
    "kwallet",
    "pass-secret",
    "psono",
    "buttercup",
)


def from_password_manager(source: str) -> bool:
    """Whether the app named `source` (bundle id, process or window class) is a vault."""
    name = source.strip().lower()
    return bool(name) and any(manager in name for manager in PASSWORD_MANAGERS)


def _luhn(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _entropy(token: str) -> float:
    counts = {char: token.count(char) for char in set(token)}
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


def looks_secret(text: str) -> bool:
    """A copied credential: a known token format, a private key or certificate block, a
    JWT, a one-time code or card number copied alone, or one long random-looking token."""
    token = text.strip()
    if not token:
        return False
    if _PEM.search(token) or _EMBEDDED.search(token):
        return True
    if len(token) <= 4096 and _JWT.fullmatch(token):
        return True
    if len(token) > 400:
        return False
    if _SECRET.fullmatch(token) or _ONE_TIME_CODE.fullmatch(token):
        return True
    if _CARD.fullmatch(token):
        digits = re.sub(r"\D", "", token)
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    if _TOKENISH.fullmatch(token):
        # Mixed case and digits and high entropy: not a hash (hex only), a path or a word.
        classes = (
            any(c.islower() for c in token),
            any(c.isupper() for c in token),
            any(c.isdigit() for c in token),
        )
        return all(classes) and _entropy(token) >= 4.2
    return False


def limit_clip(
    clip: Clip,
    max_text: int = clipstore.MAX_TEXT_BYTES,
    max_image: int = clipstore.MAX_IMAGE_BYTES,
) -> Clip | None:
    """Drop oversize or blank parts; None when nothing is left to store."""
    if clip.concealed or from_password_manager(clip.source_app) or looks_secret(clip.text):
        return Clip(concealed=True)
    text = clip.text if 0 < len(clip.text.encode("utf-8", "replace")) <= max_text else ""
    if not text.strip():
        text = ""
    image = clip.image_png if 0 < len(clip.image_png) <= max_image else b""
    if not text and not image:
        return None
    return Clip(text=text, image_png=image)


_PNG_END = b"IEND\xaeB`\x82"


def trim_png(data: bytes) -> bytes:
    """A PNG without trailing bytes: some clipboards return a block larger than the image."""
    end = data.rfind(_PNG_END)
    return data if end == -1 else data[: end + len(_PNG_END)]


def _platform_watcher(platform: str) -> Watcher:
    # Imported lazily: each module needs libraries that exist only on its platform.
    if platform == "linux":
        import clipwatch_linux

        return clipwatch_linux.create()
    if platform == "macos":
        import clipwatch_macos

        return clipwatch_macos.create()
    if platform == "windows":
        import clipwatch_windows

        return clipwatch_windows.create()
    raise Unavailable(f"Clipboard history is not supported on {platform}.")


def create_watcher(platform: str | None = None) -> Watcher:
    """The watcher for this system, or Unavailable."""
    return _platform_watcher(platform or desktop.platform_name())
