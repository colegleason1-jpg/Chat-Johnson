"""The capability card states features, boundaries, and roadmap from the code's own tables."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import capabilities, connectors


def test_capability_card_names_every_feature_boundary_and_roadmap_item():
    card = capabilities.capability_card()
    for name, _ in capabilities.IMPLEMENTED:
        assert f"- {name}:" in card
    for boundary in capabilities.BOUNDARIES:
        assert boundary in card
    for name, status, _ in connectors.ROADMAP_FEATURES:
        assert f"- {name} ({status})" in card
    assert "never writes to GitHub on its own" in card and "session-only GitHub push slot" in card
    assert "never invent executors" in card
    assert len(card) < 3000  # a few hundred tokens per request, not a document
