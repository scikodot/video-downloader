"""Contains base functionality for loading videos."""

import datetime
import logging
import pathlib
import re
import tempfile
from abc import ABCMeta, abstractmethod
from datetime import datetime as dt
from typing import Any

import moviepy
import moviepy.tools
import requests
from moviepy import AudioFileClip, VideoFileClip
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
from selenium import webdriver
from selenium.common import TimeoutException
from selenium.webdriver.support.wait import WebDriverWait

from exceptions import QualityNotFoundError

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

DEFAULT_TITLE_PREFIX = "video"
DEFAULT_EXTENSION = ".mp4"

class LoaderBase(metaclass=ABCMeta):
    """Base class for video loader classes."""

    def __init__(self, **kwargs: Any) -> None:
        """Create a new instance of the loader class."""
        options = webdriver.ChromeOptions()
        if "user_profile" in kwargs:
            path = pathlib.Path(kwargs["user_profile"])
            # options.add_experimental_option("excludeSwitches", CHROME_DEFAULT_SWITCHES)
            options.add_argument(f"--user-data-dir={path.parent}")
            options.add_argument(f"--profile-directory={path.name}")

         # Hide browser GUI
        if kwargs["headless"]:
            options.add_argument("--headless=new")

        options.add_argument("--mute-audio")  # Mute the browser
        # options.add_argument('--disable-gpu')  # Disable GPU hardware acceleration
        # options.add_argument('--disable-dev-shm-usage')  # Overcome limited resource problems
        # options.add_argument('--no-sandbox')  # Bypass OS security model
        # options.add_argument('--disable-web-security')  # Disable web security
        # options.add_argument('--allow-running-insecure-content')  # Allow running insecure content
        # options.add_argument('--disable-webrtc')  # Disable WebRTC

        self.driver = webdriver.Chrome(options=options)
        self.output_path = pathlib.Path(kwargs["output_path"])
        self.rate = kwargs["rate"]
        self.quality = kwargs["quality"]
        self.timeout = kwargs["timeout"]
        self.exact = kwargs["exact"]
        self.overwrite = kwargs["overwrite"]
        self.logger = logging.getLogger(self.get_logger_name())

        self._ensure_no_file_or_can_overwrite()
        self._ensure_output_directory_exists()

        # Increase resource timing buffer size.
        # The default of 250 is not always enough.
        self.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": f"performance.setResourceTimingBufferSize({PERF_BUFFER_SIZE})"})

        # Clear browser cache.
        # Cached URLs are not listed in performance entries.
        self.driver.execute_cdp_cmd("Network.clearBrowserCache", {})

    def _copy_cookies(self, session: requests.Session) -> None:
        selenium_user_agent = self.driver.execute_script("return navigator.userAgent;")
        self.logger.debug("User agent: %s", selenium_user_agent)

        session.headers.update({"user-agent": selenium_user_agent})
        for cookie in self.driver.get_cookies():
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie["domain"])

    def _download_file(self, session: requests.Session, url: str) -> requests.Response:
        response = session.get(url)
        self.logger.debug("Response: %s", response)
        self.logger.debug("Headers: %s", response.headers)

        return response

    # TODO: replace info type with namedtuple or dataclass
    def _download_file_by_info(self, session: requests.Session, info: dict) -> None:
        url, bytes_pos = info["url"], info["bytes_pos"]
        with pathlib.Path(info["path"]).open("ab") as file:
            bytes_start, bytes_num = 0, self.rate * 1024
            finished = False
            while not finished:
                # TODO: consider constructing URL from the previously parsed one
                bytes_end = bytes_start + bytes_num - 1
                url = url[:bytes_pos] + f"{bytes_start}-{bytes_end}"
                self.logger.debug("URL: %s", url)

                response = self._download_file(session, url)
                if response.status_code not in RESPONSE_OK_CODES:
                    self.logger.error(
                        "Download request for bytes (%s, %s) "
                        "failed with code %s, exiting...",
                        bytes_start, bytes_end, response.status_code)
                    break

                # Get the packet size.
                headers = response.headers
                content_length = 0
                if "Content-Length" in headers:
                    content_length = int(headers["Content-Length"])
                elif "Content-Range" in headers:
                    content_range = re.split(r"\s|-|/", headers["Content-Range"])
                    start, end = int(content_range[1]), int(content_range[2])
                    content_length = end - start
                # If no headers are present for content length,
                # calculate it from the actual content.
                else:
                    content_length = sum(
                        len(chunk) for chunk
                        in response.iter_content(chunk_size=128))

                # Packet is smaller than required => file is exhausted.
                if content_length < bytes_num:
                    finished = True

                # Packet is empty => previous packet was the last.
                # Negative check is required,
                # because Content-Length header value can be negative.
                if content_length <= 0:
                    break

                for chunk in response.iter_content(chunk_size=128):
                    file.write(chunk)

                bytes_start += bytes_num

    def _write_file(self, response: requests.Response, filepath: pathlib.Path) -> None:
        with pathlib.Path(filepath).open("wb") as f:
            for chunk in response.iter_content(chunk_size=128):
                f.write(chunk)

    def _filter_urls(
            self,
            session: requests.Session,
            directory: str,
            urls: dict,
            target_quality: int) -> dict:
        pairs = { k: {} for k in urls }
        target_urls_type = None
        for urls_type, urls_list in urls.items():
            for url, bytes_pos in urls_list:
                response = self._download_file(session, url)

                content_type = response.headers.get("Content-Type", "")
                if not content_type.startswith(("audio", "video")):
                    raise ValueError("Inappropriate MIME-type.")

                filename = content_type.replace("/", f".type{urls_type}.")
                filepath = pathlib.Path(directory) / filename
                self.logger.debug("Filepath: %s", str(filepath))

                self._write_file(response, filepath)

                filename = content_type.replace("/", ".")
                if content_type.startswith("audio"):
                    pairs[urls_type]["audio"] = {
                        "url": url,
                        "bytes_pos": bytes_pos,
                        "path": pathlib.Path(directory) / filename,
                    }
                else:
                    # Don't check duration, as it may not be recognized
                    # for incomplete files.
                    infos = ffmpeg_parse_infos(str(filepath), check_duration=False)
                    self.logger.debug("Infos: %s", infos)

                    # Here, we take the minimum of width and height to also handle
                    # non-standard aspect ratios.
                    # In other words, 144p, 240p, etc. can also stand for width
                    # rather than height only.
                    quality = min(infos["video_size"])
                    pairs[urls_type]["video"] = {
                        "url": url,
                        "bytes_pos": bytes_pos,
                        "path": pathlib.Path(directory) / filename,
                        "quality": quality,
                    }

                    if quality == target_quality:
                        target_urls_type = urls_type

        if not target_urls_type:
            raise ValueError(f"Could not find content with the quality value of {target_quality}p.")

        return pairs[target_urls_type]

    def _ensure_video_accessible(self) -> None:
        access_restricted_msg = self.check_restrictions()
        if access_restricted_msg:
            self.logger.error(
                "Could not access the video. Reason: %s", access_restricted_msg)
            return

    def _ensure_filename_present_and_valid(self) -> None:
        if not self.output_path.suffix:
            # Get the video title
            try:
                title = self.get_title().strip()

                # Remove invalid characters from the title
                invalid_chars = set()
                parts = title.split()  # Split by sequences of whitespaces
                title_valid = "_".join(
                    "".join(
                        ch if (ch.isalnum() or ch == "_")
                        else (invalid_chars.add(ch) or "")  # Always empty string
                        for ch in part
                    ) for part in parts
                )

                if invalid_chars:
                    self.logger.warning(
                        "Title '%s' contains invalid characters: %s. "
                        "Title '%s' will be used instead.",
                        title, invalid_chars, title_valid)

                self.output_path /= title

            # Use a timestamp-based one if none found
            except TimeoutException:
                timestamp = dt.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
                title = f"{DEFAULT_TITLE_PREFIX}_{timestamp}"
                self.logger.exception(
                    "Could not find video title. Using '%s' instead.", title)

    def _ensure_extension_present_and_valid(self) -> None:
        suffix = self.output_path.suffix
        if (not suffix
            or not (info := moviepy.tools.extensions_dict.get(suffix[1:]))
            or info["type"] != "video"):
            self.output_path = self.output_path.with_suffix(DEFAULT_EXTENSION)

    def _ensure_no_file_or_can_overwrite(self) -> None:
        if self.output_path.suffix and self.output_path.exists() and not self.overwrite:
            self.logger.error(
                "Cannot save the video to the already existing file '%s'. "
                "Use '--overwrite' argument to be able to overwrite the existing file.",
                self.output_path)
            return

    def _ensure_output_directory_exists(self) -> None:
        # Use suffix to determine if the path points to a file or a directory.
        # This correctly assumes that entries like "folder/.ext" have no suffix,
        # i. e. they are directories.
        directory = self.output_path
        if self.output_path.suffix:
            directory = directory.parent
        pathlib.Path.mkdir(directory, parents=True, exist_ok=True)

    def _get_target_quality(self) -> None:
        self.qualities = self.get_qualities()
        target_quality = 0
        for q in self.qualities:
            if target_quality < q <= self.quality:
                target_quality = q

        self.logger.debug(
            "Qualities: %s", ", ".join(f"{q}p" for q in sorted(self.qualities)))
        if target_quality < self.quality:
            if self.exact:
                raise QualityNotFoundError

            self.logger.info(
                "Could not find quality value %sp. "
                "Using the nearest lower quality: %sp.",
                self.quality, target_quality)

        return target_quality

    # TODO: replace dict with namedtuple or dataclass
    def _get_av_urls(self) -> dict[str, list]:
        urls = (
            WebDriverWait(self.driver, self.timeout)
            .until(lambda: self.get_urls())
        )
        for urls_type, urls_list in urls.items():
            self.logger.debug("URLs, type %s: %s", urls_type, urls_list)

        return urls

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
    def get_urls(self) -> dict[int, list[str]]:
        """Get a list of direct URLs for audio/video content.

        This method is expected to return at least 2*``q_num`` URLs,
        where ``q_num`` is the number of available qualities.
        """
        ...

    def get(self, url: str) -> None:
        """Navigate to ``url``, locate the video and load it."""
        self.driver.get(url)

        self._ensure_video_accessible()
        self._ensure_filename_present_and_valid()
        self._ensure_extension_present_and_valid()
        self._ensure_no_file_or_can_overwrite()

        try:
            self.disable_autoplay()
        except TimeoutException:
            self.logger.exception(
                "Could not find an autoplay button to click, operation timed out.")

        try:
            target_quality = self._get_target_quality()
        except QualityNotFoundError:
            self.logger.exception(
                "Could not find quality value of exactly %sp, "
                "as required by --exact flag.",
                self.quality)
            return
        except TimeoutException:
            self.logger.exception(
                "Could not obtain the available qualities, operation timed out.")
            return

        try:
            urls = self._get_av_urls()
        except TimeoutException:
            self.logger.exception(
                "Could not obtain the required URLs due to a timeout.")
            return

        # Open a new session and copy user agent and cookies to it.
        # This is required so that this session is allowed to access
        # the previously obtained URLs.
        with requests.Session() as session:
            self._copy_cookies(session)
            with tempfile.TemporaryDirectory() as directory:
                self.logger.debug("Temporary directory: %s", directory)

                target = self._filter_urls(session, directory, urls, target_quality)

                audio_info = target["audio"]
                video_info = target["video"]
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
