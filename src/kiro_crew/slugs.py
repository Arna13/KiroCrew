"""Shared helpers for the backend slug builders and slug validators.

Each slug builder filters a user-supplied name down to a small ASCII set and
needs a fallback when nothing survives: a name written wholly in a non-ASCII
script (Chinese, Korean, Arabic, ...) filters to the empty string. Substituting
a bare constant there makes every such name derive the SAME identifier, so
distinct names collide.

Mirror ``knowledge.agent_source.document_slug`` and the frontend Spec Builder's
``slugify`` instead: fall back to a stable hash of the ORIGINAL input, so the
same name always derives the same id and distinct names derive distinct ids.

A slug VALIDATOR refuses rather than derives -- a name that becomes a directory
and an argv component stays a small ASCII alphabet on purpose. What it owes the
user instead is the character it refused, so the dashboard can say which one in
the user's own language (``refused_slug_char``).
"""

import hashlib
import unicodedata

__all__ = ["refused_slug_char", "slug_hash_fallback"]


def slug_hash_fallback(original: str, prefix: str) -> str:
    """Return ``<prefix>-<16 hex chars>`` derived deterministically from ``original``.

    The digest is a pure function of the input, so the same name derives the
    same id across calls and processes, while distinct names get distinct ids.
    The ``prefix-`` shape keeps each caller's naming convention, and the
    lowercase-hex charset plus the short fixed length satisfy every caller's
    slug pattern and length cap. ``surrogatepass`` keeps the encoding total:
    a lone surrogate is representable in a ``str`` (JSON escapes decode to
    one), and a strict encode would raise where the builders must not.
    """
    digest = hashlib.sha256(original.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def refused_slug_char(name: str, alphabet: str) -> str:
    """Return the first character of *name* outside *alphabet*, in a form safe to show.

    Reporting only, never validation: the caller's own pattern decides whether
    *name* is accepted, and this answers the follow-up question "which character
    stopped it". ``""`` means every character is in the alphabet, so the name
    failed on its length or on where a character sits (a leading dot, say).

    NFC first, so a decomposed ``u`` + U+0308 is reported as the ``ü`` the user
    typed rather than as a bare combining mark. A character that cannot stand on
    its own in a sentence -- a control or format character (a bidi override, a
    zero-width space), a combining mark, any separator -- comes back as its code
    point (``U+202E``) instead, so the message that names it can be neither
    hidden nor reordered by it.
    """
    for char in unicodedata.normalize("NFC", name):
        if char in alphabet:
            continue
        category = unicodedata.category(char)
        if char.isprintable() and category[0] not in "MZ":
            return char
        return f"U+{ord(char):04X}"
    return ""
