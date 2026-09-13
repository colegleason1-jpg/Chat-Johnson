"""Stateless, signed OAuth state that survives the redirect."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.github_auth import mint_state, verify_state


def test_state_round_trips_without_session_memory():
    state = mint_state("client-secret", now=1_000_000)
    assert verify_state("client-secret", state, now=1_000_100) == (True, "ok")


def test_state_rejects_tampering_wrong_secret_and_expiry():
    state = mint_state("client-secret", now=1_000_000)
    nonce, stamp, sig = state.split(".")
    assert verify_state("client-secret", f"{nonce}x.{stamp}.{sig}", now=1_000_001)[0] is False
    assert verify_state("other-secret", state, now=1_000_001)[0] is False
    assert verify_state("client-secret", state, now=1_000_000 + 601) == (False, "state expired")
    assert verify_state("client-secret", "garbage", now=1_000_001)[0] is False
    assert verify_state("", state, now=1_000_001)[0] is False
