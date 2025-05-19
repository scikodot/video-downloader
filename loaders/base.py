"""Contains base functionality for loading videos."""

import datetime
import logging
import pathlib
import re
import tempfile
from abc import ABCMeta, abstractmethod
from collections.abc import Iterable
from datetime import datetime as dt
from typing import Any

import moviepy
import moviepy.tools
import requests
from moviepy import AudioFileClip, VideoFileClip
from selenium.common import TimeoutException
from selenium.webdriver.remote.webdriver import WebDriver

from exceptions import (
    AccessRestrictedError,
    DownloadRequestError,
    FileExistsNoOverwriteError,
    GeneratorExitError,
    InvalidMimeTypeError,
    QualityContentNotFoundError,
    QualityNotFoundError,
)

DEFAULT_CHROME_SWITCHES = [
    "allow-pre-commit-input",
    "disable-background-networking",
    "disable-backgrounding-occluded-windows",
    "disable-client-side-phishing-detection",
    "disable-default-apps",
    "disable-hang-monitor",
    "disable-popup-blocking",
    "disable-prompt-on-repost",
    "disable-sync",
    # "enable-automation",
    # "enable-logging",
    # "log-level",
    # "no-first-run",
    # "no-service-autorun",
    # "password-store",
    # "remote-debugging-port",
    # "test-type",
    # "use-mock-keychain",
    # "flag-switches-begin",
    # "flag-switches-end"
]
PERF_BUFFER_SIZE = 1000
RESPONSE_OK_CODES = range(200, 300)

DEFAULT_VIDEO_PREFIX = "video"
DEFAULT_AUDIO_PREFIX = "audio"
DEFAULT_EXTENSION = ".mp4"


