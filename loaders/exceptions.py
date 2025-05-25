"""Exceptions related to loaders."""

from dataclasses import dataclass

from typing_extensions import override

from exceptions import ParameterizedError
from loaders.utils import MediaType


@dataclass
class GeneratorExitError(ParameterizedError):
    """Thrown when it is unclear as to when the generator must exit."""

    @property
    @override
    def _message(self) -> str:
        return "Exit condition is undefined. {0}"

    details: str = ""


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


class InvalidMimeTypeError(Exception):
    """Thrown when the MIME type of the retrieved content is invalid."""


class DownloadRequestError(Exception):
    """Thrown when the HTTP download request failed."""


class AmbiguousUrlsError(Exception):
    """Thrown when there are too many distinct URLs for download."""


class InvalidMpdError(Exception):
    """Thrown when the provided Media Presentation Document (MPD) is malformed."""
