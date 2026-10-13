"""kiro_crew.mcp_grant -- the pod-aware OAuth-cache-directory resolver.

The resolver ``kiro_oauth_cache_dir()`` must not call ``Path.home()``
directly. A pod's gateway process and its OWN kiro-cli children both keep the
REAL host ``$HOME`` unless something remaps it, so a pod's ``mcp_grant`` reads
(mint, status, disconnect, mcp_discovery's remote probe) stated grants under
the real host's ``~/.aws/sso/cache`` while a pod's own kiro-cli children wrote
grants there too -- so a Connections card inside a pod read "Connected" from a
grant the operator minted on the real machine, and a grant minted inside a
pod was a real, durable, machine-level credential that OUTLIVED ``pod down``.

These tests pin the resolver in isolation. The ACP-spawn side of the fix (the
``HOME`` remap applied to a pod-spawned kiro-cli child) is pinned in
``test_acp_pod_home_remap.py``; the pod-boot seeding side is pinned in
``test_pod.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kiro_crew import mcp_grant
from kiro_crew.config import paths as paths_mod


def _no_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIROCREW_OS_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)


class TestKiroOauthCacheHome:
    """``config.paths.kiro_oauth_cache_home`` -- the resolver ``mcp_grant``
    and the pod-spawn ``HOME`` remap must both read.
    """

    def test_defaults_to_path_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _no_overrides(monkeypatch)
        assert paths_mod.kiro_oauth_cache_home() == Path.home()

    def test_honors_kirocrew_os_home_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(tmp_path / "pod-os-home"))
        assert paths_mod.kiro_oauth_cache_home() == (tmp_path / "pod-os-home").resolve()

    def test_expands_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", "~/some-os-home")
        assert paths_mod.kiro_oauth_cache_home() == (Path.home() / "some-os-home").resolve()

    def test_refuses_a_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A malformed override must degrade to Path.home(), never scatter
        OAuth artifacts across a filesystem root."""
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", "/")
        assert paths_mod.kiro_oauth_cache_home() == Path.home()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="/usr, /etc are POSIX system dirs; not privileged on Windows",
    )
    @pytest.mark.parametrize("bad", ["/usr", "/etc"])
    def test_refuses_posix_system_dirs(self, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", bad)
        assert paths_mod.kiro_oauth_cache_home() == Path.home()

    def test_the_override_is_inert_outside_a_pod(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Reads and writes turn on together. `_apply_pod_home_remap` moves
        kiro-cli's writes only when `KIROCREW_POD == "1"`, so honouring the
        override here without that marker would repoint grant READS while the
        writes stayed under the real home -- recreating the exact read/write
        split this resolver exists to close."""
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv("KIROCREW_OS_HOME", str(tmp_path / "pod-os-home"))
        assert paths_mod.kiro_oauth_cache_home() == Path.home()

    def test_shares_the_unsafe_home_predicate_with_kiro_home(self) -> None:
        """One predicate for both overrides, so they refuse the same targets --
        catching a future divergence where one resolver's guard is edited and
        the other's is not."""
        from kiro_crew.config.paths import _is_unsafe_home

        assert paths_mod.kiro_oauth_cache_home.__module__ == paths_mod.kiro_home.__module__
        assert _is_unsafe_home(Path(Path("/").resolve().anchor))


class TestKiroOauthCacheDirResolvesThroughTheSharedDefault:
    """``mcp_grant.kiro_oauth_cache_dir()`` -- the ONE default every caller
    (mint, status, disconnect, mcp_discovery) reaches with no explicit
    ``cache_dir``/``home``.
    """

    def test_default_follows_kirocrew_os_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pod_os_home = tmp_path / "pod-os-home"
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(pod_os_home))
        assert mcp_grant.kiro_oauth_cache_dir() == pod_os_home.resolve() / ".aws" / "sso" / "cache"

    def test_default_falls_back_to_real_home_outside_a_pod(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_overrides(monkeypatch)
        assert mcp_grant.kiro_oauth_cache_dir() == Path.home() / ".aws" / "sso" / "cache"

    def test_explicit_home_still_overrides_the_resolver(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An explicit ``home=`` kwarg (tests, or a future caller that already
        resolved a directory) must win over KIROCREW_OS_HOME -- the resolver is
        the DEFAULT, not the only path."""
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(tmp_path / "pod-os-home"))
        explicit = tmp_path / "explicit-home"
        assert mcp_grant.kiro_oauth_cache_dir(home=explicit) == explicit / ".aws" / "sso" / "cache"

    def test_grant_presence_reads_the_pod_scoped_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A grant minted on the REAL host must be invisible from inside a pod,
        and vice versa -- the core isolation contract."""
        real_home = tmp_path / "real-home"
        pod_os_home = tmp_path / "pod-os-home"
        real_cache = real_home / ".aws" / "sso" / "cache"
        real_cache.mkdir(parents=True)
        key = mcp_grant.grant_key("https://mcp.example.com/mcp")
        (real_cache / f"{key}.token.json").write_text("{}")
        (real_cache / f"{key}.registration.json").write_text("{}")

        monkeypatch.delenv("KIROCREW_OS_HOME", raising=False)
        assert mcp_grant.grant_presence("https://mcp.example.com/mcp", cache_dir=real_cache) is True

        # From "inside the pod" (KIROCREW_OS_HOME set), the same URL's grant is
        # unreadable -- no artifacts exist under the pod-scoped tree.
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(pod_os_home))
        assert paths_mod.kiro_oauth_cache_home() == pod_os_home.resolve()
        pod_cache_dir = mcp_grant.kiro_oauth_cache_dir()
        assert pod_cache_dir == pod_os_home.resolve() / ".aws" / "sso" / "cache"
        assert (
            mcp_grant.grant_presence("https://mcp.example.com/mcp", cache_dir=pod_cache_dir)
            is False
        )

    def test_every_default_caller_reaches_one_resolver(self) -> None:
        """Structural guard: every ``mcp_grant`` function that resolves its own
        cache directory delegates through ``kiro_oauth_cache_dir()``, so a
        second hand-written ``Path.home()`` call cannot silently reintroduce the
        split this module exists to close."""
        import ast
        import inspect

        offenders: list[str] = []
        for name, fn in vars(mcp_grant).items():
            if not inspect.isfunction(fn) or fn.__module__ != mcp_grant.__name__:
                continue
            if name == "kiro_oauth_cache_dir":
                continue
            try:
                source = inspect.getsource(fn)
            except OSError:
                continue
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "home"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "Path"
                ):
                    offenders.append(name)
        assert not offenders, (
            "mcp_grant function(s) call Path.home() directly instead of "
            f"routing through kiro_oauth_cache_dir(): {offenders}"
        )


def _write_pair(cache: Path, key: str) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{key}.token.json").write_text("{}")
    (cache / f"{key}.registration.json").write_text("{}")


class TestGrantLayoutDriftSuspected:
    """Cached grant pairs that match no configured server read as drift.

    A drifted key mirror makes every authorized server look signed out. The only
    evidence on this machine is the pairs kiro-cli actually wrote, so pairs that
    belong to no configured url turn a confident "no grant" into "unknown".
    """

    URL = "https://mcp.example.com/mcp"

    @pytest.fixture(autouse=True)
    def _reset_warn_once(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(mcp_grant, "_drift_warned", False)

    def test_pairs_matching_no_configured_server_are_drift(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_pair(tmp_path, "f" * 64)
        with caplog.at_level("WARNING", logger="kiro_crew.mcp_grant"):
            assert mcp_grant.grant_layout_drift_suspected([self.URL], cache_dir=tmp_path)
        warnings = [r for r in caplog.records if "layout drift?" in r.getMessage()]
        assert len(warnings) == 1
        # A count, never a key: the keys name which endpoints are signed in.
        assert "f" * 64 not in caplog.text
        assert mcp_grant.grant_key(self.URL) not in caplog.text

    def test_the_warning_fires_once_per_episode(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_pair(tmp_path, "f" * 64)
        with caplog.at_level("WARNING", logger="kiro_crew.mcp_grant"):
            for _ in range(3):
                assert mcp_grant.grant_layout_drift_suspected([self.URL], cache_dir=tmp_path)
        assert sum("layout drift?" in r.getMessage() for r in caplog.records) == 1

    def test_a_matching_pair_proves_the_mirror(self, tmp_path: Path) -> None:
        _write_pair(tmp_path, "f" * 64)
        _write_pair(tmp_path, mcp_grant.grant_key(self.URL))
        other = "https://other.example.com/mcp"
        assert not mcp_grant.grant_layout_drift_suspected([other, self.URL], cache_dir=tmp_path)

    @pytest.mark.parametrize(
        "bad_url", ["https://h:99999/mcp", "https://h:abc/mcp", "http://[::1/mcp"]
    )
    def test_a_malformed_configured_url_is_skipped_not_raised(
        self, tmp_path: Path, bad_url: str
    ) -> None:
        _write_pair(tmp_path, mcp_grant.grant_key(self.URL))
        assert not mcp_grant.grant_layout_drift_suspected([bad_url, self.URL], cache_dir=tmp_path)
        assert mcp_grant.grant_layout_drift_suspected([bad_url], cache_dir=tmp_path)

    def test_no_pairs_is_a_user_who_never_signed_in(self, tmp_path: Path) -> None:
        # A lone token file is the single-file SSO naming, not a grant pair.
        (tmp_path / f"{'a' * 64}.token.json").write_text("{}")
        (tmp_path / f"{'b' * 64}.json").write_text("{}")
        assert not mcp_grant.grant_layout_drift_suspected([self.URL], cache_dir=tmp_path)

    def test_a_missing_cache_directory_is_not_drift(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        assert not mcp_grant.grant_layout_drift_suspected([self.URL], cache_dir=missing)

    def test_an_unlistable_cache_directory_is_not_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_pair(tmp_path, "f" * 64)

        def _denied(_path):
            raise PermissionError("denied")

        monkeypatch.setattr(mcp_grant.os, "scandir", _denied)
        assert not mcp_grant.grant_layout_drift_suspected([self.URL], cache_dir=tmp_path)

    def test_no_artifact_is_opened(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_pair(tmp_path, "f" * 64)

        def _refuse(*_args, **_kwargs):
            raise AssertionError("a grant artifact was opened")

        monkeypatch.setattr(Path, "open", _refuse)
        monkeypatch.setattr("builtins.open", _refuse)
        assert mcp_grant.grant_layout_drift_suspected([self.URL], cache_dir=tmp_path)
