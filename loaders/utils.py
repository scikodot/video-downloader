"""Various utilities for loaders."""

from enum import StrEnum, auto
from typing import Self

from lxml import etree
from typing_extensions import override

# Import whole module instead of specific exceptions.
# Otherwise it would cause a circular import error,
# as this module is referenced by exceptions' module.
from loaders import exceptions


class MediaType(StrEnum):
    """Enumeration class of known media types."""

    AUDIO = auto()
    VIDEO = auto()

    @classmethod
    def from_mime_type(cls, mime_type: str) -> Self:
        for media_type in cls:
            if mime_type.startswith(media_type):
                return media_type

        raise exceptions.InvalidMimeTypeError(mime_type)


class CustomElement(etree._Element):  # noqa: SLF001
    """Wrapper class for ``lxml.etree._Element``.

    Raises exceptions instead of returning ``None`` when nothing is found.
    """

    def __init__(self, elem: etree._Element) -> None:
        """Create a new ``lxml.etree._Element`` wrapper for ``elem``."""
        self.elem = elem

    @override
    def find(self, path, namespaces=None) -> "CustomElement":  # noqa: ANN001
        res = self.elem.find(path, namespaces)
        if not res:
            raise exceptions.InvalidMpdError
        return CustomElement(res)

    # Ignore override typing; base method returns
    # a specific type (list[etree._Element], which is invariant)
    # instead of a more general one, hence no opportunity for typesafe subtyping.
    @override
    def findall(self, path, namespaces=None) -> "list[CustomElement]":  # type: ignore[override] # noqa: ANN001
        res = self.elem.findall(path, namespaces)
        if not res:
            raise exceptions.InvalidMpdError
        return [CustomElement(elem) for elem in res]

    # Ignore override typing; base methods are @overload'ed,
    # @override cannot determine the right version,
    # and overriding the base implementation (with 'default' param) is unnecessary.
    @override
    def get(self, key) -> str:  # type: ignore[override]  # noqa: ANN001
        res = self.elem.get(key)
        if not res:
            raise exceptions.InvalidMpdError
        return res


class CustomElementTree(etree._ElementTree):  # noqa: SLF001
    """Wrapper class for ``lxml.etree._ElementTree``.

    Raises exceptions instead of returning ``None`` when nothing is found.
    """

    def __init__(self, tree: etree._ElementTree) -> None:
        """Create a new ``lxml.etree._ElementTree`` wrapper for ``tree``."""
        self.tree = tree

    @override
    def find(self, path, namespaces=None) -> CustomElement:  # noqa: ANN001
        res = self.tree.find(path, namespaces)
        if res is None:
            raise exceptions.InvalidMpdError
        return CustomElement(res)
