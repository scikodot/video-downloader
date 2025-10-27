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
    netloc = parsed_url.netloc
    if netloc.endswith("vkvideo.ru"):
        return (netloc, vk.VkVideoLoader)
    if netloc.endswith("ok.ru"):
        return (netloc, vk.OkLoader)

    return (netloc, None)
