"""Split a ``*_MCP_SERVER_ARGS`` env value into argv, Windows paths intact.

POSIX ``shlex.split`` treats ``\\`` as an escape character, so a Windows
entrypoint like ``C:\\x\\dist\\index.js`` came back as ``C:xdistindex.js``.
That was latent from the first commit (Windows normally launches the vendored
SEA exe, which takes no args) and surfaced once ``--doctor`` / the service's
Forma probe started checking that the entrypoint exists (2026-09-13).

On Windows the lexer keeps POSIX quote handling — ``"C:\\Program Files\\x.js"``
is one token, quotes removed — but has no escape character, so every
backslash is literal. Everywhere else ``shlex.split`` is unchanged, so a macOS
setup that escapes a space as ``\\ `` keeps working.
"""

from __future__ import annotations

import os
import shlex

# Read at call time (not baked into a default argument) so a test can force
# either mode on any OS by monkeypatching this attribute.
_WINDOWS = os.name == "nt"


def split_server_args(raw: str) -> list[str]:
    """Return argv for ``raw``; an empty string yields ``[]``."""
    if not raw:
        return []
    if not _WINDOWS:
        return shlex.split(raw)
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)
