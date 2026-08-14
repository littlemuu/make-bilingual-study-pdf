from __future__ import annotations

import re

from lxml import etree, html


SAFE_TABLE_TAGS = frozenset(
    {
        "table",
        "caption",
        "colgroup",
        "col",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "br",
        "p",
        "div",
        "span",
        "strong",
        "b",
        "em",
        "i",
        "sup",
        "sub",
        "ul",
        "ol",
        "li",
        "code",
    }
)
COMMON_ATTRIBUTES = frozenset({"align", "valign", "width", "height"})
TAG_ATTRIBUTES = {
    "table": frozenset({"border", "cellpadding", "cellspacing"}),
    "colgroup": frozenset({"span"}),
    "col": frozenset({"span"}),
    "th": frozenset({"rowspan", "colspan", "scope"}),
    "td": frozenset({"rowspan", "colspan"}),
}
INTEGER_ATTRIBUTES = frozenset(
    {"width", "height", "border", "cellpadding", "cellspacing", "span", "rowspan", "colspan"}
)
MINERU_DOCUMENT_WRAPPER_RE = re.compile(
    r"^\s*<html\s*>\s*<body\s*>(?P<table><table\b.*</table>)\s*</body\s*>\s*</html\s*>\s*$",
    re.IGNORECASE | re.DOTALL,
)


def validate_table_html(source: str) -> str:
    """Validate inert table HTML and return one canonical ``<table>`` fragment.

    MinerU pipeline content lists legitimately wrap ``table_body`` in a minimal
    ``<html><body>`` document.  Strip only that exact, attribute-free wrapper;
    every downstream consumer receives the same standalone table fragment.
    """

    if not isinstance(source, str) or not source.strip():
        raise ValueError("table HTML must be a nonempty string")
    candidate = source
    if source.lstrip().lower().startswith("<html"):
        wrapper = MINERU_DOCUMENT_WRAPPER_RE.fullmatch(source)
        if wrapper is None:
            raise ValueError(
                "MinerU table HTML wrapper must be exactly <html><body><table>..."
            )
        candidate = wrapper.group("table")
    try:
        fragments = html.fragments_fromstring(
            candidate,
            parser=html.HTMLParser(encoding="utf-8", recover=False, no_network=True),
        )
    except (etree.ParserError, etree.XMLSyntaxError, ValueError) as exc:
        raise ValueError(f"invalid table HTML: {exc}") from exc
    elements = [item for item in fragments if isinstance(item, etree._Element)]
    text_fragments = [item for item in fragments if isinstance(item, str) and item.strip()]
    if len(elements) != 1 or text_fragments:
        raise ValueError("table HTML must contain exactly one root table")
    root = elements[0]
    if not isinstance(root.tag, str) or root.tag.lower() != "table":
        raise ValueError("table HTML root must be <table>")
    if isinstance(root.tail, str) and root.tail.strip():
        raise ValueError("table HTML must not contain text after the root table")

    row_count = 0
    cell_count = 0
    for element in root.iter():
        if not isinstance(element.tag, str):
            raise ValueError("table HTML comments and processing instructions are forbidden")
        tag = element.tag.lower()
        if tag not in SAFE_TABLE_TAGS:
            raise ValueError(f"unsafe table HTML tag: {tag}")
        if tag == "tr":
            row_count += 1
        if tag in {"th", "td"}:
            cell_count += 1
        allowed = COMMON_ATTRIBUTES | TAG_ATTRIBUTES.get(tag, frozenset())
        for raw_name, value in element.attrib.items():
            name = raw_name.lower()
            if name not in allowed:
                raise ValueError(f"unsafe table HTML attribute on {tag}: {raw_name}")
            if name in INTEGER_ATTRIBUTES:
                if not value.isascii() or not value.isdigit():
                    raise ValueError(f"table HTML attribute {name} must be an integer")
                number = int(value)
                if number <= 0 or number > 10_000:
                    raise ValueError(f"table HTML attribute {name} is out of range")
            elif name == "scope" and value.lower() not in {
                "row",
                "col",
                "rowgroup",
                "colgroup",
            }:
                raise ValueError("table HTML scope attribute is invalid")
    if row_count == 0 or cell_count == 0:
        raise ValueError("table HTML must contain at least one row and cell")
    return html.tostring(root, encoding="unicode", method="html", with_tail=False)
