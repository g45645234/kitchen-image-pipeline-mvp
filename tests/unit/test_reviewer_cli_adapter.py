import json
import shlex
import sys

from app.commands.reviewer_cli_adapter import build_prompt, run_backend


def test_build_prompt_contains_contract_and_payload():
    payload = {"candidate": {"id": 1}, "mistake": {"title": "Bad lighting"}}

    prompt = build_prompt("codex", payload)

    assert "respond as a single valid JSON object" in prompt
    assert "Do not copy placeholder values" in prompt
    assert '"reviewer_name"' in prompt
    assert '"id": 1' in prompt
    assert "Bad lighting" in prompt


def test_run_backend_passes_prompt_via_stdin():
    script = "import sys; print(sys.stdin.read().upper())"
    command = f"{sys.executable} -c {shlex.quote(script)}"

    output = run_backend(command, "hello", prompt_as_arg=False, timeout=5)

    assert output == "HELLO"


def test_run_backend_can_pass_prompt_as_arg():
    script = 'import sys, json; print(json.dumps({"arg": sys.argv[1]}))'
    command = f"{sys.executable} -c {shlex.quote(script)}"

    output = run_backend(command, "hello", prompt_as_arg=True, timeout=5)

    assert json.loads(output) == {"arg": "hello"}



def test_run_backend_timeout_kills_process_group(monkeypatch):
    import subprocess
    import app.commands.reviewer_cli_adapter as adapter

    killed = []

    class FakeProcess:
        pid = 12345
        returncode = None

        def __init__(self):
            self.calls = 0

        def communicate(self, input=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(cmd=["fake"], timeout=timeout)
            return b"", b""

    fake_process = FakeProcess()
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(adapter.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    try:
        adapter.run_backend("fake-cli", "prompt", prompt_as_arg=False, timeout=1)
    except TimeoutError as e:
        assert "timed out" in str(e)
    else:
        raise AssertionError("run_backend should raise TimeoutError")

    assert killed == [(12345, adapter.signal.SIGKILL)]
