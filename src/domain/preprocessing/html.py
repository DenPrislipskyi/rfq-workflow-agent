"""L0: HTML email bodies to plain text."""

from selectolax.parser import HTMLParser

_DROPPED_TAGS = ["script", "style", "head", "noscript"]


def html_to_text(html: str) -> str:
    """Strip markup, keeping one line per block element."""
    tree = HTMLParser(html)
    tree.strip_tags(_DROPPED_TAGS)
    node = tree.body or tree.root
    return node.text(separator="\n") if node else ""
