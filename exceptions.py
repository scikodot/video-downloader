"""Contains various exceptions that may occur."""

from dataclasses import dataclass
from typing import ClassVar


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


class QualityNotFoundError(Exception):
    """Thrown when the quality value cannot be found and ``--exact`` flag is used."""


class AccessRestrictedError(Exception):
    """Thrown when the video could not be accessed for whatever reason."""


class FileAlreadyExistsError(Exception):
    """Thrown when the file already exists and ``--overwrite`` flag is not used."""
