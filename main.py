import argparse
import logging
import pathlib
import urllib.parse as urlparser
from typing import Any

import validators
from selenium.common.exceptions import WebDriverException

from loaders.base import LoaderBase
from loaders.vk import VkVideoLoader

PROGRAM_NAME = "video-downloader"

DEFAULT_OUTPUT_SUBPATH = "output"
DEFAULT_RATE, MINIMUM_RATE = 1024, 128
DEFAULT_QUALITY, MINIMUM_QUALITY = 720, 144
DEFAULT_TIMEOUT, MINIMUM_TIMEOUT = 10, 1

class ArgumentParserCustom(argparse.ArgumentParser):
    def add_argument(self, *args: Any, **kwargs: Any) -> argparse.Action:
        # Add empty line after every help message to visually separate entries
        if "help" in kwargs:
            kwargs["help"] += "\n \n"
        return super().add_argument(*args, **kwargs)

def validate_url(url: str) -> str:
    if not validators.url(url):
        raise argparse.ArgumentTypeError("Invalid URL.")

    return url

# TODO: consider replacing pathlib.Path with Path
def get_default_output_path() -> pathlib.Path:
    directory = pathlib.Path(__file__).parent.resolve()
    return directory / DEFAULT_OUTPUT_SUBPATH

def validate_output_path(output_path: str) -> str:
    path = pathlib.Path(output_path)
    if not path.is_absolute():
        output_path = get_default_output_path() / output_path
    elif path.drive and not pathlib.Path(path.drive).exists():
        raise argparse.ArgumentTypeError(f"No such drive: {path.drive}.")

    return output_path

def validate_rate(rate: int) -> int:
    rate = int(rate)
    if rate < MINIMUM_RATE:
        raise argparse.ArgumentTypeError(f"Too small value, must be at least {MINIMUM_RATE} KB(-s).")

    return rate

def validate_quality(quality: int) -> int:
    quality = int(quality)
    if quality < MINIMUM_QUALITY:
        raise argparse.ArgumentTypeError(f"Too small value, must be at least {MINIMUM_QUALITY}p.")

    return quality

def validate_timeout(timeout: int) -> int:
    timeout = int(timeout)
    if timeout < MINIMUM_TIMEOUT:
        raise argparse.ArgumentTypeError(f"Too small value, must be at least {MINIMUM_TIMEOUT} second(-s).")

    return timeout

def validate_user_profile(user_profile: str) -> str:
    if not pathlib.Path(user_profile).is_dir():
        raise argparse.ArgumentTypeError(f"No such directory: {user_profile}.")

    return user_profile

def get_loader_class(url: str) -> tuple[str, LoaderBase | None]:
    parsed_url = urlparser.urlparse(url)
    if parsed_url.netloc.endswith("vkvideo.ru"):
        return (parsed_url.netloc, VkVideoLoader)

    return (parsed_url.netloc, None)

def main() -> None:
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
        action="store_true")

    args = parser.parse_args()

    # Set common (root) logger format and verbosity level
    # TODO: consider adding 3 verbosity levels:
    # -v: messages of level INFO or higher
    # -vv: messages of level DEBUG or higher (this package only)
    # -vvv: messages of level DEBUG or higher (all used packages, like selenium, etc.)
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level="DEBUG" if args.verbose else "WARNING")
    logger = logging.getLogger("root")

    logger.debug("Args: %s", vars(args))

    # Ensure the output path directory exists.
    # Use suffix to determine if the path points to a file or a directory.
    # This correctly assumes that entries like "folder/.ext" have no suffix,
    # i. e. they are directories.
    output_path = pathlib.Path(args.output_path)
    output_path_dir = output_path.parent if output_path.suffix else output_path
    pathlib.Path.mkdir(output_path_dir, parents=True, exist_ok=True)

    logger.info("Setting up loader...")
    netloc, loader_class = get_loader_class(args.url)
    if not loader_class:
        logger.info(
            "Could not find loader for '%s'. Perhaps, it is not supported yet.", netloc)
        return

    loader = loader_class(**vars(args))
    try:
        logger.info("Navigating to %s...", args.url)
        loader.get(args.url)
    finally:
        logger.info("Closing driver...")
        try:
            loader.driver.close()
            loader.driver.quit()
        except WebDriverException as ex:
            logger.debug("Could not terminate the driver gracefully.\n%s", ex)

    return

if __name__ == "__main__":
    main()
