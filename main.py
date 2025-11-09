"""Video downloader for specific websites."""

import argparse
import logging
import pathlib
import sys
from collections.abc import Callable
from types import TracebackType
from typing import Any, TypeVar

import validators
from selenium.common.exceptions import WebDriverException
from typing_extensions import override

from driver import CustomWebDriver, get_driver_options
from exceptions import (
    ArgumentStringError,
    PathNotFoundError,
    TooSmallValueError,
    UrlValidationError,
)
from loaders import get_loader_class
from loaders.exceptions import FileExistsNoOverwriteError
from loaders.utils import get_current_timestamp

PROGRAM_NAME = "video-downloader"

SHORT_ARG_LEN = 2
MAX_PACKAGE_VERBOSITY = 2
VERBOSITY_LEVELS = {
    # Package level logging, i. e. only current package
    0: "WARNING",
    1: "INFO",
    2: "DEBUG",
    # Root level logging, i. e. all used packages
    3: "INFO",
    4: "DEBUG",
}

LOGS_SUBPATH = "logs"
DEFAULT_OUTPUT_SUBPATH = "output"
DEFAULT_CHUNK_SIZE, MINIMUM_CHUNK_SIZE = 1024, 128
DEFAULT_QUALITY, MINIMUM_QUALITY = 720, 144
DEFAULT_TIMEOUT, MINIMUM_TIMEOUT = 10, 1


def _validate_url(url: str) -> str:
    if not validators.url(url):
        raise UrlValidationError(url)

    return url


def get_root_path() -> pathlib.Path:
    """Get the current file path."""
    return pathlib.Path(__file__).parent.resolve()


def get_logs_path() -> pathlib.Path:
    """Get the path to the logs."""
    return get_root_path() / LOGS_SUBPATH


def get_default_output_path() -> pathlib.Path:
    """Get the default output path for downloaded videos."""
    return get_root_path() / DEFAULT_OUTPUT_SUBPATH


def _validate_output_path(output_path: str) -> str:
    path = pathlib.Path(output_path)
    if not path.is_absolute():
        path = get_default_output_path() / path
    elif path.drive and not pathlib.Path(path.drive).exists():
        raise PathNotFoundError(path.drive)

    return str(path)


T = TypeVar("T", int, float)


def _assert_arg_type(t: type[T]) -> Callable[[Callable[[T], T]], Callable[[str], T]]:
    """Assert that the function's argument is convertible to the specified type.

    Returns a new function that accepts the argument as a string
    and attempts to convert it to the specified type.
    If the conversion is successful, calls the decorated function
    with the converted argument.
    """

    def decorator(f: Callable[[T], T]) -> Callable[[str], T]:
        def wrapper(arg: str) -> T:
            return f(t(arg))

        # This ensures that the exception messages contain
        # the name of the actual type to which the argument is converted,
        # instead of the function's name.
        wrapper.__name__ = t.__name__
        return wrapper

    return decorator


@_assert_arg_type(int)
def _validate_chunk_size(chunk_size: int) -> int:
    if chunk_size < MINIMUM_CHUNK_SIZE:
        raise TooSmallValueError(
            chunk_size,
            lower_bound=MINIMUM_CHUNK_SIZE,
            inclusive=True,
            units="KB(-s)",
        )

    return chunk_size


@_assert_arg_type(float)
def _validate_speed_limit(speed_limit: float) -> float:
    if speed_limit <= 0:
        raise TooSmallValueError(
            speed_limit,
            lower_bound=0,
            inclusive=False,
            units="Mibps",
        )
    return speed_limit


@_assert_arg_type(int)
def _validate_quality(quality: int) -> int:
    if quality < MINIMUM_QUALITY:
        raise TooSmallValueError(
            quality,
            lower_bound=MINIMUM_QUALITY,
            inclusive=True,
            units="p",
            indent="",
        )

    return quality


@_assert_arg_type(int)
def _validate_timeout(timeout: int) -> int:
    if timeout < MINIMUM_TIMEOUT:
        raise TooSmallValueError(
            timeout,
            lower_bound=MINIMUM_TIMEOUT,
            inclusive=True,
            units="second(-s)",
        )

    return timeout


