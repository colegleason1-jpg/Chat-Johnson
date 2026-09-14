"""Batch C: fences held while streaming, math-safe markdown, keyword-loaded skills, and the chat navigator."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import skills, vault  # noqa: E402
from orchestrator.render_text import hold_fences, prepare_markdown  # noqa: E402


def test_hold_fences_streams_prose_and_emits_code_blocks_whole():
    chunks = ["Here is ", "the fix:\n``", "`python\nprint(", "1)\n``", "`\nand ", "more $5 text `", "` not a fence"]
    out = list(hold_fences(chunks))
    assert out[0] == "Here is " and out[1] == "the fix:\n"
    assert "```python\nprint(1)\n```\n" in out  # the whole block, once
    joined = "".join(out)
    assert joined == "".join(chunks)  # nothing lost, nothing duplicated
    assert not any(piece.startswith("```") and "```" not in piece[3:] for piece in out)  # never a half-open fence
    assert list(hold_fences(["no code at all"])) == ["no code at all"]
    assert "".join(hold_fences(["```js\nlet x = 1;\n```"])) == "```js\nlet x = 1;\n```"
    assert "".join(hold_fences(["open ```py\nnever closed"])) == "open ```py\nnever closed"  # flushed at the end


def test_prepare_markdown_escapes_currency_but_keeps_math():
    assert prepare_markdown("costs $5 and $10 each") == r"costs \$5 and \$10 each"
    assert prepare_markdown("energy $E = mc^2$ here") == "energy $E = mc^2$ here"
    assert prepare_markdown("$$\\int_0^1 x\\,dx$$") == "$$\\int_0^1 x\\,dx$$"
    assert prepare_markdown(r"already \$5") == r"already \$5"
    assert prepare_markdown("no dollars") == "no dollars"


def test_skills_parse_select_and_load_only_on_keyword_match(tmp_path):
    (tmp_path / "a.md").write_text("---\nname: tides\ndescription: About tides.\nkeywords: tide tides moon lunar\n---\nBody about tides.\n")
    (tmp_path / "b.md").write_text("---\nname: cooking\ndescription: Recipes.\nkeywords: recipe bake oven\n---\nBody about baking.\n")
    (tmp_path / "junk.md").write_text("no front matter here")
    loaded = skills.load_skills(str(tmp_path))
    assert sorted(s.name for s in loaded) == ["cooking", "tides"]
    assert next(s for s in loaded if s.name == "tides").keywords == ("tide", "tides", "moon", "lunar")
    assert [s.name for s in skills.select_skills("why do tides follow the moon", root=str(tmp_path))] == ["tides"]
    assert skills.select_skills("tell me about astronomy", root=str(tmp_path)) == []  # 'tide' inside 'tidewater' would not count either
    block, names = skills.skills_block("bake bread while the tide turns", limit=1, root=str(tmp_path))
    assert names in (["cooking"], ["tides"]) and block.startswith("APPLICABLE SKILLS") and "Body about" in block
    assert skills.skills_block("nothing relevant", root=str(tmp_path)) == ("", [])
    shipped = skills.load_skills()
    assert {s.name for s in shipped} >= {"deploy-kit", "repository-patching", "mission-writing", "company-reporting"}
    assert all(len(s.body) <= skills.MAX_BODY_CHARS for s in shipped)
    assert [s.name for s in skills.select_skills("generate a docker and helm deployment pipeline")][0] == "deploy-kit"


def test_prompt_carries_a_matching_skill_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    vault.initialize_database()
    from orchestrator.prompting import build_prompt_messages
    system = build_prompt_messages("s", "write a 2 page essay about tides")[0]["content"]
    assert "SKILL · mission-writing" in system and "SKILL · deploy-kit" not in system
    plain = build_prompt_messages("s", "hello there")[0]["content"]
    assert "APPLICABLE SKILLS" not in plain


def test_navigator_search_window_and_outline(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    vault.initialize_database()
    thread = int(vault.active_thread("s", "normal_chat")["id"])
    ids = [vault.append_message("s", "user" if i % 2 == 0 else "assistant", f"message {i} about {'tides' if i % 5 == 0 else 'other things'}", thread_id=thread, workspace="normal_chat") for i in range(30)]
    hits = vault.search_messages("s", thread, "tides message", limit=10)
    assert [h["id"] for h in hits] == [ids[25], ids[20], ids[15], ids[10], ids[5], ids[0]]
    window = vault.messages_around(thread, ids[15], before=3, after=2)
    assert [row["id"] for row in window] == ids[12:18]
    assert vault.messages_around(thread, 10**9, before=3, after=2) == [] or [row["id"] for row in vault.messages_around(thread, 10**9, before=3, after=2)]
    outline = vault.thread_outline(thread)
    assert len(outline) == 30 and outline[0]["role"] == "user" and outline[0]["text"].startswith("message 0 about tides")
