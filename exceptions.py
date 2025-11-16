"""Main routine exceptions, base classes, customization, etc."""

import io
import logging
import pathlib
import traceback
from abc import ABC, abstractmethod
from dataclasses import KW_ONLY, dataclass

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
class ParameterizedError(Exception, ABC):
    """Common base class for parameterized exceptions.

    Classes that inherit from this class must implement ``_message`` property
    that returns a value containing formatting placeholders (like {0}, {1}, etc.).
    All other attributes' values are then used to format ``_message``
    in their declaration order.

    The number of other attributes is expected to be equal
    to the number of placeholders in ``_message``.
    """

    @property
    @abstractmethod
    def _message(self) -> str: ...

    def __post_init__(self) -> None:  # noqa: D105
        args = tuple(v for k, v in vars(self).items() if not k.startswith("_"))
        super().__init__(self._message.format(*args))


@dataclass
class ArgumentStringError(ParameterizedError):
    """Thrown when the specified argument string is malformed."""

    @property
    @override
    def _message(self) -> str:
        return (
            "Coult not parse argument string '{1}'. "
            "Argument '{0}' requires a whitespace separated value."
        )

    arg: str
    arg_string: str


@dataclass
class UrlValidationError(ParameterizedError):
    """Thrown when the provided URL is invalid."""

    @property
    @override
    def _message(self) -> str:
        return "Invalid URL: {0}"

    url: str


@dataclass
class PathNotFoundError(ParameterizedError):
    """Thrown when the path is expected to exist but does not."""

    @property
    @override
    def _message(self) -> str:
        return "Path not found: {0}"

    path: pathlib.Path | str


@dataclass
class TooSmallValueError(ParameterizedError):
    """Thrown when the value is smaller than its lower bound."""

    @property
    @override
    def _message(self) -> str:
        bound_clause = "at least" if self.inclusive else "greater than"
        return "Value {0} is too small, must be " + bound_clause + " {1}{4}{3}."

    value: int | float
    _: KW_ONLY
    lower_bound: int | float
    inclusive: bool
    units: str
    indent: str = " "


@dataclass
class UnknownStringValueError(ParameterizedError):
    """Thrown when the value is not in the set of known values."""

    @property
    @override
    def _message(self) -> str:
        known_str = ", ".join(f"'{kv}'" for kv in self.known_values)
        return "Value '{0}' is unknown, must be one of the following: " + known_str

    value: str
    known_values: list[str]