def _validate_user_profile(user_profile: str) -> str:
    if not pathlib.Path(user_profile).is_dir():
        raise PathNotFoundError(user_profile)

    return user_profile


class PositionalArgument:
    """Positional command-line argument."""

    def __init__(self, name: str, **kwargs: Any) -> None:
        """Define a new positional command-line argument."""
        self.name = name
        self.kwargs = kwargs


class OptionalArgument:
    """Optional command-line argument."""

    def __init__(self, short_name: str, full_name: str, **kwargs: Any) -> None:
        """Define a new optional command-line argument."""
        self.short_name = short_name
        self.full_name = full_name
        self.kwargs = kwargs


class ArgumentsSpec:
    """Specification of command-line arguments that the program accepts."""

    positional: list[PositionalArgument]
    optional: list[OptionalArgument]
    flags: list[OptionalArgument]

    def __init__(self, *args: PositionalArgument | OptionalArgument) -> None:
        """Define a new command-line arguments specification."""
        self.positional = []
        self.optional = []
        self.flags = []
        for arg in args:
            if isinstance(arg, PositionalArgument):
                self.positional.append(arg)
            elif isinstance(arg, OptionalArgument):
                # Flags do not require a value, so they don't have 'type' parameter
                if "type" in arg.kwargs:
                    self.optional.append(arg)
                else:
                    self.flags.append(arg)


ARGSPEC = ArgumentsSpec(
    PositionalArgument("url", help="Video URL.", type=_validate_url),
    OptionalArgument(
        "-h",
        "--help",
        help="Show this help message and exit.",
        action="help",
        default=argparse.SUPPRESS,
    ),
    OptionalArgument(
        "-o",
        "--output-path",
        help=(
            "Where to put the downloaded video. May be absolute or relative.\n"
            "If relative, the video will be saved at the specified path "
            "under the directory the program was run from.\n"
            f"If omitted, the video will be saved to the '{DEFAULT_OUTPUT_SUBPATH}/' "
            "path under the directory the program was run from."
        ),
        default=get_default_output_path(),
        type=_validate_output_path,
    ),
    OptionalArgument(
        "-c",
        "--chunk-size",
        help=(
            "Number of kibibytes (KiBs) to download on every request "
            "in case of chunked data.\n"
            "Higher values are advised for longer videos."
        ),
        default=DEFAULT_CHUNK_SIZE,
        type=_validate_chunk_size,
    ),
    OptionalArgument(
        "-s",
        "--speed-limit",
        help=(
            "Maximum connection speed (Mib/s) to establish.\n"
            "Generally, higher values are preferrable, "
            "but one must take care of not becoming subject to possible restrictions "
            "that the server host may impose "
            "if the client consumes too much traffic at once."
        ),
        type=_validate_speed_limit,
    ),
    # TODO: also add "min" and "max" values
    # to download videos in minimum and maximum available qualities
    OptionalArgument(
        "-q",
        "--quality",
        help=(
            f"Which quality the downloaded video must have (e. g. {DEFAULT_QUALITY}).\n"
            "This parameter determines the exact quality "
            "if used together with '--exact' flag, and a maximum quality otherwise.\n"
            "In the latter case, the first quality value lower than or equal "
            "to this parameter value will be used."
        ),
        default=DEFAULT_QUALITY,
        type=_validate_quality,
    ),
    OptionalArgument(
        "-t",
        "--timeout",
        help=(
            "How many seconds to wait for every operation on the page to complete.\n"
            "Few tens of seconds is usually enough."
        ),
        default=DEFAULT_TIMEOUT,
        type=_validate_timeout,
    ),
    OptionalArgument(
        "-u",
        "--user-profile",
        help=(
            "Path to the user profile to launch Chrome with.\n"
            "This must be a combination of both '--user-data-dir' "
            "and '--profile-directory' arguments supplied to Chrome."
        ),
        type=_validate_user_profile,
    ),
    OptionalArgument(
        "-e",
        "--exact",
        help=(
            "Do not load the video in any quality "
            "if the specified quality is not found."
        ),
        action="store_true",
    ),
    OptionalArgument(
        "-w",
        "--overwrite",
        help="Overwrite the video file with the same name if it exists.",
        action="store_true",
    ),
    OptionalArgument(
        "-l",
        "--headless",
        help="Run browser in headless mode, i. e. without GUI.",
        action="store_true",
    ),
    OptionalArgument(
        "-v",
        "--verbose",
        help="Show detailed information about performed actions.",
        action="count",
        default=0,
    ),
)


