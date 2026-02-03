"""Exceptions related to loaders."""

from abc import ABC
from dataclasses import dataclass
from pathlib import Path

from typing_extensions import override

from exceptions import ParameterizedError
from loaders.utils import MediaType


class LoaderError(Exception, ABC):
    """Base class for all loader exceptions."""


class LoaderNotFoundError(LoaderError):
    """Thrown when no loader could be found for the given URL."""


class PlaylistNotFoundError(LoaderError):
    """Thrown when the playlist could not be found at the given URL."""


class VideoSourceNotFoundError(LoaderError):
    """Thrown when the video source URL could not be found."""


class QualityNotFoundError(LoaderError):
    """Thrown when the quality value cannot be found and ``--exact`` flag is used."""


@dataclass
class MediaNotFoundError(ParameterizedError, LoaderError):
    """Thrown when a content of the specified quality could not be found."""

    @property
    @override
    def _message(self) -> str:
        return "No {0} content found for quality {1}p."

    media_type: MediaType
    quality: int


class AccessRestrictedError(LoaderError):
    """Thrown when the video could not be accessed for whatever reason."""


@dataclass
class FileExistsNoOverwriteError(ParameterizedError, LoaderError):
    (
        """Thrown when the file already exists and cannot be overwritten, """
        """e. g. due to ``--overwrite`` flag absence."""
    )

    @property
    @override
    def _message(self) -> str:
        return (
            "Cannot save the video to the already existing file at {0}. "
            "Consider using --overwrite flag."
        )

    path: Path | str


class MimeTypeNotFoundError(LoaderError):
    """Thrown when the MIME type was not found among response headers."""


class InvalidMimeTypeError(LoaderError):
    """Thrown when the MIME type of the retrieved content is invalid."""


class DownloadRequestError(LoaderError):
    """Thrown when the HTTP download request failed."""


class AmbiguousUrlsError(LoaderError):
    """Thrown when there are too many distinct URLs for download."""


class InvalidMpdError(LoaderError):
    """Thrown when the provided Media Presentation Document (MPD) is malformed."""


@dataclass
class DocumentScrollError(ParameterizedError, LoaderError):
    """Thrown when the performed scroll operation returned an unusual result."""

    @property
    @override
    def _message(self) -> str:
        return "Scrolled to height {0} but got new height {1}."

    height_old: int
    height_new: int
