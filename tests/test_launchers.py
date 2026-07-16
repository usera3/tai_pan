from pathlib import Path


def test_launchers_bind_only_to_fixed_loopback_address():
    batch = Path("start.bat").read_text(encoding="utf-8")
    shell = Path("scripts/start.sh").read_text(encoding="utf-8")
    powershell = Path("scripts/wait-and-open.ps1").read_text(encoding="utf-8")

    combined = "\n".join((batch, shell, powershell))
    assert "127.0.0.1" in combined
    assert "8765" in combined
    assert "0.0.0.0" not in combined
    assert "uvicorn app.main:app" in shell
    assert "/health" in powershell


def test_runtime_secrets_and_dependencies_are_ignored():
    ignored = Path(".gitignore").read_text(encoding="utf-8")

    assert ".local/" in ignored
    assert ".venv/" in ignored
    assert "node_modules/" in ignored
