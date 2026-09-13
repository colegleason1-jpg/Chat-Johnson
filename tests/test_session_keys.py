"""Session-only BYOK overlay: precedence, redaction, and clearing."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import config, router


@pytest.fixture(autouse=True)
def clean_overlay():
    config.clear_session_keys()
    yield
    config.clear_session_keys()


def test_overlay_takes_precedence_over_environment(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert config.resolve_secret("GROQ_API_KEY") == "from-env"
    config.set_session_key("GROQ_API_KEY", "  from-ui  ")
    assert config.resolve_secret("GROQ_API_KEY") == "from-ui"
    assert config.provider_api_key(config.PROVIDERS["groq"]) == "from-ui"
    config.set_session_key("GROQ_API_KEY", "")
    assert config.resolve_secret("GROQ_API_KEY") == "from-env"


def test_router_sees_ui_keys_without_environment(monkeypatch):
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert not router.cortex_available()
    config.set_session_key("GROQ_API_KEY", "ui-groq")
    assert router.cortex_available()
    assert router.byok_status()["groq"] == {"env_key": "GROQ_API_KEY", "configured": True}
    assert "ui-groq" not in str(router.byok_status())
    zero = {name: 0.0 for name in router.CORTEX_ENDPOINTS}
    assert router.select_milp_endpoint("chat", 100, entropy_by_endpoint=zero).endpoint.name == "groq"


def test_clear_removes_everything(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config.set_session_key("GEMINI_API_KEY", "x")
    config.set_session_key("HF_TOKEN", "y")
    config.clear_session_keys()
    assert config.resolve_secret("GEMINI_API_KEY") == ""
    assert config.SESSION_KEYS == {}
