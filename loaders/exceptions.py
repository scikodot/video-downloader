"""Exceptions related to loaders."""

from dataclasses import dataclass

from typing_extensions import override

from exceptions import ParameterizedError
from loaders.utils import MediaType


class LoaderNotFoundError(Exception):
    """Thrown when no loader could be found for the given URL."""


class VideoSourceNotFoundError(Exception):
    """Thrown when the video source URL could not be found."""


class QualityNotFoundError(Exception):
    """Thrown when the quality value cannot be found and ``--exact`` flag is used."""


@dataclass
class MediaNotFoundError(ParameterizedError):
    """Thrown when a content of the specified quality could not be found."""

    @property
    @override
    def _message(self) -> str:
        return "No {0} content found for quality {1}p."

    media_type: MediaType
    quality: int


class AccessRestrictedError(Exception):
    """Thrown when the video could not be accessed for whatever reason."""


class FileExistsNoOverwriteError(Exception):
    """Thrown when the file already exists and ``--overwrite`` flag is not used."""


class MimeTypeNotFoundError(Exception):
    """Thrown when the MIME type was not found among response headers."""


class InvalidMimeTypeError(Exception):
    """Thrown when the MIME type of the retrieved content is invalid."""


class DownloadRequestError(Exception):
    """Thrown when the HTTP download request failed."""


class AmbiguousUrlsError(Exception):
    """Thrown when there are too many distinct URLs for download."""


class InvalidMpdError(Exception):
    """Thrown when the provided Media Presentation Document (MPD) is malformed."""
