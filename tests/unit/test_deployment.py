from pathlib import Path


def test_systemd_template_uses_current_repository_directory():
    root = Path(__file__).parents[2]
    service = (root / "deploy/systemd/showroom-guide.service").read_text()

    assert "WorkingDirectory=%h/showroom-guide" in service
    assert "ExecStart=%h/showroom-guide/.venv/bin/python" in service
    assert "xzinfra-voice-guide" not in service
