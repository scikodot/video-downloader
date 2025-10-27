"""Contains base functionality for loading videos."""

import datetime
import json
import logging
import pathlib
import re
import tempfile
import urllib.parse as urlparser
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime as dt
from io import BufferedWriter
from typing import Any

import moviepy
import moviepy.tools
import requests
from moviepy import AudioFileClip, VideoFileClip
from selenium.common import TimeoutException
from selenium.webdriver.remote.webdriver import WebDriver

from loaders.exceptions import (
    AccessRestrictedError,
    DownloadRequestError,
    FileExistsNoOverwriteError,
    InvalidMimeTypeError,
    InvalidMpdError,
    LoaderNotFoundError,
    MediaNotFoundError,
    MimeTypeNotFoundError,
    QualityNotFoundError,
    VideoSourceNotFoundError,
)
from loaders.utils import LimitedResponse, LimitedResponseOptions

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
HTTP_OK_CODES = range(200, 300)

DEFAULT_VIDEO_PREFIX = "video"
DEFAULT_AUDIO_PREFIX = "audio"
DEFAULT_EXTENSION = ".mp4"


@dataclass
class ResourceSpec:
    """Specification of a remote resource that needs to be downloaded."""

    # Pairs of type (url, bytes_exp).
    # If the expected number of bytes is not provided (= None),
    # it is considered that any positive number of loaded bytes is acceptable.
    source: Iterable[tuple[str, int | None]]
    target: pathlib.Path


@dataclass
class MediaSpec:
    """Specification of a media resource consisting of audio and video components."""

    audio: ResourceSpec
    video: ResourceSpec


