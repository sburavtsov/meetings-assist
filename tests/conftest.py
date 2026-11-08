"""Offline tests: no network, no API keys, never your real ~/.meetings-assist (everything points into a temp dir)."""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_HOME = Path(tempfile.mkdtemp(prefix="meetings-assist-tests-"))
(_HOME / "rec").mkdir()
(_HOME / "config.json").write_text(json.dumps({"watch": {"dir": str(_HOME / "rec")}}))
os.environ["MEETINGS_ASSIST_CONFIG"] = str(_HOME / "config.json")
os.environ["MEETINGS_ASSIST_SECRETS"] = str(_HOME / "secrets.json")
os.environ["MEETINGS_ASSIST_STATE"] = str(_HOME / "state.db")
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import settings  # noqa: E402
import texts  # noqa: E402

_PRISTINE = copy.deepcopy(settings.S)


@pytest.fixture(autouse=True)
def fresh_settings():
    """Every test starts from the default settings (tests change them freely)."""
    settings.S.clear()
    settings.S.update(copy.deepcopy(_PRISTINE))
    texts.reset()
    yield


@pytest.fixture
def store(tmp_path):
    from store import Store
    return Store(tmp_path / "state.db")


class FakeAI:
    """AIAdapter stand-in: records prompts; generate_json returns `points` (or raises `error`)."""
    name = "fake"

    def __init__(self, points=None, error=None):
        self.prompts, self.plans, self.points, self.error, self.result = [], [], points, error, {}

    def generate_json(self, prompt, schema):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return {"points": self.points if self.points is not None else []}

    def analyze(self, audio, speakers, context, plan=None):
        self.plans.append(plan)
        return json.loads(json.dumps(self.result))


@pytest.fixture
def fake_ai():
    return FakeAI()


def msg(uid, text, name="N"):
    from adapters.base import IncomingMessage
    return IncomingMessage(uid, name, text, "chat" + uid)
