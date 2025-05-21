"""Contains various exceptions that may occur."""

import io
import logging
import traceback
from dataclasses import dataclass
from typing import ClassVar

# TODO: add typing_extensions to reqs
from typing_extensions import override


class ExceptionFormatter(logging.Formatter):
    """Custom formatter for logging exceptions without stacktrace."""

    @override
    def formatException(self, ei: tuple) -> str:
        sio = io.StringIO()
        # Setting limit=0 prints exception without stacktrace.
        traceback.print_exception(ei[0], ei[1], ei[2], limit=0, file=sio)
        s = sio.getvalue()
        sio.close()

        # Also strip the exception message of the traceback if it is present.
        if (pos := s.find("Traceback")) > 0:
            s = s[:pos]
        if (pos := s.find("Stacktrace")) > 0:
            s = s[:pos]

        if s[-1] == "\n":
            s = s[:-1]
        return f" | {s}"

    @override
    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        s = s.replace("\n", "")
        # Disable caching exception info as that would prevent
        # other formatters from getting its stacktrace.
        if record.exc_text:
            record.exc_text = None
        return s


@dataclass
class ParameterizedError(Exception):
    """Common base class for parameterized exceptions.

    Classes that inherit from this class must declare ``_message`` attribute
    with the default value containing formatting placeholders (like {0}, {1}, etc.).
    All other attributes' values are then used to format ``_message``
    in their declaration order.

    The number of other attributes is expected to be equal
    to the number of placeholders in ``_message``.
    """

    # ClassVar's are excluded from @dataclass workflow.
    # This field will not make its way to __init__, etc.
    # TODO: consider converting to a property
    _message: ClassVar[str]

    def __post_init__(self) -> None:  # noqa: D105
        args = tuple(v for k, v in vars(self).items() if not k.startswith("_"))
        super().__init__(self._message.format(*args))


@dataclass
class UrlValidationError(ParameterizedError):
    """Thrown when the provided URL is invalid."""

    _message: ClassVar[str] = "Invalid URL: {0}"
    url: str


@dataclass
class PathNotFoundError(ParameterizedError):
    """Thrown when the path is expected to exist but does not."""

    _message: ClassVar[str] = "Path not found: {0}"
    path: str


@dataclass
class TooSmallValueError(ParameterizedError):
    """Thrown when the value is smaller than its lower bound."""

    _message: ClassVar[str] = "Value {0} is too small, must be at least {1}{3}{2}."
    value: int | float
    lower_bound: int | float
    units: str
    indent: str = " "


@dataclass
class GeneratorExitError(ParameterizedError):
    """Thrown when it is unclear as to when the generator must exit."""

    _message: ClassVar[str] = "Exit condition is undefined. {0}"
    details: str = ""


class QualityNotFoundError(Exception):
    """Thrown when the quality value cannot be found and ``--exact`` flag is used."""


class QualityContentNotFoundError(Exception):
    """Thrown when a content of the specified quality could not be found."""


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