class CustomArgumentParser(argparse.ArgumentParser):
    """Custom argument parser that adds extra formatting for help messages."""

    @override
    def add_argument(self, *args: Any, **kwargs: Any) -> argparse.Action:
        if "help" in kwargs:
            kwargs["help"] += "\n \n"

        return super().add_argument(*args, **kwargs)

    @override
    def _parse_known_args(
        self,
        arg_strings: list[str],
        namespace: argparse.Namespace,
    ) -> tuple[argparse.Namespace, list[str]]:
        opt_args = {a.short_name[1]: a for a in ARGSPEC.optional}
        for arg_string in arg_strings:
            if (
                arg_string.startswith("-")
                and not arg_string.startswith("--")
                and len(arg_string) > SHORT_ARG_LEN
            ):
                for ch in arg_string[1:]:
                    if ch in opt_args:
                        raise ArgumentStringError(
                            opt_args[ch].short_name,
                            arg_string,
                        )
        return super()._parse_known_args(arg_strings, namespace)


def _parse_args() -> argparse.Namespace:
    parser = CustomArgumentParser(
        prog=PROGRAM_NAME,
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=False,
    )

    for arg in ARGSPEC.positional:
        parser.add_argument(arg.name, **arg.kwargs)

    for arg in ARGSPEC.optional + ARGSPEC.flags:
        parser.add_argument(arg.short_name, arg.full_name, **arg.kwargs)

    return parser.parse_args()


_log_timestamp = get_current_timestamp()
_log_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")


def _get_log_console_handler() -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(_log_formatter)
    return handler


def _get_log_file_handler() -> logging.Handler:
    handler = logging.FileHandler(
        get_logs_path() / f"log_{_log_timestamp}.txt",
        delay=True,
    )
    handler.setFormatter(_log_formatter)
    return handler


def _get_logger(name: str | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(VERBOSITY_LEVELS[0])
    logger.addHandler(_get_log_console_handler())
    logger.addHandler(_get_log_file_handler())
    return logger


def main() -> None:
    """Entry point for the video downloader."""
    local_logger = _get_logger("loaders")

    # Ensure logs directory exists
    pathlib.Path.mkdir(get_logs_path(), parents=True, exist_ok=True)

    def excepthook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        local_logger.critical(
            "An unhandled exception has occured.",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    # Use sys.excepthook to log unhandled exceptions
    sys.excepthook = excepthook

    args = _parse_args()

    # Use local package logger
    if args.verbose <= MAX_PACKAGE_VERBOSITY:
        logger = local_logger
        logger.setLevel(VERBOSITY_LEVELS[args.verbose])

    # Use root logger that can be used by all packages
    else:
        logger = _get_logger()
        level = min(args.verbose, len(VERBOSITY_LEVELS) - 1)
        logger.setLevel(VERBOSITY_LEVELS[level])

    logger.debug("Args: %s", vars(args))

    logger.info("Setting up loader...")
    netloc, loader_class = get_loader_class(args.url)
    if not loader_class:
        logger.error(
            "Could not find loader for '%s'. Perhaps, it is not supported yet.",
            netloc,
        )
        logger.info("Exiting...")
        return

    options = get_driver_options(
        user_profile=args.user_profile,
        headless=args.headless,
    )
    try:
        with CustomWebDriver(logger, options=options) as driver:
            loader = None
            try:
                loader = loader_class(driver=driver, **vars(args))
                logger.info("Navigating to %s...", args.url)
                loader.get(args.url)
            except FileExistsNoOverwriteError:
                logger.exception(
                    "Cannot save the video to the already existing file. "
                    "Use '--overwrite' argument to be able to overwrite it.",
                )
    except WebDriverException:
        logger.exception("Driver error has occured.")

    logger.info("Exiting...")
    return


if __name__ == "__main__":
    main()
