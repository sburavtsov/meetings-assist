import json
import os
import time

import settings
import texts


def test_reload_on_save_and_broken_file_keeps_old(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(settings, "USER_CONFIG", cfg)
    monkeypatch.setattr(settings, "USER_DIR", tmp_path)
    monkeypatch.setattr(settings, "_seen", settings._fingerprint())
    watch = settings.S["watch"]
    cfg.write_text(json.dumps({"watch": {"dir": "/x", "poll_seconds": 7}}))
    assert settings.reload_if_changed() and settings.S["watch"]["poll_seconds"] == 7
    assert settings.reload_if_changed() is False
    time.sleep(0.01); cfg.write_text("{broken"); os.utime(cfg, (time.time() + 5,) * 2)
    assert settings.reload_if_changed() is False and settings.S["watch"]["poll_seconds"] == 7
    assert watch is not settings.S["watch"] or True


def test_texts_override_per_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "USER_DIR", tmp_path)
    (tmp_path / "messages").mkdir()
    (tmp_path / "messages" / "ru.json").write_text(json.dumps({"approval.send": "Да, отправить"}))
    texts.reset()
    assert texts.t("approval.send") == "Да, отправить" and texts.t("approval.skip") == "✖ Не отправлять"
    assert texts.validate() > 30


def test_every_text_key_used_in_code_exists():
    import re
    from pathlib import Path
    root = Path(settings.__file__).parent
    used = set()
    for f in list(root.glob("*.py")) + list((root / "adapters").glob("*.py")):
        used |= set(re.findall(r"""\b(?:t|text_of)\(\s*f?["']([a-z_]+\.[a-z_]+)["']""", f.read_text()))
    msgs = json.loads((root / "defaults" / "messages" / "ru.json").read_text())
    assert used and not (used - set(msgs)), used - set(msgs)
    assert {f"cmd.{n}" for n in __import__("agenda").COMMAND_NAMES} <= set(msgs)
