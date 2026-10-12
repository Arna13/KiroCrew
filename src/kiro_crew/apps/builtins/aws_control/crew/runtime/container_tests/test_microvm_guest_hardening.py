"""What the guest keeps away from the crew uid, from a stranger, and from a log.

Four properties of a VM whose hook port is reachable from the internet, whose
root identity reads every crew's secret under the lane's prefix, and whose crew is
the operator's own agent running as another uid in the same kernel.

**The node's role credentials are root's.** ``Profile.ShareCreds`` makes the SSM
agent publish them under the HOME of the process that runs it, and the image's
process-wide ``HOME`` is the crew user's own directory. An agent that inherits
that HOME writes the lane's strongest credential into a directory the crew uid
owns -- readable, and replaceable before a rotation. So the agent gets a HOME of
its own, the image gates that the crew uid can neither read nor replace it, and
the crew's own environment is built from an allowlist that does not carry the
pointer to it.

**A ``/run`` payload is not evidence of the platform.** The hook listener holds no
key and ``/run`` is single-shot, so a caller who reaches the port first hands the
VM an activation minted in their own account. The payload has to agree with itself
and with what the image was built knowing.

**Every reply is a constant, including the ones the stdlib writes.** ``http.server``
answers an unimplemented method or an unparseable request line with an HTML page
naming the method and the reason, which describes the guest's surface to whoever
asked.

**The agent itself is pinned.** ``latest/`` in a bucket is a moving object that
becomes a root process holding that role, inside an image addressed by a digest
that does not change when the bucket does.
"""

from __future__ import annotations

import re
import socket
import threading
from pathlib import Path

import pytest
from container import common
from container.microvm import hooks

#: The image layer the MicroVM lane builds, read rather than restated.
_RUNTIME = Path(common.__file__).resolve().parents[2]
_DOCKERFILE = _RUNTIME / "Dockerfile.microvm"

#: The lane's base stack, which carries the two guest roles. Reached from the
#: runtime directory because this package ships inside an image and the template
#: does not: a tree without it skips those tests rather than failing them.
_TEMPLATE = _RUNTIME.parents[4] / "cloud" / "templates" / "kirocrew-microvm-base.yaml"

#: Every directory the guest looks in for the published credential, derived from
#: the guest's own list so a candidate added there has to be gated too.
_CREDENTIAL_HOMES = {
    path[: -len("/.aws/credentials")]
    for path in hooks.SHARED_CREDS_CANDIDATES
    if path.endswith("/.aws/credentials")
}

#: A payload the lane really writes: version 1, one account, one region, the
#: activation under ``ssm``, and both secrets as ARNs.
_ACCOUNT = "111122223333"
_REGION = "us-east-1"


def _payload(**overrides: object) -> dict:
    base = {
        "v": 1,
        "tag": "kc-a1b2c3",
        "gen": 1,
        "region": _REGION,
        "ssm": {"id": "a1b2c3d4-0000-0000-0000-000000000000", "code": "code-value"},
        "secretRef": (
            f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:kirocrew/crew/kc-a1b2c3-AbCdEf"
        ),
        "identityRef": (
            f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:kirocrew/crew/identity-GhIjKl"
        ),
    }
    base.update(overrides)
    return base


def _dockerfile() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def _dockerfile_code() -> str:
    """The Dockerfile with its comment lines dropped.

    Every assertion below is about an instruction that RUNS. A substring search
    over the whole file passes on a directive that appears only in a comment,
    which is the shape of this file's own prose.
    """
    joined = re.sub(r"\\\s*\n\s*", " ", _dockerfile())
    return "\n".join(line for line in joined.splitlines() if not line.lstrip().startswith("#"))


