import sys
import os

import pytest

# Add project root and src/nl2infra to sys.path
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
src_dir = os.path.join(root_dir, "src", "nl2infra")

for p in [root_dir, src_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _isolate_events(tmp_path, monkeypatch):
    """Point the notifications store at a per-test file so emits never pollute a shared log."""
    monkeypatch.setenv("NL2INFRA_EVENTS_FILE", str(tmp_path / "events.jsonl"))
    import notifications
    notifications.reset_for_tests()
    yield
    notifications.reset_for_tests()
