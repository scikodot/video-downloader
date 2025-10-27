"""Collection of loaders for different websites."""

import urllib.parse as urlparser

from loaders import (
    base,
    exceptions,
    utils,
    vk,
)

__all__ = [
    "base",
    "exceptions",
    "utils",
    "vk",
]


def get_loader_class(url: str) -> tuple[str, type[base.LoaderBase] | None]:
    """Get the corresponding loader class for the specified URL."""
    parsed_url = urlparser.urlparse(url)
    if parsed_url.netloc.endswith(("vkvideo.ru", "ok.ru")):
        return (parsed_url.netloc, vk.VkVideoLoader)

    return (parsed_url.netloc, None)
