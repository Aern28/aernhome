"""
nexus_md.py — markdown → safe HTML for the Nexus Docs section.

The render primitive that lets rich content (tables, headings, lists, code, task
lists) have a native home in Nexus instead of being exiled to Obsidian. Kept in its
own module so other sections (Notes, Feed) can borrow it later.

SECURITY: markdown output is sanitized through a strict bleach allowlist before it is
marked |safe in a template. Single-user + Tailscale-only makes the risk low, but
producers like Aernbot or feed digests could write here someday, so untrusted markup
never reaches the DOM unscrubbed.
"""
import markdown as _markdown
import bleach

# Block + inline tags a reference doc legitimately needs. Everything else is stripped.
_ALLOWED_TAGS = [
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "strong", "em", "b", "i", "del", "ins", "sup", "sub", "mark",
    "blockquote", "code", "pre",
    "a", "img",
    "table", "thead", "tbody", "tr", "th", "td",
    "span", "div",
]
_ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "img": ["src", "alt", "title"],
    "th": ["align"],
    "td": ["align"],
    "code": ["class"],   # fenced-code language hint (language-xxx)
    "span": ["class"],
    "li": ["class"],     # markdown task-list checkboxes carry a class
    "input": ["type", "checked", "disabled"],
}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

# Task lists (- [ ] / - [x]) render to <input type=checkbox>; allow them read-only.
_ALLOWED_TAGS_WITH_TASKS = _ALLOWED_TAGS + ["input"]


def render_markdown(text):
    """Render a markdown string to sanitized HTML. Returns '' for empty input."""
    text = (text or "").strip()
    if not text:
        return ""
    html = _markdown.markdown(
        text,
        extensions=["extra", "sane_lists"],   # 'extra' brings tables + fenced_code
        output_format="html5",
    )
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS_WITH_TASKS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