class LoaderBase(metaclass=ABCMeta):
    """Base class for video loader classes."""

    def __init__(self, driver: WebDriver, **kwargs: Any) -> None:
        """Create a new instance of the loader class."""
        try:
            self.output_path = pathlib.Path(kwargs["output_path"])
            self.rate = kwargs["rate"]
            self.quality = kwargs["quality"]
            self.timeout = kwargs["timeout"]
            self.exact = kwargs["exact"]
            self.overwrite = kwargs["overwrite"]
            self.driver = driver
            self.logger = logging.getLogger(self.get_logger_name())

            self._ensure_no_file_or_can_overwrite()
            self._ensure_output_directory_exists()

            # Increase resource timing buffer size.
            # The default of 250 is not always enough.
            source = f"performance.setResourceTimingBufferSize({PERF_BUFFER_SIZE})"
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": source},
            )

            # Clear browser cache.
            # Cached URLs are not listed in performance entries.
            self.driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        except Exception:
            self.logger.error("Loader initialization failed.")  # noqa: TRY400
            raise

    def _get_quality_with_units(self, quality: int) -> str:
        return f"{quality}p"

    def _copy_cookies(self, session: requests.Session) -> None:
        selenium_user_agent = self.driver.execute_script("return navigator.userAgent;")
        self.logger.debug("User agent: %s", selenium_user_agent)

        session.headers.update({"user-agent": selenium_user_agent})
        for cookie in self.driver.get_cookies():
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie["domain"],
            )

    def _download_file(self, session: requests.Session, url: str) -> requests.Response:
        response = session.get(url)
        self.logger.debug("Response: %s", response)
        self.logger.debug("Headers: %s", response.headers)

        return response

    # TODO: replace info type with namedtuple or dataclass
    def _download_file_by_info(self, session: requests.Session, info: dict) -> None:
        with pathlib.Path(info["path"]).open("ab") as file:
            for url in info["urls"]:
                response = self._download_file(session, url)
                if response.status_code not in RESPONSE_OK_CODES:
                    raise DownloadRequestError(
                        {
                            "url": url,
                            "code": response.status_code,
                        },
                    )

                # Get the packet size.
                headers = response.headers
                content_length = 0
                if "Content-Range" in headers:
                    content_range = re.split(r"\s|-|/", headers["Content-Range"])
                    start, end = (int(x) for x in content_range[1:3])
                    content_length = end - start
                elif "Content-Length" in headers:
                    content_length = int(headers["Content-Length"])
                # If no headers are present for content length,
                # calculate it from the actual content.
                else:
                    content_length = sum(
                        len(chunk) for chunk in response.iter_content(chunk_size=128)
                    )

                # Set the obtained content length for the generator
                self._content_length = content_length

                # Packet is empty => previous packet was the last.
                # Negative check is required,
                # because Content-Length header value can be negative.
                if content_length <= 0:
                    break

                for chunk in response.iter_content(chunk_size=128):
                    file.write(chunk)

    def _write_file(self, response: requests.Response, filepath: pathlib.Path) -> None:
        with pathlib.Path(filepath).open("wb") as f:
            for chunk in response.iter_content(chunk_size=128):
                f.write(chunk)

    def _ensure_video_accessible(self) -> None:
        access_restricted_msg = self.check_restrictions()
        if access_restricted_msg:
            raise AccessRestrictedError(access_restricted_msg)

    def _get_title_with_timestamp(self, prefix: str) -> str:
        timestamp = dt.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
        return f"{prefix}_{timestamp}"

    def _format_title(self, title: str) -> str:
        invalid_chars = set()

        def process_char(ch: str) -> str:
            if ch.isalnum():
                return ch
            if ch != "_":
                invalid_chars.add(ch)
            return ""

        # Split by sequences of whitespaces.
        # This also strips the title the same way on both ends.
        parts = title.split()

        # Remove invalid characters from the title.
        title_valid = "_".join(
            "".join(process_char(ch) for ch in part) for part in parts
        )

        if invalid_chars:
            self.logger.warning(
                "Title '%s' contains invalid characters: %s. "
                "Title '%s' will be used instead.",
                title,
                invalid_chars,
                title_valid,
            )

        return title_valid

    def _ensure_filename_present_and_valid(self) -> None:
        title = None
        if not self.output_path.suffix:
            # Get the video title.
            try:
                title = self.get_title()
                if title:
                    title = self._format_title(title)
            except TimeoutException:
                pass

            # Use a timestamp-based one if none found.
            if not title:
                title = self._get_title_with_timestamp(DEFAULT_VIDEO_PREFIX)
                self.logger.exception(
                    "Could not find video title. Using '%s' instead.",
                    title,
                )

            self.output_path /= title

    def _ensure_extension_present_and_valid(self) -> None:
        suffix = self.output_path.suffix
        if (
            not suffix
            or not (info := moviepy.tools.extensions_dict.get(suffix[1:]))
            or info["type"] != "video"
        ):
            self.output_path = self.output_path.with_suffix(DEFAULT_EXTENSION)

    def _ensure_no_file_or_can_overwrite(self) -> None:
        if self.output_path.suffix and self.output_path.exists() and not self.overwrite:
            raise FileExistsNoOverwriteError(self.output_path)

    def _ensure_output_directory_exists(self) -> None:
        # Use suffix to determine if the path points to a file or a directory.
        # This correctly assumes that entries like "folder/.ext" have no suffix,
        # i. e. they are directories.
        directory = self.output_path
        if self.output_path.suffix:
            directory = directory.parent
        pathlib.Path.mkdir(directory, parents=True, exist_ok=True)

    def _get_target_quality(self) -> int:
        self.qualities = self.get_qualities()
        target_quality = 0
        for q in self.qualities:
            if target_quality < q <= self.quality:
                target_quality = q

        qs = ", ".join(self._get_quality_with_units(q) for q in sorted(self.qualities))
        self.logger.debug("Qualities: %s", qs)
        if target_quality < self.quality:
            if self.exact:
                raise QualityNotFoundError(self._get_quality_with_units(self.quality))

            self.logger.info(
                "Could not find quality value %sp. "
                "Using the nearest lower quality: %sp.",
                self.quality,
                target_quality,
            )

        return target_quality

    @abstractmethod
    def get_logger_name(self) -> str:
        """Get the name of the logger used by this class."""
        ...

    @abstractmethod
    def check_restrictions(self) -> str | None:
        """Check whether the video is available.

        Returns a message describing the existing restrictions
        if present, and ``None`` otherwise.
        """
        ...

    @abstractmethod
    def disable_autoplay(self) -> None:
        """Disable the Autoplay button if one exists."""
        ...

    @abstractmethod
    def get_title(self) -> str | None:
        """Get the video title if one exists."""
        ...

    @abstractmethod
    def get_qualities(self) -> set[int]:
        """Get a set of available qualities."""
        ...

    @abstractmethod
    def get_urls(
        self,
        session: requests.Session,
        directory: pathlib.Path,
    ) -> dict[str, dict[str, Iterable[str] | str]]:
        """Get a list of direct URLs for audio/video content.

        This method is expected to return at least 2*``q_num`` URLs,
        where ``q_num`` is the number of available qualities.
        """
        ...

    def get(self, url: str) -> None:
        """Navigate to ``url``, locate the video and load it."""
        self.driver.get(url)

        try:
            self._ensure_video_accessible()
            self._ensure_filename_present_and_valid()
            self._ensure_extension_present_and_valid()
            self._ensure_no_file_or_can_overwrite()
        except AccessRestrictedError:
            self.logger.exception("Could not access the video.")
            return

        try:
            self.disable_autoplay()
        except TimeoutException:
            self.logger.exception(
                "Could not find an autoplay button to click, operation timed out.",
            )

        try:
            self.target_quality = self._get_target_quality()
        except QualityNotFoundError:
            self.logger.exception(
                "Could not find exact quality value as required by --exact flag.",
            )
            return
        except TimeoutException:
            self.logger.exception(
                "Could not obtain the available qualities, operation timed out.",
            )
            return

        try:
            with (
                requests.Session() as session,
                tempfile.TemporaryDirectory() as directory,
            ):
                self.logger.debug("Temporary directory: %s", directory)

                # Copy user agent and cookies to the new session.
                # This is required so that this session is allowed to access
                # the previously obtained URLs.
                self._copy_cookies(session)

                urls = self.get_urls(session, pathlib.Path(directory))

                audio_info = urls["audio"]
                video_info = urls["video"]
                self._download_file_by_info(session, audio_info)
                self._download_file_by_info(session, video_info)

                # Merge the downloaded files into one (audio + video)
                # TODO: if the file could not be saved to output path (for any reason:
                # missing directory, existing file without --overwrite, etc.),
                # either keep it in this temp dir or move to some safe dir
                with (
                    AudioFileClip(audio_info["path"]) as audio,
                    VideoFileClip(video_info["path"]) as video,
                ):
                    video.with_audio(audio).write_videofile(self.output_path)
        except TimeoutException:
            self.logger.exception(
                "Could not obtain the required URLs, operation timed out.",
            )
        except InvalidMimeTypeError:
            self.logger.exception("Could not recognize MIME type of the content.")
        except QualityContentNotFoundError:
            self.logger.exception("Could not find content with the required quality.")
        except DownloadRequestError:
            self.logger.exception("Could not download files due to a request error.")
        except GeneratorExitError:
            self.logger.exception("Could not download files due to a generator error.")

        return
