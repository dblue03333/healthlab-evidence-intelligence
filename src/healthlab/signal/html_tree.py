"""Small bounded HTML tree shared by declarative adapters."""

from dataclasses import dataclass, field
from html.parser import HTMLParser

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
    parent: object = field(default=None, repr=False)

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.nodes()

    def text(self, *, include_hidden=False):
        if self.tag in HIDDEN and not include_hidden:
            return ""
        return " ".join(
            " ".join(
                child.text(include_hidden=include_hidden) if isinstance(child, Element) else child
                for child in self.children
            ).split()
        )


class ArticleTree(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.feed(raw.decode("utf-8", errors="replace"))

    def handle_starttag(self, tag, attrs):
        if len(self.stack) >= 128:
            raise IngestionError("html_depth_limit", "HTML nesting exceeds parser limit")
        node = Element(tag, dict(attrs))
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for position in range(len(self.stack) - 1, 0, -1):
            if self.stack[position].tag == tag:
                del self.stack[position:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)
