"""Bind an AWS named profile to a project directory (``aws.project_profiles``).

The operator maps absolute project directories to profile names in
``config.json``; a session whose working directory falls inside a mapped
directory starts its agent with ``AWS_PROFILE`` set to that profile. This is a
guard against running a command on the wrong account, not an isolation boundary:
the agent can still name another profile itself.

The map lives in the operator's own configuration and nowhere else. Nothing here
reads a file inside the project checkout, so a cloned repository cannot choose
which credentials an agent uses. Nothing here reads credential material either:
the value is a profile NAME, and the AWS SDK or CLI the agent runs resolves it
through the operator's own ``~/.aws`` files exactly as it would for a gateway
started with ``AWS_PROFILE`` set.

An unmapped directory, an empty map, and a caller that already chose a profile
(a cron job's ``env``, a workflow) all leave the spawn environment exactly as the
caller passed it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import PurePath

from kiro_crew.constants import AWS_PROFILE_NAME_RE

logger = logging.getLogger(__name__)

#: The config path of the map, for messages.
PROJECT_PROFILES_KEY = "aws.project_profiles"

#: The variable a bound project sets on its agent process.
AWS_PROFILE_ENV = "AWS_PROFILE"

#: Variables through which a caller has already chosen a profile. Either one in
#: the caller's ``extra_env`` means the binding stays out of the way.
CALLER_PROFILE_VARS = (AWS_PROFILE_ENV, "AWS_DEFAULT_PROFILE")

#: Longest directory key accepted, in characters.
PROJECT_DIR_MAX_LEN = 4096

_PROFILE_RULE = (
    "a profile name is 1 to 128 characters of letters, digits, '_', '.', '+' "
    "and '-', and does not start with '-'"
)


def _entry_refusal(key: object, value: object) -> str | None:
    """Why one ``directory -> profile`` entry is unusable, or ``None``."""
    if not isinstance(key, str) or not key or len(key) > PROJECT_DIR_MAX_LEN or "\x00" in key:
        return "the key must be an absolute directory path"
    if not os.path.isabs(key):
        return f"{key!r}: the key must be an absolute directory path"
    if not isinstance(value, str) or not AWS_PROFILE_NAME_RE.match(value):
        # The value is never echoed: a mistyped entry may hold anything.
        return f"{key!r}: {_PROFILE_RULE}"
    return None


def project_profile_refusals(raw: object) -> list[str]:
    """Reasons each entry of *raw* would be dropped at load; empty when all are usable.

    A ``None`` (the key absent) is usable. Messages name the directory only,
    never the profile value.
    """
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"{PROJECT_PROFILES_KEY} must be an object of directory -> profile name"]
    reasons = []
    for key, value in raw.items():
        reason = _entry_refusal(key, value)
        if reason is not None:
            reasons.append(reason)
    return reasons


def coerce_project_profiles(raw: object) -> dict[str, str]:
    """Normalize ``aws.project_profiles`` to ``{normalized absolute dir: profile}``.

    An unusable entry is dropped with a warning naming its directory, so a typo
    costs that one binding and the project falls back to the default credential
    chain. Directory keys are normalized with :func:`os.path.normpath` (a
    trailing separator is dropped); when two keys normalize to the same
    directory, the first one wins.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("%s is not an object; ignoring it", PROJECT_PROFILES_KEY)
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        reason = _entry_refusal(key, value)
        if reason is not None:
            logger.warning("%s: ignoring an entry, %s", PROJECT_PROFILES_KEY, reason)
            continue
        directory = os.path.normpath(key)
        if directory in out:
            logger.warning(
                "%s: ignoring %r, it names the same directory as an earlier entry",
                PROJECT_PROFILES_KEY,
                key,
            )
            continue
        out[directory] = value
    return out


def _dir_parts(path: str) -> tuple[str, ...]:
    """Comparable path components of *path*, symlinks resolved."""
    return PurePath(os.path.normcase(os.path.realpath(path))).parts


def resolve_project_profile(mapping: Mapping[str, str], cwd: str) -> str | None:
    """The profile bound to *cwd*, or ``None`` when no mapped directory contains it.

    A mapped directory contains *cwd* when it is *cwd* itself or one of its
    ancestors, compared whole component by component after resolving symlinks
    on both sides: ``/a/proj`` contains ``/a/proj/src`` but not ``/a/project2``.
    The deepest containing directory wins. Touches the filesystem (``realpath``),
    so an async caller runs it off the event loop.
    """
    if not mapping or not cwd:
        return None
    target = _dir_parts(cwd)
    best: str | None = None
    best_depth = -1
    for directory, profile in mapping.items():
        parts = _dir_parts(directory)
        depth = len(parts)
        if depth > best_depth and target[:depth] == parts:
            best, best_depth = profile, depth
    return best


def bound_profile_env(
    mapping: Mapping[str, str],
    cwd: str | None,
    extra_env: dict[str, str] | None,
) -> dict[str, str] | None:
    """*extra_env* with ``AWS_PROFILE`` set for a bound *cwd*, else *extra_env* itself.

    The same object comes back, untouched, whenever nothing is bound: an empty
    map, no *cwd*, a directory no entry contains, or a caller whose *extra_env*
    already names a profile (:data:`CALLER_PROFILE_VARS`). Otherwise a new
    mapping is returned and the caller's dict is not mutated.
    """
    if not mapping or not cwd:
        return extra_env
    if extra_env and any(name in extra_env for name in CALLER_PROFILE_VARS):
        return extra_env
    profile = resolve_project_profile(mapping, cwd)
    if profile is None:
        return extra_env
    return {**(extra_env or {}), AWS_PROFILE_ENV: profile}


def configured_project_profiles(cfg: object) -> Mapping[str, str]:
    """``cfg.aws.project_profiles``, or an empty map for a config without one."""
    aws = getattr(cfg, "aws", None)
    profiles = getattr(aws, "project_profiles", None)
    return profiles if isinstance(profiles, Mapping) else {}
