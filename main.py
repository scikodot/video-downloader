"""Video downloader for specific websites."""

import argparse
import logging
import pathlib
import urllib.parse as urlparser
from typing import Any

import validators
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from typing_extensions import override

from exceptions import (
    ArgumentStringError,
    ExceptionFormatter,
    PathNotFoundError,
    TooSmallValueError,
    UrlValidationError,
)
from loaders.base import LoaderBase
from loaders.exceptions import FileExistsNoOverwriteError
from loaders.vk import VkVideoLoader

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

DEFAULT_OUTPUT_SUBPATH = "output"
DEFAULT_RATE, MINIMUM_RATE = 1024, 128
DEFAULT_QUALITY, MINIMUM_QUALITY = 720, 144
DEFAULT_TIMEOUT, MINIMUM_TIMEOUT = 10, 1


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
        for arg_string in arg_strings:
            if (
                arg_string.startswith("-")
                and not arg_string.startswith("--")
                and len(arg_string) > SHORT_ARG_LEN
            ):
                for ch in arg_string[1:]:
                    # TODO: move args declaration to .json
                    if ch in "orqtu":
                        raise ArgumentStringError(arg_string, f"-{ch}")
        return super()._parse_known_args(arg_strings, namespace)


def _validate_url(url: str) -> str:
    if not validators.url(url):
        raise UrlValidationError(url)

    return url


def get_default_output_path() -> pathlib.Path:
    """Get the default output path for downloaded videos."""
    directory = pathlib.Path(__file__).parent.resolve()
    return directory / DEFAULT_OUTPUT_SUBPATH


def _validate_output_path(output_path: str) -> str:
    path = pathlib.Path(output_path)
    if not path.is_absolute():
        path = get_default_output_path() / path
    elif path.drive and not pathlib.Path(path.drive).exists():
        raise PathNotFoundError(path.drive)

    return str(path)


def _validate_rate(rate: int) -> int:
    rate = int(rate)  # TODO: ???
    if rate < MINIMUM_RATE:
        raise TooSmallValueError(rate, MINIMUM_RATE, "KB(-s)")

    return rate


def _validate_speed_limit(speed_limit: int) -> int:
    # TODO: throw a warning of too low/high value?
    return speed_limit


def _validate_quality(quality: int) -> int:
    quality = int(quality)
    if quality < MINIMUM_QUALITY:
        raise TooSmallValueError(quality, MINIMUM_QUALITY, "p", indent="")

    return quality


def _validate_timeout(timeout: int) -> int:
    timeout = int(timeout)
    if timeout < MINIMUM_TIMEOUT:
        raise TooSmallValueError(timeout, MINIMUM_TIMEOUT, "second(-s)")

    return timeout


def _validate_user_profile(user_profile: str) -> str:
    if not pathlib.Path(user_profile).is_dir():
        raise PathNotFoundError(user_profile)

    return user_profile


def get_loader_class(url: str) -> tuple[str, type[LoaderBase] | None]:
    """Get the corresponding loader class for the specified URL."""
    parsed_url = urlparser.urlparse(url)
    if parsed_url.netloc.endswith("vkvideo.ru"):
        return (parsed_url.netloc, VkVideoLoader)

    return (parsed_url.netloc, None)