@pytest.fixture()
def guest_state(tmp_path, monkeypatch):
    """The guest's own state directory, redirected into a temp tree."""
    monkeypatch.setattr(hooks, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(hooks, "RUN_MARKER", str(tmp_path / "run.done"))
    monkeypatch.setattr(hooks, "BOOT_STATE", str(tmp_path / "boot.json"))
    monkeypatch.setattr(hooks, "PAYLOAD_SEEN", str(tmp_path / "payload-seen.json"))
    return tmp_path


class TestTheNodeCredentialIsOutOfTheCrewUidsReach:
    """Blocking: the managed node's role credentials landed in the crew's own home."""

    def test_the_agent_runs_with_a_home_of_its_own(self, monkeypatch):
        """Even -- especially -- when this process's own HOME is the crew's."""
        monkeypatch.setenv("HOME", hooks.CREW_HOME)
        assert hooks.agent_env()["HOME"] == hooks.AGENT_HOME

    def test_that_home_is_not_inside_the_crew_users_own(self):
        assert not hooks.AGENT_HOME.startswith(hooks.CREW_HOME)

    def test_that_home_is_not_in_the_run_command_tree(self):
        """Run Command stages a script under the agent's orchestration root and
        runs it as ``sudo -u crew``, so that tree stays traversable by the crew
        uid and cannot hold the credential."""
        assert not hooks.AGENT_HOME.startswith("/var/lib/amazon/ssm")

    def test_the_agent_is_not_told_where_this_process_put_the_credential(self, monkeypatch):
        """``wait_for_shared_credentials`` sets this name on PID 1 so the guest's
        own boto3 reads the file. Handing it back to the agent would let a value
        this process chose decide where the agent writes."""
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/var/lib/crew/.aws/credentials")
        assert "AWS_SHARED_CREDENTIALS_FILE" not in hooks.agent_env()

    def test_no_credential_candidate_sits_under_the_crew_home(self):
        """The guest looks for the published credential in a fixed list. A path
        under the crew's home in that list is the defect restated."""
        for path in hooks.SHARED_CREDS_CANDIDATES:
            assert not path.startswith(hooks.CREW_HOME), path

    def test_the_candidate_list_is_the_agents_home_and_roots(self):
        """Non-vacuity: the list must still name somewhere the agent writes, or
        the boot waits out its credential timeout and the crew never starts."""
        assert hooks.SHARED_CREDS_CANDIDATES[0].startswith(hooks.AGENT_HOME + "/")
        assert hooks.SHARED_CREDS_CANDIDATES[0].endswith("/.aws/credentials")

    def test_the_run_hook_starts_the_agent_with_that_home(self, guest_state, monkeypatch):
        """The call site, which is where the defect was: ``spawn`` passed no env,
        so the agent inherited the crew user's HOME from PID 1."""
        monkeypatch.setenv("HOME", hooks.CREW_HOME)
        started: list[tuple] = []
        monkeypatch.setattr(hooks, "reset_machine_id", lambda: "id")
        monkeypatch.setattr(hooks, "register_agent", lambda *a, **k: None)
        monkeypatch.setattr(hooks, "read_managed_instance_id", lambda: "mi-0123456789abcdef0")
        monkeypatch.setattr(hooks, "boot_crew", lambda payload: None)
        monkeypatch.setattr(
            hooks,
            "spawn",
            lambda name, argv, env=None, **kw: started.append((name, argv, env)),
        )

        import json as _json

        hooks.handle_run(_json.dumps(_payload()).encode("utf-8"))

        agent = [row for row in started if row[0] == "ssm-agent"]
        assert agent, "the run hook did not start the agent"
        name, argv, env = agent[0]
        assert argv == [hooks.AGENT_BIN]
        assert env is not None, "the agent inherited this process's environment"
        assert env["HOME"] == hooks.AGENT_HOME


class TestTheImageGatesThatCredentialPath:
    """A mode is a claim until something tries it as the uid that must fail."""

    def test_the_image_creates_the_agent_home_root_only(self):
        code = _dockerfile_code()
        assert hooks.AGENT_HOME in code, "the image does not create the agent's home"
        assert f"chmod 0700 {hooks.AGENT_HOME}" in code
        assert f"chown -R root:root {hooks.AGENT_HOME}" in code

    def test_the_image_pre_creates_the_aws_directory_with_the_same_mode(self):
        """So the agent writes into a directory whose mode the image chose."""
        code = _dockerfile_code()
        assert f"mkdir -p {hooks.AGENT_HOME}/.aws" in code
        assert f"{hooks.AGENT_HOME}/.aws" in code.split("chmod 0700", 1)[1]

    @pytest.mark.parametrize("home", sorted(_CREDENTIAL_HOMES))
    @pytest.mark.parametrize("probe", ["test -x", "test -r"])
    def test_the_gate_asks_the_crew_user_whether_it_can_reach_every_candidate(self, home, probe):
        """Every directory the guest looks in, not just the one the agent is told
        to use: a program that takes its home from the passwd entry writes to
        ``/root`` whatever the environment says.

        Traverse and read are asked separately because they fail separately, and
        each is asked in a form where SUCCESS fails the build.
        """
        code = _dockerfile_code()
        line = f"sudo -u crew {probe} {home}"
        assert line in code, f"the image does not gate: {line}"
        after = code.split(line, 1)[1]
        assert "exit 1" in after[:200], "the gate does not fail the build on success"

    @pytest.mark.parametrize("home", sorted(_CREDENTIAL_HOMES))
    def test_the_image_makes_every_candidate_root_only(self, home):
        code = _dockerfile_code()
        assert f"chmod 0700 {home}" in code or re.search(
            rf"chmod 0700 [^;\n]*\b{re.escape(home)}\b", code
        ), f"{home} is not made root-only"

    def test_the_gate_asks_whether_the_crew_user_can_replace_the_credential(self):
        """Replacing the ``.aws`` directory needs write on the PARENT rather than
        on ``.aws``, so a mode on the credential file alone would not refuse it --
        which is the half of the finding a read test does not cover."""
        code = _dockerfile_code()
        assert f"echo x > {hooks.AGENT_HOME}/.aws/credentials" in code
        assert f"mv {hooks.AGENT_HOME}/.aws {hooks.AGENT_HOME}/.aws-crew" in code

    def test_the_gate_checks_the_mode_and_the_owner(self):
        code = _dockerfile_code()
        assert "root:root:700" in code
        assert code.count("root:root:700") >= 2, "only one of the two directories is checked"


class TestTheCrewInheritsAnAllowlistAndNotTheEnvironment:
    """PID 1 is root and holds the pointer to the node's credential; the crew is
    the one child that runs as another uid."""

    def test_the_pointer_to_the_node_credential_is_not_forwarded(self, monkeypatch):
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", f"{hooks.AGENT_HOME}/.aws/credentials")
        env = hooks.supervisor_env(_payload(), "ctrl", "identity")
        assert "AWS_SHARED_CREDENTIALS_FILE" not in env

    @pytest.mark.parametrize(
        "name",
        [
            "AWS_SESSION_TOKEN",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_PROFILE",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        ],
    )
    def test_no_aws_credential_name_is_forwarded(self, monkeypatch, name):
        monkeypatch.setenv(name, "value")
        assert name not in hooks.supervisor_env(_payload(), "ctrl", "identity")

    def test_a_name_nobody_anticipated_is_not_forwarded(self, monkeypatch):
        """The property an allowlist has and a denylist does not: a name this
        file never heard of does not reach the crew."""
        monkeypatch.setenv("SOME_FUTURE_NODE_TOKEN", "value")
        assert "SOME_FUTURE_NODE_TOKEN" not in hooks.supervisor_env(_payload(), "ctrl", "id")

    @pytest.mark.parametrize("name", ["PATH", "HOME", "LANG", "KIROCREW_HOME"])
    def test_what_any_child_needs_to_run_is_forwarded(self, monkeypatch, name):
        """Non-vacuity. A supervisor without PATH or HOME does not start, and the
        allowlist that forgets one is an allowlist that takes the crew down."""
        monkeypatch.setenv(name, "/some/value")
        env = hooks.supervisor_env(_payload(), "ctrl", "identity")
        assert env[name] == "/some/value"

    def test_the_containers_own_settings_contract_is_forwarded(self, monkeypatch):
        """The ``SMC_`` names are the image's and this module's to set, and the
        supervisor reads them through ``container.common.load``."""
        monkeypatch.setenv("SMC_DATA_HOME", "/var/lib/kirocrew")
        monkeypatch.setenv("SMC_BACKEND_PORT", "8765")
        env = hooks.supervisor_env(_payload(), "ctrl", "identity")
        assert env["SMC_DATA_HOME"] == "/var/lib/kirocrew"
        assert env["SMC_BACKEND_PORT"] == "8765"

    def test_the_guests_own_answers_are_still_final(self, monkeypatch):
        """The allowlist carries the ``SMC_`` prefix whole, so a value baked into
        the image arrives -- and these three lines are what make the guest's
        answer the one that lands."""
        monkeypatch.setenv("SMC_REQUIRE_AUTH_ALL_ROUTES", "0")
        monkeypatch.setenv("SMC_FRONT_BIND", "0.0.0.0")
        env = hooks.supervisor_env(_payload(), "ctrl-secret", "identity-doc")
        assert env["SMC_REQUIRE_AUTH_ALL_ROUTES"] == "1"
        assert env["SMC_FRONT_BIND"] == "127.0.0.1"
        assert env["SMC_CONTROL_SECRET"] == "ctrl-secret"
        assert env["KIRO_IDENTITY"] == "identity-doc"


class TestRunRefusesAPayloadThatIsNotThisVmsOwn:
    """``/run`` carries no credential, so the payload has to answer for itself."""

    def test_a_payload_the_lane_writes_is_accepted(self, monkeypatch):
        monkeypatch.setenv(hooks.EXPECTED_ACCOUNT_ENV, _ACCOUNT)
        monkeypatch.setenv(hooks.EXPECTED_REGION_ENV, _REGION)
        assert hooks.run_payload_verdict(_payload()) == (True, "ok")

    def test_a_payload_naming_another_account_is_refused(self, monkeypatch):
        monkeypatch.setenv(hooks.EXPECTED_ACCOUNT_ENV, _ACCOUNT)
        stranger = _payload(
            secretRef=f"arn:aws:secretsmanager:{_REGION}:999988887777:secret:kirocrew/crew/x-Ab",
            identityRef=f"arn:aws:secretsmanager:{_REGION}:999988887777:secret:kirocrew/crew/y-Cd",
        )
        allowed, verdict = hooks.run_payload_verdict(stranger)
        assert allowed is False
        assert verdict == "account-is-not-this-vm"

    def test_a_payload_naming_another_region_is_refused(self, monkeypatch):
        monkeypatch.setenv(hooks.EXPECTED_REGION_ENV, _REGION)
        allowed, verdict = hooks.run_payload_verdict(_payload(region="eu-west-1"))
        assert allowed is False
        assert verdict == "region-is-not-this-vm"

    def test_an_arn_in_another_region_is_refused_too(self, monkeypatch):
        """The payload's ``region`` field is not the only place a region appears,
        and the secret the guest reads is fetched in the ARN's region."""
        monkeypatch.setenv(hooks.EXPECTED_REGION_ENV, _REGION)
        mixed = _payload(
            identityRef=f"arn:aws:secretsmanager:eu-west-1:{_ACCOUNT}:secret:kirocrew/crew/y-Cd"
        )
        assert hooks.run_payload_verdict(mixed)[0] is False

    def test_a_payload_naming_two_accounts_is_refused_without_any_pin(self, monkeypatch):
        """The check that needs no build-time value: the lane writes both secrets
        in one account, so a mixture is a payload assembled from two launches."""
        monkeypatch.delenv(hooks.EXPECTED_ACCOUNT_ENV, raising=False)
        monkeypatch.delenv(hooks.EXPECTED_REGION_ENV, raising=False)
        mixed = _payload(
            identityRef=f"arn:aws:secretsmanager:{_REGION}:999988887777:secret:kirocrew/crew/y-Cd"
        )
        allowed, verdict = hooks.run_payload_verdict(mixed)
        assert allowed is False
        assert verdict == "payload-names-two-accounts"

    def test_a_payload_naming_two_regions_is_refused_without_any_pin(self, monkeypatch):
        monkeypatch.delenv(hooks.EXPECTED_ACCOUNT_ENV, raising=False)
        monkeypatch.delenv(hooks.EXPECTED_REGION_ENV, raising=False)
        mixed = _payload(
            identityRef=f"arn:aws:secretsmanager:eu-west-1:{_ACCOUNT}:secret:kirocrew/crew/y-Cd"
        )
        assert hooks.run_payload_verdict(mixed) == (False, "payload-names-two-regions")

    def test_a_bare_secret_name_is_not_a_mismatch(self, monkeypatch):
        """The launcher may send a name rather than an ARN, and a check that
        refused names would refuse every launch that uses them."""
        monkeypatch.delenv(hooks.EXPECTED_ACCOUNT_ENV, raising=False)
        monkeypatch.setenv(hooks.EXPECTED_REGION_ENV, _REGION)
        named = _payload(secretRef="kirocrew/crew/kc-a1b2c3", identityRef="kirocrew/crew/identity")
        assert hooks.run_payload_verdict(named)[0] is True

    def test_an_image_with_no_pin_still_accepts_its_own_launch(self, monkeypatch):
        """An image built without the two values keeps working: the pin is the
        stronger check, not the only one."""
        monkeypatch.delenv(hooks.EXPECTED_ACCOUNT_ENV, raising=False)
        monkeypatch.delenv(hooks.EXPECTED_REGION_ENV, raising=False)
        assert hooks.run_payload_verdict(_payload())[0] is True

    def test_a_refused_run_registers_nothing_and_writes_nothing(self, guest_state, monkeypatch):
        import json as _json

        monkeypatch.setenv(hooks.EXPECTED_ACCOUNT_ENV, _ACCOUNT)
        calls: list[str] = []

        def _reset_machine_id() -> str:
            calls.append("machine-id")
            return "x"

        def _register_agent(*_a: object, **_k: object) -> None:
            calls.append("register")

        monkeypatch.setattr(hooks, "reset_machine_id", _reset_machine_id)
        monkeypatch.setattr(hooks, "register_agent", _register_agent)
        monkeypatch.setattr(hooks, "spawn", lambda *a, **k: calls.append("spawn"))
        monkeypatch.setattr(hooks, "boot_crew", lambda payload: calls.append("boot"))

        stranger = _payload(
            secretRef=f"arn:aws:secretsmanager:{_REGION}:999988887777:secret:kirocrew/crew/x-Ab",
            identityRef=f"arn:aws:secretsmanager:{_REGION}:999988887777:secret:kirocrew/crew/y-Cd",
        )
        answer = hooks.handle_run(_json.dumps(stranger).encode("utf-8"))

        assert calls == [], f"a refused payload still acted: {calls}"
        assert answer == {"ok": True, "repeat": False}
        assert not (guest_state / "payload-seen.json").exists()

    def test_a_refused_run_does_not_consume_the_single_shot_marker(self, guest_state, monkeypatch):
        """So the owner's real launch, arriving after a stranger's, still boots.
        A refusal that burned the marker would be a denial of service with one
        unauthenticated request."""
        import json as _json

        monkeypatch.setenv(hooks.EXPECTED_ACCOUNT_ENV, _ACCOUNT)
        monkeypatch.setattr(hooks, "reset_machine_id", lambda: "x")
        monkeypatch.setattr(hooks, "register_agent", lambda *a, **k: None)
        monkeypatch.setattr(hooks, "read_managed_instance_id", lambda: "mi-0")
        monkeypatch.setattr(hooks, "boot_crew", lambda payload: None)
        booted: list[str] = []
        monkeypatch.setattr(hooks, "spawn", lambda name, *a, **k: booted.append(name))

        stranger = _payload(
            secretRef=f"arn:aws:secretsmanager:{_REGION}:999988887777:secret:kirocrew/crew/x-Ab",
            identityRef=f"arn:aws:secretsmanager:{_REGION}:999988887777:secret:kirocrew/crew/y-Cd",
        )
        hooks.handle_run(_json.dumps(stranger).encode("utf-8"))
        assert not (guest_state / "run.done").exists()

        hooks.handle_run(_json.dumps(_payload()).encode("utf-8"))
        assert booted == ["ssm-agent"]
        assert (guest_state / "run.done").exists()

    def test_the_refusal_answers_what_a_first_call_and_a_repeat_answer(
        self, guest_state, monkeypatch
    ):
        """The listener cannot authenticate its caller, so a refusal that looked
        different from a repeat would tell a stranger which of the two they hit.

        The same shape, with the same keys, for all three. What the HTTP route
        puts on the wire is narrower still: see
        ``test_the_run_route_answers_the_same_whatever_the_guest_decided``.
        """
        import json as _json

        monkeypatch.setenv(hooks.EXPECTED_REGION_ENV, _REGION)
        monkeypatch.setattr(hooks, "reset_machine_id", lambda: "x")
        monkeypatch.setattr(hooks, "register_agent", lambda *a, **k: None)
        monkeypatch.setattr(hooks, "read_managed_instance_id", lambda: "mi-0")
        monkeypatch.setattr(hooks, "boot_crew", lambda payload: None)
        monkeypatch.setattr(hooks, "spawn", lambda *a, **k: None)

        good = _json.dumps(_payload()).encode("utf-8")
        first = hooks.handle_run(good)
        repeat = hooks.handle_run(good)
        refused = hooks.handle_run(_json.dumps(_payload(region="eu-west-1")).encode("utf-8"))

        assert set(first) == set(repeat) == set(refused)
        assert first["ok"] is repeat["ok"] is refused["ok"] is True

    def test_the_refusal_log_carries_no_payload_value(self, guest_state, monkeypatch):
        """The verdict reaches a log, and this module's logs carry no
        caller-chosen content."""
        import json as _json

        monkeypatch.setenv(hooks.EXPECTED_ACCOUNT_ENV, _ACCOUNT)
        lines: list[dict] = []
        monkeypatch.setattr(
            hooks, "log", lambda event, **fields: lines.append({**fields, "e": event})
        )
        stranger = _payload(
            secretRef=f"arn:aws:secretsmanager:{_REGION}:999988887777:secret:kirocrew/crew/x-Ab",
            identityRef=f"arn:aws:secretsmanager:{_REGION}:999988887777:secret:kirocrew/crew/y-Cd",
        )
        hooks.handle_run(_json.dumps(stranger).encode("utf-8"))
        text = _json.dumps(lines)
        assert "999988887777" not in text
        assert "code-value" not in text


def _ask(port: int, raw: bytes) -> tuple[bytes, dict[str, str], bytes]:
    """One raw request to the listener, read to EOF. Returns status, headers, body."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(raw)
        sock.settimeout(5)
        chunks = []
        while True:
            try:
                piece = sock.recv(4096)
            except socket.timeout:  # pragma: no cover - a reply that never ends
                break
            if not piece:
                break
            chunks.append(piece)
    reply = b"".join(chunks)
    if not reply.startswith(b"HTTP/"):
        # A request line the stdlib could not parse leaves ``request_version`` at
        # HTTP/0.9, where it writes no status line and no headers at all. The body
        # is the whole reply, and the body is what this file is about.
        return b"", {}, reply
    head, _, body = reply.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = lines[0] if lines else b""
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return status, headers, body


class TestEveryReplyIsAConstant:
    """Including the ones ``http.server`` writes when no handler is reached."""

    @pytest.fixture()
    def port(self):
        server = hooks.HookServer(("127.0.0.1", 0), hooks.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @pytest.mark.parametrize(
        "raw",
        [
            b"GET /does-not-exist HTTP/1.1\r\nHost: g\r\nConnection: close\r\n\r\n",
            b"OPTIONS * HTTP/1.1\r\nHost: g\r\nConnection: close\r\n\r\n",
            b"OPTIONS /aws/lambda-microvms/runtime/v1/run HTTP/1.1\r\n"
            b"Host: g\r\nConnection: close\r\n\r\n",
            b"PATCH /aws/lambda-microvms/runtime/v1/run HTTP/1.1\r\n"
            b"Host: g\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            b"TRACE / HTTP/1.1\r\nHost: g\r\nConnection: close\r\n\r\n",
            b"BREW /coffee HTTP/1.1\r\nHost: g\r\nConnection: close\r\n\r\n",
            b"PUT /crews HTTP/1.1\r\nHost: g\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            b"DELETE /crews HTTP/1.1\r\nHost: g\r\nConnection: close\r\n\r\n",
        ],
    )
    def test_the_body_and_the_status_are_the_one_refusal(self, port, raw):
        status, headers, body = _ask(port, raw)
        assert status.startswith(b"HTTP/1.1 404"), status
        assert body == b'{"ok": false}', body
        assert headers.get("content-type") == "application/json", headers
        assert "server" not in headers, headers

    def test_a_malformed_request_line_gets_the_same_body(self, port):
        """``http.server``'s own answer here is an HTML page naming the reason,
        which tells a caller a malformed request from an unimplemented one."""
        _, _, body = _ask(port, b"NOT-A-REQUEST-LINE\r\n\r\n")
        assert body == b'{"ok": false}', body

    def test_an_unsupported_version_gets_the_same_body(self, port):
        _, _, body = _ask(port, b"GET / HTTP/9.9\r\n\r\n")
        assert body == b'{"ok": false}', body

    def test_the_run_route_answers_the_same_whatever_the_guest_decided(self, port, monkeypatch):
        """A bootstrap, a repeat and a refused payload are one body on the wire.

        ``handle_run`` returns the guest's own verdict, which the log and this
        file's own tests read. Putting it in the reply would tell a caller who
        cannot be authenticated whether a crew is already up.
        """
        request = (
            b"POST /aws/lambda-microvms/runtime/v1/run HTTP/1.1\r\n"
            b"Host: g\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
        )
        bodies = set()
        for verdict in (
            {"ok": True, "repeat": False},
            {"ok": True, "repeat": True},
            {"ok": True, "repeat": False, "refused": "account-is-not-this-vm"},
        ):
            monkeypatch.setattr(hooks, "handle_run", lambda body, answer=verdict: answer)
            status, _, body = _ask(port, request)
            assert status.startswith(b"HTTP/1.1 200"), status
            bodies.add(body)
        assert bodies == {b'{"ok": true}'}, bodies

    def test_no_reply_names_the_interpreter(self, port):
        """The stdlib default reads ``BaseHTTP/0.6 Python/3.12.x``."""
        for raw in (
            b"OPTIONS / HTTP/1.1\r\nHost: g\r\nConnection: close\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: g\r\nConnection: close\r\n\r\n",
        ):
            _, headers, body = _ask(port, raw)
            assert "server" not in headers
            assert b"Python" not in body

    def test_the_constant_is_one_value(self):
        """Read off the class rather than restated, so a handler that answers
        something else fails the parametrised test above rather than passing a
        copy of the old body."""
        assert hooks.Handler.CONSTANT_REFUSAL == {"ok": False}


class TestTheAgentPackageIsPinnedAndChecked:
    def test_the_version_is_pinned_and_the_url_uses_it(self):
        code = _dockerfile_code()
        version = re.search(r"ARG SSM_AGENT_VERSION=([0-9]+(?:\.[0-9]+)+)", code)
        assert version, "the SSM agent version is not pinned"
        assert f"/{version.group(1)}/debian_arm64/" in code or (
            "${SSM_AGENT_VERSION}/debian_arm64/" in code
        )

    def test_the_moving_object_is_not_what_is_fetched(self):
        """``latest/`` is whatever the bucket holds at build time, inside an image
        addressed by a digest that does not change when the bucket does."""
        assert "/latest/" not in _dockerfile_code()

    def test_the_download_is_checksummed(self):
        code = _dockerfile_code()
        digest = re.search(r"ARG SSM_AGENT_SHA256=([0-9a-f]{64})", code)
        assert digest, "no sha256 is pinned for the agent package"
        assert "sha256sum -c" in code, "the pinned digest is never checked"
        assert "${SSM_AGENT_SHA256}" in code

    def test_the_installed_binary_is_checked_against_the_pin(self):
        """The digest covers the package; this covers what came out of it."""
        code = _dockerfile_code()
        assert 'amazon-ssm-agent -version | grep -F "${SSM_AGENT_VERSION}"' in code


class TestTheLaneStackScopesWhatTheGuestMayDo:
    """The grants behind the credential the blocking finding is about."""

    @staticmethod
    def _template() -> str:
        if not _TEMPLATE.exists():  # pragma: no cover - an installed image has no template
            pytest.skip(f"{_TEMPLATE.name} is not in this tree")
        return _TEMPLATE.read_text(encoding="utf-8")

    @classmethod
    def _resource(cls, name: str) -> str:
        block = re.search(rf"^  {name}:\n(.*?)(?=^  \w|\Z)", cls._template(), re.S | re.M)
        assert block, f"{name} is not in the template"
        return block.group(1)

    @staticmethod
    def _code(body: str) -> str:
        return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))

    @pytest.mark.parametrize("role", ["HybridActivationRole", "ExecutionRole"])
    def test_the_key_grant_only_works_through_secrets_manager(self, role):
        """Bare ``kms:Decrypt`` on the lane key decrypts ANY ciphertext made under
        it, including one handed to the guest rather than one it fetched."""
        body = self._code(self._resource(role))
        assert "kms:Decrypt" in body
        assert "kms:ViaService" in body, f"{role} holds an unconditioned decrypt"
        assert "secretsmanager." in body.split("kms:ViaService", 1)[1]

    def test_the_deregister_grant_names_this_lanes_own_nodes(self):
        body = self._code(self._resource("HybridActivationRole"))
        assert "ssm:DeregisterManagedInstance" in body
        after = body.split("ssm:DeregisterManagedInstance", 1)[1]
        assert "managed-instance/*" in after
        assert 'Resource: "*"' not in body, "the guest may deregister anything SSM manages"

    def test_the_deregister_grant_is_conditioned_on_the_lanes_own_tag(self):
        """``engine.create_activation`` tags every activation it mints
        ``kirocrew:managed=true``, and SSM applies an activation's tags to the
        node registered through it."""
        body = self._code(self._resource("HybridActivationRole"))
        assert "aws:ResourceTag/kirocrew:managed" in body

    def test_the_secret_grant_stays_scoped_to_the_lanes_prefix(self):
        """Non-vacuity for the KMS condition above: what bounds WHICH secret the
        guest may decrypt is this grant, so the two belong to one bound."""
        body = self._code(self._resource("HybridActivationRole"))
        assert "secretsmanager:GetSecretValue" in body
        assert "secret:${Prefix}/*" in body

    def test_the_ssm_trust_cannot_be_used_by_another_account(self):
        """The principal is a service and the thing that asks it to assume this
        role is an activation, which anyone holding ``ssm:CreateActivation`` can
        make."""
        body = self._code(self._resource("HybridActivationRole"))
        trust = body.split("ManagedPolicyArns", 1)[0]
        assert "aws:SourceAccount" in trust, "the ssm trust has no account condition"
        assert "aws:SourceArn" in trust, "the ssm trust has no source-arn condition"
        assert "AWS::AccountId" in trust.split("aws:SourceAccount", 1)[1]
