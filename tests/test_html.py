"""HTML bodies to text.

Untested until now because the corpus is entirely plain text. The structured API
mode accepts `body_html`, so this is where the gap closes.
"""

from src.domain.preprocessing.html import html_to_text
from src.domain.preprocessing.raw_email import body_to_text


def test_tags_are_dropped_and_blocks_become_lines() -> None:
    text = html_to_text("<html><body><p>Hello</p><p>World</p></body></html>")
    assert text == "Hello\nWorld"


def test_a_fragment_without_a_body_tag_still_works() -> None:
    """Outlook often sends a bare div, not a whole document."""
    assert html_to_text("<div>Dear Sir<br>please quote</div>") == "Dear Sir\nplease quote"


def test_script_style_and_head_never_reach_the_model() -> None:
    """Stylesheet text inside a prompt is noise at best and injection at worst."""
    html = "<head><title>T</title><style>p{color:red}</style></head><body><script>x()</script><p>Real</p></body>"
    assert html_to_text(html) == "Real"


def test_plain_text_passes_through_unharmed() -> None:
    assert html_to_text("no tags at all") == "no tags at all"


def test_empty_input_gives_empty_output() -> None:
    assert html_to_text("") == ""


# --------------------------------------------------------------------------- #
# Choosing between the two body parts
# --------------------------------------------------------------------------- #


def test_the_plain_part_wins_when_both_are_present() -> None:
    assert body_to_text("plain wins", "<p>html loses</p>") == "plain wins"


def test_html_is_used_when_the_plain_part_is_blank() -> None:
    """A whitespace-only text part is not a body - it is an empty alternative."""
    assert body_to_text("   \n  ", "<div>Dear Sir</div>") == "Dear Sir"


def test_neither_part_gives_an_empty_body() -> None:
    assert body_to_text(None, None) == ""


def test_converted_html_is_normalized() -> None:
    """NFKC turns the non-breaking spaces Outlook loves into ordinary ones."""
    assert body_to_text(None, "<p>A&nbsp;B</p>") == "A B"