def _parse_args() -> argparse.Namespace:
    parser = CustomArgumentParser(
        prog=PROGRAM_NAME,
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=False,
    )

    parser.add_argument("url", help="Video URL.", type=_validate_url)

    parser.add_argument(
        "-h",
        "--help",
        help="Show this help message and exit.",
        action="help",
        default=argparse.SUPPRESS,
    )

    parser.add_argument(
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
    )

    # TODO: rename to '--chunk' or '--chunk-size'
    parser.add_argument(
        "-r",
        "--rate",
        help=(
            "How many kilobytes (KBs) to download on every request.\n"
            "Higher rates are advised for longer videos."
        ),
        default=DEFAULT_RATE,
        type=_validate_rate,
    )

    parser.add_argument(
        "-s",
        "--speed-limit",
        help=(
            "Maximum connection speed to establish.\n"
            "Generally, higher values are preferrable, "
            "but one must take care of not becoming subject to possible restrictions "
            "that the server host may impose "
            "if the client consumes too much traffic at once."
        ),
        type=_validate_speed_limit,
    )

    parser.add_argument(
        "-q",
        "--quality",
        help=(
            f"Which quality the downloaded video must have (e. g. {DEFAULT_QUALITY}).\n"
            "This parameter determines the exact quality "
            "if used together with '--strict' flag, and a maximum quality otherwise.\n"
            "In the latter case, the first quality value lower than or equal "
            "to this parameter value will be used."
        ),
        default=DEFAULT_QUALITY,
        type=_validate_quality,
    )

    parser.add_argument(
        "-t",
        "--timeout",
        help=(
            "How many seconds to wait for every operation on the page to complete.\n"
            "Few tens of seconds is usually enough."
        ),
        default=DEFAULT_TIMEOUT,
        type=_validate_timeout,
    )

    parser.add_argument(
        "-u",
        "--user-profile",
        help=(
            "Path to the user profile to launch Chrome with.\n"
            "This must be a combination of both '--user-data-dir' "
            "and '--profile-directory' arguments supplied to Chrome."
        ),
        type=_validate_user_profile,
    )

    parser.add_argument(
        "-e",
        "--exact",
        help=(
            "Do not load the video in any quality "
            "if the specified quality is not found."
        ),
        action="store_true",
    )

    parser.add_argument(
        "-w",
        "--overwrite",
        help="Overwrite the video file with the same name if it exists.",
        action="store_true",
    )

    parser.add_argument(
        "-l",
        "--headless",
        help="Run browser in headless mode, i. e. without GUI.",
        action="store_true",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        help="Show detailed information about performed actions.",
        action="count",
        default=0,
    )

    return parser.parse_args()


def _get_logger(verbosity: int) -> logging.Logger:
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
    return logger


def _get_chrome_options(
    *,
    user_profile: str | None,
    headless: bool,
) -> webdriver.ChromeOptions:
    options = webdriver.ChromeOptions()
    if user_profile:
        path = pathlib.Path(user_profile)
        # options.add_experimental_option("excludeSwitches", CHROME_DEFAULT_SWITCHES)
        options.add_argument(f"--user-data-dir={path.parent}")
        options.add_argument(f"--profile-directory={path.name}")

    # Hide browser GUI
    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--mute-audio")  # Mute the browser
    # options.add_argument('--disable-gpu')  # Disable GPU hardware acceleration
    # options.add_argument('--disable-dev-shm-usage')  # Overcome limited resource problems
    # options.add_argument('--no-sandbox')  # Bypass OS security model
    # options.add_argument('--disable-web-security')  # Disable web security
    # options.add_argument('--allow-running-insecure-content')  # Allow running insecure content
    # options.add_argument('--disable-webrtc')  # Disable WebRTC
    return options


def main() -> None:
    """Entry point for the video downloader."""
    args = _parse_args()
    logger = _get_logger(args.verbose)

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

    options = _get_chrome_options(
        user_profile=args.user_profile,
        headless=args.headless,
    )
    try:
        logger.info("Setting up driver...")
        with webdriver.Chrome(options=options) as driver:
            loader = None
            try:
                loader = loader_class(driver=driver, **vars(args))
                logger.info("Navigating to %s...", args.url)
                loader.get(args.url)
            # TODO: also log all uncaught exceptions, via sys.excepthook or similar
            except FileExistsNoOverwriteError:
                logger.exception(
                    "Cannot save the video to the already existing file. "
                    "Use '--overwrite' argument to be able to overwrite "
                    "the existing file.",
                )

            logger.info("Closing driver...")
    except WebDriverException:
        logger.exception("Driver error has occured.")

    logger.info("Exiting...")
    return


if __name__ == "__main__":
    main()
