"""Video downloader for specific websites."""

import argparse
import logging
import pathlib
import urllib.parse as urlparser
from typing import Any

import validators
from selenium.common.exceptions import WebDriverException

from exceptions import (
    ExceptionFormatter,
    FileExistsNoOverwriteError,
    PathNotFoundError,
    TooSmallValueError,
    UrlValidationError,
)
from loaders.base import LoaderBase
from loaders.vk import VkVideoLoader

PROGRAM_NAME = "video-downloader"

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

DEFAULT_OUTPUT_SUBPATH = "output"
DEFAULT_RATE, MINIMUM_RATE = 1024, 128
DEFAULT_QUALITY, MINIMUM_QUALITY = 720, 144
DEFAULT_TIMEOUT, MINIMUM_TIMEOUT = 10, 1

class ArgumentParserCustom(argparse.ArgumentParser):
    """Custom argument parser that adds extra formatting for help messages."""

    def add_argument(self, *args: Any, **kwargs: Any) -> argparse.Action:
        """Add a blank line after every help message to visually separate entries."""
        if "help" in kwargs:
            kwargs["help"] += "\n \n"

        return super().add_argument(*args, **kwargs)

def validate_url(url: str) -> str:
    """Assert that the provided URL is valid."""
    if not validators.url(url):
        raise UrlValidationError(url)

    return url

# TODO: consider replacing pathlib.Path with Path
def get_default_output_path() -> pathlib.Path:
    """Get the default output path for downloaded videos."""
    directory = pathlib.Path(__file__).parent.resolve()
    return directory / DEFAULT_OUTPUT_SUBPATH

def validate_output_path(output_path: str) -> str:
    """Assert that the provided output path is valid."""
    path = pathlib.Path(output_path)
    if not path.is_absolute():
        output_path = get_default_output_path() / output_path
    elif path.drive and not pathlib.Path(path.drive).exists():
        raise PathNotFoundError(path.drive)

    return output_path

def validate_rate(rate: int) -> int:
    """Assert that the provided download rate is valid."""
    rate = int(rate)
    if rate < MINIMUM_RATE:
        raise TooSmallValueError(rate, MINIMUM_RATE, "KB(-s)")

    return rate

def validate_quality(quality: int) -> int:
    """Assert that the required quality is valid."""
    quality = int(quality)
    if quality < MINIMUM_QUALITY:
        raise TooSmallValueError(quality, MINIMUM_QUALITY, "p", indent="")

    return quality

def validate_timeout(timeout: int) -> int:
    """Assert that the provided timeout is valid."""
    timeout = int(timeout)
    if timeout < MINIMUM_TIMEOUT:
        raise TooSmallValueError(timeout, MINIMUM_TIMEOUT, "second(-s)")

    return timeout

def validate_user_profile(user_profile: str) -> str:
    """Assert that the provided user profile is available."""
    if not pathlib.Path(user_profile).is_dir():
        raise PathNotFoundError(user_profile)

    return user_profile

def get_loader_class(url: str) -> tuple[str, LoaderBase | None]:
    """Get the corresponding loader class for the specified URL."""
    parsed_url = urlparser.urlparse(url)
    if parsed_url.netloc.endswith("vkvideo.ru"):
        return (parsed_url.netloc, VkVideoLoader)

    return (parsed_url.netloc, None)

def main() -> None:
    """Entry point for the video downloader."""
    parser = ArgumentParserCustom(
        prog=PROGRAM_NAME,
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=False)

    parser.add_argument(
        "url",
        help="Video URL.",
        type=validate_url)

    parser.add_argument(
        "-h", "--help",
        help="Show this help message and exit.",
        action="help",
        default=argparse.SUPPRESS)

    parser.add_argument(
        "-o", "--output-path",
        help=(
            "Where to put the downloaded video. May be absolute or relative.\n"
            "If relative, the video will be saved at the specified path "
            "under the directory the program was run from.\n"
            f"If omitted, the video will be saved to the '{DEFAULT_OUTPUT_SUBPATH}/' "
            "path under the directory the program was run from."
        ),
        default=get_default_output_path(),
        type=validate_output_path)

    parser.add_argument(
        "-r", "--rate",
        help=(
            "How many kilobytes (KBs) to download on every request.\n"
            "Higher rates are advised for longer videos."
        ),
        default=DEFAULT_RATE,
        type=validate_rate)

    parser.add_argument(
        "-q", "--quality",
        help=(
            f"Which quality the downloaded video must have (e. g. {DEFAULT_QUALITY}).\n"
            "This parameter determines the exact quality "
            "if used together with '--strict' flag, and a maximum quality otherwise.\n"
            "In the latter case, the first quality value lower than or equal "
            "to this parameter value will be used."
        ),
        default=DEFAULT_QUALITY,
        type=validate_quality)

    parser.add_argument(
        "-t", "--timeout",
        help=(
            "How many seconds to wait for every operation on the page to complete.\n"
            "Few tens of seconds is usually enough."
        ),
        default=DEFAULT_TIMEOUT,
        type=validate_timeout)

    parser.add_argument(
        "-u", "--user-profile",
        help=(
            "Path to the user profile to launch Chrome with.\n"
            "This must be a combination of both '--user-data-dir' "
            "and '--profile-directory' arguments supplied to Chrome."
        ),
        default=argparse.SUPPRESS,
        type=validate_user_profile)

    parser.add_argument(
        "-e", "--exact",
        help=(
            "Do not load the video in any quality "
            "if the specified quality is not found."
        ),
        action="store_true")

    parser.add_argument(
        "-w", "--overwrite",
        help="Overwrite the video file with the same name if it exists.",
        action="store_true")

    parser.add_argument(
        "-l", "--headless",
        help="Run browser in headless mode, i. e. without GUI.",
        action="store_true")

    parser.add_argument(
        "-v", "--verbose",
        help="Show detailed information about performed actions.",
        action="count",
        default=0)

    args = parser.parse_args()

    verbosity = args.verbose
    # Use local package logger
    if verbosity <= MAX_PACKAGE_VERBOSITY:
        logger = logging.getLogger("loaders")
        logger.setLevel(VERBOSITY_LEVELS[verbosity])
    # Use root logger that can be used by all packages
    else:
        logger = logging.getLogger("root")
        level = min(verbosity, len(VERBOSITY_LEVELS) - 1)
        logger.setLevel(VERBOSITY_LEVELS[level])

    handler = logging.StreamHandler()
    formatter = ExceptionFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logger.debug("Args: %s", vars(args))

    logger.info("Setting up loader...")
    netloc, loader_class = get_loader_class(args.url)
    if not loader_class:
        logger.error(
            "Could not find loader for '%s'. Perhaps, it is not supported yet.", netloc)
        logger.info("Exiting...")
        return

    try:
        loader = loader_class(**vars(args))
        logger.info("Navigating to %s...", args.url)
        loader.get(args.url)
    except FileExistsNoOverwriteError:
        loader.logger.exception(
            "Cannot save the video to the already existing file. "
            "Use '--overwrite' argument to be able to overwrite the existing file.")
    finally:
        logger.info("Closing driver...")
        try:
            loader.driver.close()
            loader.driver.quit()
        except WebDriverException:
            logger.exception("Could not terminate the driver gracefully.")

    logger.info("Exiting...")
    return

if __name__ == "__main__":
    main()
