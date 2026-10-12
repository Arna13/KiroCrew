"""``aws.project_profiles``: an operator-owned project directory -> AWS profile map.

A session whose working directory lies in a mapped directory starts its agent
with ``AWS_PROFILE`` set to that profile. These tests pin what makes the binding
safe to ship as an opt-in:

* an empty map leaves every spawn environment exactly as the caller passed it;
* a profile the caller already chose (a cron job's ``env``, a workflow) wins;
* an invalid profile name or a relative key is ignored at load and refused at save;
* the deepest mapped directory wins, compared component by component, so a
  sibling sharing a name prefix (``proj`` / ``project2``) does not match;
* resolving the binding reads no file at all: no credential material, and
  nothing inside the project checkout;
* the scrub that strips AWS secrets from the agent environment still applies.
"""

from __future__ import annotations

import builtins
import io
import json
import logging
import os
import pathlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import aws_project_profiles as app
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.sections import AwsConfig


def _project(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


class TestResolveProjectProfile:
    def test_the_mapped_directory_itself_is_bound(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        assert app.resolve_project_profile({str(proj): "dev"}, str(proj)) == "dev"

    def test_a_directory_under_the_mapped_one_is_bound(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        nested = _project(proj, "src/pkg")
        assert app.resolve_project_profile({str(proj): "dev"}, str(nested)) == "dev"

    def test_an_unmapped_directory_is_unbound(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        other = _project(tmp_path, "other")
        assert app.resolve_project_profile({str(proj): "dev"}, str(other)) is None

    def test_a_sibling_sharing_a_name_prefix_is_not_bound(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        sibling = _project(tmp_path, "project2")
        assert app.resolve_project_profile({str(proj): "dev"}, str(sibling)) is None

    def test_the_deepest_mapped_directory_wins(self, tmp_path: Path) -> None:
        outer = _project(tmp_path, "work")
        inner = _project(outer, "billing")
        cwd = _project(inner, "infra")
        mapping = {str(inner): "billing-prod", str(outer): "dev"}
        assert app.resolve_project_profile(mapping, str(cwd)) == "billing-prod"
        assert app.resolve_project_profile(mapping, str(outer)) == "dev"
        reordered = {str(outer): "dev", str(inner): "billing-prod"}
        assert app.resolve_project_profile(reordered, str(cwd)) == "billing-prod"

    def test_a_trailing_separator_on_the_key_still_matches(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        coerced = app.coerce_project_profiles({str(proj) + os.sep: "dev"})
        assert app.resolve_project_profile(coerced, str(proj / "src")) == "dev"

    def test_a_symlinked_cwd_resolves_to_its_target(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        link = tmp_path / "link-to-proj"
        try:
            link.symlink_to(proj, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available to this user")
        assert app.resolve_project_profile({str(proj): "dev"}, str(link)) == "dev"

    def test_a_symlinked_key_resolves_to_its_target(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        link = tmp_path / "key-link"
        try:
            link.symlink_to(proj, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available to this user")
        assert app.resolve_project_profile({str(link): "dev"}, str(proj / "src")) == "dev"

    def test_a_symlink_inside_a_bound_project_cannot_carry_another_dir_in(
        self, tmp_path: Path
    ) -> None:
        """The real path decides: a link under the project naming a dir outside is unbound."""
        proj = _project(tmp_path, "proj")
        outside = _project(tmp_path, "outside")
        link = proj / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available to this user")
        assert app.resolve_project_profile({str(proj): "dev"}, str(link)) is None

    def test_an_empty_map_or_cwd_is_unbound(self, tmp_path: Path) -> None:
        assert app.resolve_project_profile({}, str(tmp_path)) is None
        assert app.resolve_project_profile({str(tmp_path): "dev"}, "") is None


class TestBoundProfileEnv:
    def test_an_empty_map_returns_the_callers_env_object_itself(self, tmp_path: Path) -> None:
        caller = {"FOO": "bar"}
        assert app.bound_profile_env({}, str(tmp_path), caller) is caller
        assert app.bound_profile_env({}, str(tmp_path), None) is None

    def test_an_unbound_cwd_returns_the_callers_env_object_itself(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        other = _project(tmp_path, "other")
        caller = {"FOO": "bar"}
        assert app.bound_profile_env({str(proj): "dev"}, str(other), caller) is caller
        assert app.bound_profile_env({str(proj): "dev"}, str(other), None) is None
        assert app.bound_profile_env({str(proj): "dev"}, None, None) is None

    def test_a_bound_cwd_adds_the_profile_without_mutating_the_callers_env(
        self, tmp_path: Path
    ) -> None:
        proj = _project(tmp_path, "proj")
        caller = {"FOO": "bar"}
        result = app.bound_profile_env({str(proj): "dev"}, str(proj), caller)
        assert result == {"FOO": "bar", "AWS_PROFILE": "dev"}
        assert caller == {"FOO": "bar"}
        assert app.bound_profile_env({str(proj): "dev"}, str(proj), None) == {"AWS_PROFILE": "dev"}

    @pytest.mark.parametrize("name", ["AWS_PROFILE", "AWS_DEFAULT_PROFILE"])
    def test_a_profile_the_caller_chose_wins(self, tmp_path: Path, name: str) -> None:
        proj = _project(tmp_path, "proj")
        caller = {name: "from-cron"}
        assert app.bound_profile_env({str(proj): "dev"}, str(proj), caller) is caller


class TestNoFileIsRead:
    """Resolving the binding opens no file: no credential, nothing in the checkout."""

    def test_resolution_opens_no_file_even_with_project_settings_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _project(tmp_path, "home")
        aws_dir = _project(home, ".aws")
        (aws_dir / "credentials").write_text("[dev]\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        proj = _project(tmp_path, "proj")
        settings = _project(proj, ".kiro/settings")
        planted = {"aws": {"project_profiles": {str(proj): "repo-chosen"}}, "AWS_PROFILE": "x"}
        for name in ("settings.json", "aws.json", "cli.json"):
            (settings / name).write_text(json.dumps(planted), encoding="utf-8")
        (proj / ".kiro" / "settings.json").write_text(json.dumps(planted), encoding="utf-8")

        def refuse(*_a: object, **_k: object) -> None:
            raise AssertionError("resolving a project profile must not open a file")

        with (
            patch.object(builtins, "open", refuse),
            patch.object(io, "open", refuse),
            patch.object(os, "open", refuse),
            patch.object(pathlib.Path, "open", refuse),
            patch.object(pathlib.Path, "read_text", refuse),
            patch.object(pathlib.Path, "read_bytes", refuse),
        ):
            unbound = app.bound_profile_env({}, str(proj), None)
            elsewhere = app.bound_profile_env({str(home): "dev"}, str(proj), None)
            bound = app.bound_profile_env({str(proj): "dev"}, str(proj / "src"), None)

        assert unbound is None, "a repo file must never bind a profile"
        assert elsewhere is None
        assert bound == {"AWS_PROFILE": "dev"}, "only the operator's map binds"


class TestCoerce:
    def test_valid_entries_are_kept_and_keys_normalized(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        raw = {str(proj) + os.sep: "dev", str(tmp_path / "b"): "sso.Admin+ro-1_x"}
        assert app.coerce_project_profiles(raw) == {
            str(proj): "dev",
            str(tmp_path / "b"): "sso.Admin+ro-1_x",
        }

    @pytest.mark.parametrize(
        "profile", ["", "-dash", "has space", "semi;colon", "a" * 129, "new\nline", 7]
    )
    def test_an_invalid_profile_name_is_dropped_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, profile: object
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.aws_project_profiles"):
            assert app.coerce_project_profiles({str(tmp_path): profile}) == {}
        assert any(str(tmp_path) in r.getMessage() for r in caplog.records)

    def test_a_relative_key_is_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.aws_project_profiles"):
            assert app.coerce_project_profiles({"relative/dir": "dev", "": "dev"}) == {}
        assert len(caplog.records) == 2

    def test_a_non_mapping_is_ignored(self) -> None:
        assert app.coerce_project_profiles(None) == {}
        assert app.coerce_project_profiles(["/a"]) == {}
        assert app.coerce_project_profiles("dev") == {}

    def test_two_keys_for_one_directory_keep_the_first(self, tmp_path: Path) -> None:
        raw = {str(tmp_path): "first", str(tmp_path) + os.sep: "second"}
        assert app.coerce_project_profiles(raw) == {str(tmp_path): "first"}

    def test_refusals_never_carry_the_profile_value(self, tmp_path: Path) -> None:
        reasons = app.project_profile_refusals({str(tmp_path): "not a profile; AKIAEXAMPLE"})
        assert len(reasons) == 1
        assert str(tmp_path) in reasons[0]
        assert "AKIAEXAMPLE" not in reasons[0]


class TestConfigLoadAndSave:
    def test_the_default_map_is_empty(self) -> None:
        assert KiroCrewConfig().aws.project_profiles == {}
        assert KiroCrewConfig().to_dict()["aws"] == {"project_profiles": {}}

    def test_load_keeps_valid_entries_and_drops_invalid_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path, "proj")
        stored = tmp_path / "config.json"
        stored.write_text(
            json.dumps(
                {
                    "aws": {
                        "project_profiles": {
                            str(proj): "dev",
                            str(tmp_path / "bad"): "bad name",
                            "relative": "dev",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: stored)
        assert KiroCrewConfig.load().aws.project_profiles == {str(proj): "dev"}

    def test_a_saved_binding_survives_a_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path, "proj")
        stored = tmp_path / "config.json"
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: stored)
        chosen = KiroCrewConfig(aws=AwsConfig(project_profiles={str(proj): "dev"}))
        stored.write_text(json.dumps(chosen.to_dict()), encoding="utf-8")
        assert KiroCrewConfig.load().aws.project_profiles == {str(proj): "dev"}

    def test_a_write_introducing_an_invalid_profile_is_refused(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import ConfigWriteRefused, update_config_locked

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"agent": {"yolo": True}}), encoding="utf-8")
        before = path.read_bytes()

        def plant(existing: dict) -> dict:
            existing["aws"] = {"project_profiles": {str(tmp_path): "bad name; AKIAEXAMPLE"}}
            return existing

        with pytest.raises(ConfigWriteRefused) as refused:
            update_config_locked(path, mutate=plant)
        assert str(tmp_path) in str(refused.value)
        assert "AKIAEXAMPLE" not in str(refused.value)
        assert path.read_bytes() == before

    def test_a_write_introducing_a_relative_key_is_refused(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import ConfigWriteRefused, update_config_locked

        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")

        def plant(existing: dict) -> dict:
            existing["aws"] = {"project_profiles": {"relative/dir": "dev"}}
            return existing

        with pytest.raises(ConfigWriteRefused):
            update_config_locked(path, mutate=plant)

    def test_a_valid_write_lands(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import update_config_locked

        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")

        def plant(existing: dict) -> dict:
            existing["aws"] = {"project_profiles": {str(tmp_path): "dev"}}
            return existing

        update_config_locked(path, mutate=plant)
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["aws"]["project_profiles"] == {str(tmp_path): "dev"}

    def test_a_pre_existing_bad_entry_does_not_refuse_an_unrelated_write(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.config.loader import update_config_locked

        path = tmp_path / "config.json"
        stale = {"relative/dir": "dev"}
        path.write_text(json.dumps({"aws": {"project_profiles": stale}}), encoding="utf-8")

        def unrelated(existing: dict) -> dict:
            existing.setdefault("agent", {})["yolo"] = True
            return existing

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            update_config_locked(path, mutate=unrelated)
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["agent"]["yolo"] is True
        assert written["aws"]["project_profiles"] == stale
        assert any("aws.project_profiles" in r.getMessage() for r in caplog.records)


class TestScrubKeepsTheProfile:
    def test_aws_profile_survives_the_agent_scrub_and_secrets_do_not(self) -> None:
        from kiro_crew.sandbox import scrub_agent_subprocess_env

        env = {
            "PATH": "/usr/bin",
            "AWS_PROFILE": "dev",
            "AWS_SECRET_ACCESS_KEY": "s",
            "AWS_SESSION_TOKEN": "t",
        }
        scrubbed = scrub_agent_subprocess_env(env)
        assert scrubbed["AWS_PROFILE"] == "dev"
        assert "AWS_SECRET_ACCESS_KEY" not in scrubbed
        assert "AWS_SESSION_TOKEN" not in scrubbed


def _recording_factory(calls: list[dict]):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        calls.append(kwargs)
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.disown_work_dir = MagicMock()
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_abort_target = lambda: None
        m.spawn_identity = ""
        m._runtime = None
        m.cwd = kwargs.get("cwd") or ""
        return m

    return factory


def _cfg(mapping: dict[str, str], pool_size: int = 0) -> KiroCrewConfig:
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    c.session.pool_size = pool_size
    c.aws.project_profiles = dict(mapping)
    return c


class TestSessionSpawn:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller_env", [None, {"FOO": "bar"}])
    async def test_an_empty_map_passes_the_callers_env_through_unchanged(
        self, tmp_path: Path, caller_env: dict | None
    ) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        settings = _project(proj, ".kiro/settings")
        (settings / "aws.json").write_text(
            json.dumps({"project_profiles": {str(proj): "repo-chosen"}}), encoding="utf-8"
        )
        calls: list[dict] = []
        mgr = SessionManager(_cfg({}), provider_factory=_recording_factory(calls))
        try:
            await mgr.get_or_create("chat-6332", cwd=str(proj), extra_env=caller_env)
        finally:
            await mgr.close_all()
        assert len(calls) == 1
        assert calls[0]["extra_env"] is caller_env

    @pytest.mark.asyncio
    async def test_an_unbound_cwd_passes_the_callers_env_through_unchanged(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        sibling = _project(tmp_path, "project2")
        calls: list[dict] = []
        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=_recording_factory(calls))
        try:
            await mgr.get_or_create("chat-6332", cwd=str(sibling), extra_env=None)
        finally:
            await mgr.close_all()
        assert calls[0]["extra_env"] is None

    @pytest.mark.asyncio
    async def test_a_bound_cwd_starts_the_agent_with_the_profile(self, tmp_path: Path) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        calls: list[dict] = []
        caller = {"FOO": "bar"}
        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=_recording_factory(calls))
        try:
            await mgr.get_or_create("chat-6332", cwd=str(proj / "src"), extra_env=caller)
        finally:
            await mgr.close_all()
        assert calls[0]["extra_env"] == {"FOO": "bar", "AWS_PROFILE": "dev"}
        assert caller == {"FOO": "bar"}

    @pytest.mark.asyncio
    async def test_the_profile_reaches_only_the_env_and_is_logged_by_name(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        calls: list[dict] = []
        mgr = SessionManager(
            _cfg({str(proj): "zq-acct-7"}), provider_factory=_recording_factory(calls)
        )
        try:
            with caplog.at_level(logging.INFO):
                await mgr.get_or_create("chat-6332", cwd=str(proj))
        finally:
            await mgr.close_all()
        assert calls[0]["extra_env"] == {"AWS_PROFILE": "zq-acct-7"}
        elsewhere = [k for k, v in calls[0].items() if k != "extra_env" and "zq-acct-7" in repr(v)]
        assert elsewhere == [], "the profile name travels only as the AWS_PROFILE value"
        applied = [r.getMessage() for r in caplog.records if "zq-acct-7" in r.getMessage()]
        assert applied == ["session chat-6332: AWS profile 'zq-acct-7' from aws.project_profiles"]

    @pytest.mark.asyncio
    async def test_an_unbound_spawn_logs_no_profile(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        other = _project(tmp_path, "other")
        mgr = SessionManager(
            _cfg({str(proj): "zq-acct-7"}), provider_factory=_recording_factory([])
        )
        try:
            with caplog.at_level(logging.INFO):
                await mgr.get_or_create("chat-6332", cwd=str(other))
        finally:
            await mgr.close_all()
        assert not [r for r in caplog.records if "AWS profile" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_a_callers_explicit_profile_wins(self, tmp_path: Path) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        calls: list[dict] = []
        caller = {"AWS_PROFILE": "from-cron"}
        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=_recording_factory(calls))
        try:
            await mgr.get_or_create("chat-6332", cwd=str(proj), extra_env=caller)
        finally:
            await mgr.close_all()
        assert calls[0]["extra_env"] is caller

    @pytest.mark.asyncio
    async def test_a_bound_session_at_the_pool_directory_skips_the_warm_pool(
        self, tmp_path: Path
    ) -> None:
        """A pooled process was spawned without the profile, so a bound session never takes one."""
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        calls: list[dict] = []
        mgr = SessionManager(
            _cfg({str(proj): "dev"}, pool_size=2), provider_factory=_recording_factory(calls)
        )
        mgr._pool_cwd = str(proj)
        mgr._drain_and_claim = AsyncMock(side_effect=AssertionError("pool must be bypassed"))
        mgr._schedule_replenish = MagicMock()
        decisions: list[str] = []
        mgr._record_pool_decision = lambda decision, _key: decisions.append(decision)
        try:
            await mgr.get_or_create("chat-6332", cwd=str(proj))
        finally:
            await mgr.close_all()
        mgr._drain_and_claim.assert_not_awaited()
        assert decisions == ["bypass_env"]
        assert calls[0]["extra_env"] == {"AWS_PROFILE": "dev"}


class TestTaskRunStep:
    @pytest.mark.asyncio
    async def test_a_bound_step_takes_the_dedicated_path(self, tmp_path: Path) -> None:
        from kiro_crew.session import SessionManager
        from kiro_crew.start_priority import StartPriority

        proj = _project(tmp_path, "proj")
        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=_recording_factory([]))
        dedicated = MagicMock()
        mgr.get_or_create = AsyncMock(return_value=(dedicated, True, False))
        mgr._get_or_bootstrap_run_runtime = AsyncMock(
            side_effect=AssertionError("a bound step must not share the run's runtime")
        )
        try:
            result = await mgr.open_task_session(
                "taskrunner:run6332:runtime",
                "taskrunner:run6332:task0",
                agent="kirocrew",
                cwd=str(proj),
            )
        finally:
            await mgr.close_all()
        assert result == (dedicated, True, False)
        mgr.get_or_create.assert_awaited_once_with(
            "taskrunner:run6332:task0",
            agent="kirocrew",
            approval_policy="",
            cwd=str(proj),
            start_priority=StartPriority.BACKGROUND,
        )

    @pytest.mark.asyncio
    async def test_an_unbound_step_keeps_the_shared_runtime(self, tmp_path: Path) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        other = _project(tmp_path, "other")

        class _SharedPath(Exception):
            pass

        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=_recording_factory([]))
        mgr.get_or_create = AsyncMock(side_effect=AssertionError("must not divert"))
        mgr._get_or_bootstrap_run_runtime = AsyncMock(side_effect=_SharedPath())
        try:
            with pytest.raises(_SharedPath):
                await mgr.open_task_session(
                    "taskrunner:run6332b:runtime",
                    "taskrunner:run6332b:task0",
                    agent="kirocrew",
                    cwd=str(other),
                )
        finally:
            await mgr.close_all()
