"""Small bounded HTML tree shared by declarative adapters."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from healthlab.models import IngestionError

VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
HIDDEN = {"script", "style", "nav", "header", "footer", "noscript", "iframe"}


@dataclass
class Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: "Element | None" = field(default=None, repr=False)

    def nodes(self) -> Iterator["Element"]:
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.nodes()

    def text(self, *, include_hidden=False) -> str:
        if self.tag in HIDDEN and not include_hidden:
            return ""
        return " ".join(
            " ".join(
                child.text(include_hidden=include_hidden) if isinstance(child, Element) else child
                for child in self.children
            ).split()
        )


class ArticleTree(HTMLParser):
    def __init__(self, raw: bytes) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.feed(raw.decode("utf-8", errors="replace"))

    def handle_starttag(self, tag: str, attrs: list[Any]) -> None:
        if len(self.stack) >= 128:
            raise IngestionError("html_depth_limit", "HTML nesting exceeds parser limit")
        node = Element(tag, dict(attrs))
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[Any]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self.stack) - 1, 0, -1):
            if self.stack[position].tag == tag:
                del self.stack[position:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)
