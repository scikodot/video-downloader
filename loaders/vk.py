"""Contains functionality for loading videos from vkvideo.ru."""

import pathlib
import urllib.parse as urlparser
from collections.abc import Iterable
from typing import Literal

import requests
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
from selenium.common import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.wait import WebDriverWait
from typing_extensions import override

from exceptions import (
    GeneratorExitError,
    InvalidMimeTypeError,
    QualityContentNotFoundError,
)

from .base import LoaderBase

VALID_MIME_TYPE_PREFIXES = ("audio", "video")


class VkVideoLoader(LoaderBase):
    """Video loader for vkvideo.ru."""

    @override
    def get_logger_name(self) -> str:
        return __name__

    @override
    def check_restrictions(self) -> str | None:
        # The video is only available for registered users and/or subscribers
        try:
            placeholder = self.driver.find_element(
                By.CSS_SELECTOR,
                "div[data-testid='placeholder_description']",
            )
            return placeholder.get_attribute("innerText")
        except NoSuchElementException:
            pass

        # The video is blocked in the current geolocation
        # TODO: this must be done via response codes, not via HTML
        try:
            body = self.driver.find_element(By.CSS_SELECTOR, "body")
            if elem_count_str := body.get_attribute("childElementCount"):
                elem_count = int(elem_count_str)
                if elem_count == 1:
                    return body.get_attribute("innerText")
        except NoSuchElementException:
            pass

    @override
    def disable_autoplay(self) -> None:
        autoplay = WebDriverWait(self.driver, self.timeout).until(
            ec.element_to_be_clickable(
                (By.CSS_SELECTOR, "div[class~='videoplayer_btn_autoplay']"),
            ),
        )
        if autoplay.get_attribute("data-value-checked") == "true":
            autoplay.click()

    @override
    def get_title(self) -> str | None:
        title = WebDriverWait(self.driver, self.timeout).until(
            ec.visibility_of_element_located(
                (By.CSS_SELECTOR, "div[data-testid='video_modal_title']"),
            ),
        )
        return title.get_attribute("innerText")

    @override
    def get_qualities(self) -> set[int]:
        # Click the 'Settings' button
        self.logger.info("Waiting for Settings button to appear...")
        (
            WebDriverWait(self.driver, self.timeout)
            .until(
                ec.element_to_be_clickable(
                    (By.CSS_SELECTOR, "div[class~='videoplayer_btn_settings']"),
                ),
            )
            .click()
        )

        # Click the 'Quality' menu option
        self.logger.info("Waiting for Quality menu option to appear...")
        (
            WebDriverWait(self.driver, self.timeout)
            .until(
                ec.element_to_be_clickable(
                    (
                        By.CSS_SELECTOR,
                        "div[class~='videoplayer_settings_menu_list_item_quality']",
                    ),
                ),
            )
            .click()
        )

        # Get the list of available qualities
        self.logger.info("Waiting for quality options to appear...")
        quality_items = (
            WebDriverWait(self.driver, self.timeout)
            .until(
                ec.element_to_be_clickable(
                    (
                        By.CSS_SELECTOR,
                        "div[class~='videoplayer_settings_menu_sublist_item']",
                    ),
                ),
            )
            .find_element(By.XPATH, "./..")
            .find_elements(By.CSS_SELECTOR, "div[data-setting='quality']")
        )

        # Filter out the 'Auto' option with value of -1
        quality_values = (qi.get_attribute("data-value") for qi in quality_items)
        qualities = (int(qv) for qv in quality_values if qv)
        return {q for q in qualities if q > 0}

    def _get_urls_from_network_logs(
        self,
    ) -> dict[int, list[str]] | Literal[False]:
        urls, count = {}, 0
        network_logs = self.driver.execute_script(
            "return window.performance.getEntriesByType('resource');",
        )
        for network_log in network_logs:
            initiator_type = network_log.get("initiatorType", "")
            if initiator_type == "fetch":
                name = network_log.get("name", "")
                query = urlparser.parse_qs(urlparser.urlparse(name).query)
                if "bytes" in query and query["bytes"][0].startswith("0"):
                    query_type = query["type"][0]
                    if query_type not in urls:
                        urls[query_type] = []

                    urls[query_type].append(name)
                    count += 1

        urls_num = {k: len(v) for k, v in urls.items()}
        self.logger.debug(
            "Number of URLs obtained by type: %s. "
            "Total number of performance entries: %s.",
            urls_num,
            len(network_logs),
        )

        if count >= 2 * len(self.qualities):
            return urls

        # If there was not enough URLs, try to replay the video.
        # If the video is too short, not all URLs may get requested on the first play.
        # The replay enables sending the absent URLs requests once again.
        #
        # Here, we first check if the video has ended,
        # and then locate the replay button to click on it.
        try:
            video_ui = self.driver.find_element(
                By.CSS_SELECTOR,
                "div[class='videoplayer_ui']",
            )
            video_state = video_ui.get_attribute("data-state")
            if video_state == "ended":
                try:
                    replay_button = video_ui.find_element(
                        By.CSS_SELECTOR,
                        "div[class~='videoplayer_btn_play']",
                    )
                    replay_button.click()
                except NoSuchElementException:
                    self.logger.exception("Could not locate replay button to click.")
        except NoSuchElementException:
            self.logger.exception("Could not locate video UI element.")

        return False

    def _get_file_parts_urls(self, url: str) -> Iterable[str]:
        bytes_pos = url.find("bytes") + 6
        bytes_start, bytes_num = 0, self.rate * 1024
        while True:  # OK as this is a generator
            # TODO: consider constructing URL from the previously parsed one
            bytes_end = bytes_start + bytes_num - 1
            url = url[:bytes_pos] + f"{bytes_start}-{bytes_end}"
            self.logger.debug("URL: %s", url)
            yield url

            content_length = getattr(self, "_content_length", None)
            if not content_length:
                raise GeneratorExitError(details="No content length was provided.")

            # Last loaded packet is smaller than required => file is exhausted
            if self._content_length < bytes_num:
                break

            bytes_start += bytes_num

    # TODO: replace dict with namedtuple or dataclass
    @override
    def get_urls(
        self,
        session: requests.Session,
        directory: pathlib.Path,
    ) -> dict[str, dict[str, Iterable[str] | str]]:
        urls = WebDriverWait(self.driver, self.timeout).until(
            lambda _: self._get_urls_from_network_logs(),
        )
        for urls_type, urls_list in urls.items():
            self.logger.debug("URLs, type %s: %s", urls_type, urls_list)

        pairs = {k: {} for k in urls}
        target_urls_type = None
        for urls_type, urls_list in urls.items():
            for url in urls_list:
                # TODO: consider getting full file size from Content-Range header
                response = self._download_file(session, url)

                content_type = response.headers.get("Content-Type", "")
                if not content_type.startswith(VALID_MIME_TYPE_PREFIXES):
                    raise InvalidMimeTypeError(content_type)

                filename = content_type.replace("/", f".type{urls_type}.")
                filepath = directory / filename
                self.logger.debug("Filepath: %s", str(filepath))

                self._write_file(response, filepath)

                filename = content_type.replace("/", ".")
                if content_type.startswith("audio"):
                    pairs[urls_type]["audio"] = {
                        "urls": self._get_file_parts_urls(url),
                        "path": directory / filename,
                    }
                else:
                    pairs[urls_type]["video"] = {
                        "urls": self._get_file_parts_urls(url),
                        "path": directory / filename,
                    }

                    # Don't check duration, as it may not be recognized
                    # for incomplete files.
                    infos = ffmpeg_parse_infos(str(filepath), check_duration=False)
                    self.logger.debug("Infos: %s", infos)

                    # Here, we take the minimum of width and height to also handle
                    # non-standard aspect ratios.
                    # In other words, 144p, 240p, etc. can also stand for width
                    # rather than height only.
                    quality = min(infos["video_size"])
                    if quality == self.target_quality:
                        target_urls_type = urls_type

        if not target_urls_type:
            raise QualityContentNotFoundError(
                self._get_quality_with_units(self.target_quality),
            )

        return pairs[target_urls_type]
