#!/usr/bin/env python3
"""Install / remove the macOS login service that runs `meetings.py watch` (launchd, stdlib only).

  python3 tools/install_service.py            write ~/Library/LaunchAgents/com.meetings-assist.plist and load it
  python3 tools/install_service.py --uninstall

The log goes to ~/Library/Logs/meetings-assist.log (launchd cannot open logs on external volumes; the scripts
themselves may live anywhere). Needs `uv` on PATH.
"""
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.meetings-assist"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / "meetings-assist.log"
ROOT = Path(__file__).resolve().parent.parent


def launchctl(*args: str) -> None:
    subprocess.run(["launchctl", *args], check=False)


if "--uninstall" in sys.argv:
    launchctl("unload", str(PLIST))
    PLIST.unlink(missing_ok=True)
    print(f"removed {PLIST}")
    sys.exit(0)

uv = shutil.which("uv")
if not uv:
    sys.exit("uv not found on PATH (brew install uv)")
LOG.parent.mkdir(parents=True, exist_ok=True)
plist = {
    "Label": LABEL,
    "ProgramArguments": [uv, "run", "-q", str(ROOT / "meetings.py"), "watch"],
    "WorkingDirectory": str(Path.home()),
    "EnvironmentVariables": {"PATH": f"{Path(uv).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": str(LOG),
    "StandardErrorPath": str(LOG),
}
launchctl("unload", str(PLIST))
PLIST.write_bytes(plistlib.dumps(plist))
launchctl("load", str(PLIST))
print(f"installed {PLIST}\nlog: {LOG}")
