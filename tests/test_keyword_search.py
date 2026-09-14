"""Keyword search: every word must match in any order; best matches first; SQL clauses."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.keyword_search import keyword_rank, keywords, like_clauses


def test_rank_requires_every_keyword_in_any_order_and_prefers_word_starts():
    repos = ["Chazzzer/Chat-Johnson", "Chazzzer/johnson-notes", "other/chatter", "Chazzzer/deploy-kit"]
    assert keyword_rank("johnson chat", repos) == ["Chazzzer/Chat-Johnson"]
    assert keyword_rank("notes johnson", repos) == ["Chazzzer/johnson-notes"]
    assert keyword_rank("johnson", repos) == ["Chazzzer/johnson-notes", "Chazzzer/Chat-Johnson"]  # earlier word-start match first
    assert keyword_rank("chat", repos)[0] == "Chazzzer/Chat-Johnson"  # word-start match beats the substring in "chatter"
    assert keyword_rank("", repos, limit=2) == repos[:2]
    assert keyword_rank("nothing", repos) == []
    assert keywords("  Chat-Johnson, v2 ") == ["chat", "johnson", "v2"]


def test_like_clauses_and_two_keywords_across_fields():
    clause, params = like_clauses("router utf8", ("name", "file_path"))
    assert clause == "(name LIKE ? OR file_path LIKE ?) AND (name LIKE ? OR file_path LIKE ?)"
    assert params == ["%router%", "%router%", "%utf8%", "%utf8%"]
    assert like_clauses("", ("name",)) == ("1=1", [])
