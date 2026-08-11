"""
i18n.py – Internationalization support for smolplayer.

Provides gettext translation wrapper for user-facing strings.
"""

from __future__ import annotations

import gettext
import os
from typing import Callable

DOMAIN = "smolplayer"

# Search paths for compiled .mo files
_SEARCH_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale"),
    os.path.expanduser("~/.local/share/locale"),
    "/app/share/locale",
    "/usr/share/locale",
    "/usr/local/share/locale",
]


def _init_gettext() -> tuple[Callable[[str], str], Callable[[str, str, int], str]]:
    for loc_dir in _SEARCH_PATHS:
        if os.path.isdir(loc_dir):
            try:
                t = gettext.translation(DOMAIN, localedir=loc_dir, fallback=False)
                return t.gettext, t.ngettext
            except OSError:
                continue

    # Fallback if no translation file found for current locale
    t = gettext.NullTranslations()
    return t.gettext, t.ngettext


_, ngettext = _init_gettext()
