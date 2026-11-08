import subprocess
import sys
import time

import meetings
import settings


def test_open_file_is_not_finished(tmp_path):
    p = tmp_path / "rec.mkv"; p.write_bytes(b"x" * 100)
    old = time.time() - 600
    import os; os.utime(p, (old, old))
    holder = subprocess.Popen([sys.executable, "-c", f"f=open({str(p)!r}, 'rb'); import time; time.sleep(30)"])
    try:
        for _ in range(50):
            if meetings.is_open(p):
                break
            time.sleep(0.1)
        assert meetings.is_stable(p) is False                          # OBS paused: old mtime, still open
        settings.S["watch"]["skip_open_files"] = False
        assert meetings.is_stable(p) is True
        settings.S["watch"]["skip_open_files"] = True
    finally:
        holder.kill(); holder.wait()
    assert meetings.is_stable(p) is True
    p.touch()
    assert meetings.is_stable(p) is False                              # just written


def test_poll_failures_are_logged_once(capsys):
    class Svc:
        class M:
            msg = "handshake timed out"
            def poll(self, want_messages=False, wait=0):
                raise RuntimeError(Svc.M.msg)
        messenger = M()
    for _ in range(5):
        assert meetings.poll_inbox(Svc()) is False
    Svc.M.msg = "502"; meetings.poll_inbox(Svc()); meetings.poll_inbox(Svc())
    lines = [x for x in capsys.readouterr().out.splitlines() if "failed" in x]
    assert len(lines) == 2


def test_watch_settings_follow_a_reload():
    settings.S["watch"]["poll_seconds"] = 99
    assert meetings.W()["poll_seconds"] == 99