class LoaderBase(ABC):
    """Base class for video loader classes."""

    def __init__(self, driver: WebDriver, **kwargs: Any) -> None:
        """Create a new instance of the loader class."""
        try:
            self.driver = driver

            # Store kwargs for a potential redirect.
            self._kwargs = kwargs

            self.url = kwargs["url"]
            self.output_path = pathlib.Path(kwargs["output_path"])
            self.chunk_size = kwargs["chunk_size"]
            self.speed_limit = kwargs["speed_limit"]
            self.quality = kwargs["quality"]
            self.timeout = kwargs["timeout"]
            self.exact = kwargs["exact"]
            self.overwrite = kwargs["overwrite"]
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

    def _download_resource(
        self,
        session: requests.Session,
        url: str,
    ) -> LimitedResponse:
        response = session.get(url)
        self.logger.debug(
            "Response: %s; Encoding: %s; Headers: %s",
            response,
            response.encoding,
            response.headers,
        )
        return LimitedResponse(response)

    def _raise_for_status(self, url: str, response: LimitedResponse) -> None:
        if response.status_code not in HTTP_OK_CODES:
            raise DownloadRequestError(
                {
                    "url": url,
                    "code": response.status_code,
                },
            )

    def _get_content_length(self, response: LimitedResponse) -> int | None:
        if cr := response.headers.get("Content-Range"):
            _, start, end, *_ = re.split(r"\s|-|/", cr)
            return int(end) - int(start)
        if cl := response.headers.get("Content-Length"):
            return int(cl)

        return None

    def _download_resource_by_spec(
        self,
        session: requests.Session,
        spec: ResourceSpec,
    ) -> None:
        bytes_count = 0
        with spec.target.open("ab") as file:
            for url, bytes_exp in spec.source:
                response = self._download_resource(session, url)
                self._raise_for_status(url, response)

                # Get the packet size.
                content_length = self._get_content_length(response)

                # Packet is empty => previous packet was the last.
                # Negative check is required,
                # because 'Content-Length' header value can be negative.
                if content_length is not None and content_length <= 0:
                    break

                # Write the response data to the file in chunks.
                bytes_read = self._append_file(response, file)
                bytes_count += bytes_read

                # Set the content length if it could not be obtained from headers.
                if content_length is None:
                    content_length = bytes_read

                # Packet is empty.
                # Here, content length can only be >= 0,
                # so no negative check is required.
                if content_length == 0:
                    break

                if bytes_exp is not None:
                    if bytes_exp <= 0:
                        self.logger.warning(
                            "Expected number of bytes for %s "
                            "must be positive, but got %s.",
                            url,
                            bytes_exp,
                        )

                    # Packet is smaller than required => file is exhausted.
                    if content_length < bytes_exp:
                        break

        self.logger.debug("%s bytes loaded into '%s'", bytes_count, spec.target)

    def _append_file(self, response: LimitedResponse, file: BufferedWriter) -> int:
        bytes_count = 0
        for chunk in response.iter_content(
            chunk_size=self.chunk_size,
            options=LimitedResponseOptions(speed_limit=self.speed_limit),
            logger=self.logger,
        ):
            bytes_count += file.write(chunk)
        return bytes_count

    def _write_file(self, response: LimitedResponse, path: pathlib.Path) -> int:
        bytes_count = 0
        with pathlib.Path(path).open("wb") as f:
            for chunk in response.iter_content(
                chunk_size=self.chunk_size,
                options=LimitedResponseOptions(speed_limit=self.speed_limit),
                logger=self.logger,
            ):
                bytes_count += f.write(chunk)

        self.logger.debug("%s bytes loaded into '%s'", bytes_count, path)
        return bytes_count

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

    def _get_status_code(self) -> int | None:
        request_id = None
        logs = self.driver.get_log("performance")
        for log in logs:
            message = json.loads(log["message"])["message"]
            self.logger.debug(message)

            method = message.get("method")
            params = message.get("params")

            # Find the first request to the specified URL
            if not request_id:
                if (
                    method == "Network.requestWillBeSent"
                    and params["documentURL"] == self.url
                ):
                    request_id = params["requestId"]
            # Then find the first response corresponding to that URL
            elif (
                method == "Network.responseReceived"
                and params["requestId"] == request_id
            ):
                return int(params["response"]["status"])

        return None

    @abstractmethod
    def get_logger_name(self) -> str:
        """Get the name of the logger used by this class."""
        ...

    @abstractmethod
    def get_source_url(self) -> str | None:
        """Get the URL of the ``<video>`` HTML tag."""
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
    def get_media(
        self,
        session: requests.Session,
        directory: pathlib.Path,
    ) -> MediaSpec:
        """Get a ``MediaSpec`` object containing audio and video content."""
        ...

    def _execute(self) -> None:
        with (
            requests.Session() as session,
            tempfile.TemporaryDirectory() as directory,
        ):
            self.logger.debug("Temporary directory: %s", directory)

            # Copy user agent and cookies to the new session.
            # This is required so that this session is allowed to access
            # the previously obtained URLs.
            self._copy_cookies(session)

            media = self.get_media(session, pathlib.Path(directory))

            self._download_resource_by_spec(session, media.audio)
            self._download_resource_by_spec(session, media.video)

            # Merge the downloaded files into one (audio + video)
            with (
                AudioFileClip(media.audio.target) as audio,
                VideoFileClip(media.video.target) as video,
            ):
                video_with_audio = video.with_audio(audio)
                output_path = self.output_path
                try:
                    self._ensure_output_directory_exists()
                    self._ensure_no_file_or_can_overwrite()
                except FileExistsNoOverwriteError:
                    filename = self._get_title_with_timestamp(output_path.stem)
                    output_path = self.output_path.with_stem(filename)
                    self.logger.exception(
                        "Cannot save the downloaded video "
                        "to the already existing file, "
                        "as '--overwrite' argument was not used. "
                        "Filename '%s' will be used instead.",
                        filename,
                    )

                video_with_audio.write_videofile(output_path)

    def _try_ensure_all(self) -> bool:
        try:
            self._ensure_video_accessible()
            self._ensure_filename_present_and_valid()
            self._ensure_extension_present_and_valid()
            self._ensure_no_file_or_can_overwrite()
        except AccessRestrictedError:
            self.logger.exception("Could not access the video.")
        else:
            return True

        return False

    def _check_redirect(self) -> None:
        source_url = self.get_source_url()
        self.logger.debug("Source URL: %s", source_url)
        if not source_url:
            raise VideoSourceNotFoundError

        url_parsed = urlparser.urlparse(self.url)
        source_url_parsed = urlparser.urlparse(source_url)
        if url_parsed.netloc != source_url_parsed.netloc:
            self.logger.warning("Redirecting to the video source at %s...", source_url)
            self._redirect(source_url)

    def _redirect(self, url: str) -> None:
        from loaders import get_loader_class

        netloc, loader_class = get_loader_class(url)
        if not loader_class:
            raise LoaderNotFoundError(netloc)

        loader = loader_class(driver=self.driver, **self._kwargs)
        loader.get(url)

    def _try_disable_autoplay(self) -> None:
        try:
            self.disable_autoplay()
        except TimeoutException:
            self.logger.exception(
                "Could not find an autoplay button to click, operation timed out.",
            )

    def _try_get_target_quality(self) -> bool:
        try:
            self.target_quality = self._get_target_quality()
        except QualityNotFoundError:
            self.logger.exception(
                "Could not find exact quality value as required by --exact flag.",
            )
        except TimeoutException:
            self.logger.exception(
                "Could not obtain the available qualities, operation timed out.",
            )
        else:
            return True

        return False

    def _try_execute(self) -> None:
        try:
            self._execute()
        except TimeoutException:
            self.logger.exception(
                "Could not obtain the required URLs, operation timed out.",
            )
        except MimeTypeNotFoundError:
            self.logger.exception("MIME type was not provided in response headers.")
        except InvalidMimeTypeError:
            self.logger.exception("Could not recognize MIME type of the content.")
        except InvalidMpdError:
            self.logger.exception("Could not parse the provided MPD file.")
        except MediaNotFoundError:
            self.logger.exception("Could not find the required media.")
        except DownloadRequestError:
            self.logger.exception("Could not download files due to a request error.")

    def get(self, url: str) -> None:
        """Navigate to ``url``, locate the video and load it."""
        self.driver.get(url)

        if not self._try_ensure_all():
            return

        self._check_redirect()
        self._try_disable_autoplay()

        if not self._try_get_target_quality():
            return

        self._try_execute()
        return
