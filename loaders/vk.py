"""Contains functionality for loading videos from vkvideo.ru."""

import pathlib
import urllib.parse as urlparser
from collections.abc import Iterable, Mapping
from typing import Any, Literal

import requests
from lxml import etree
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
from selenium.common import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.wait import WebDriverWait
from typing_extensions import override

from exceptions import (
    AmbiguousUrlsError,
    GeneratorExitError,
    InvalidMimeTypeError,
    QualityContentNotFoundError,
)

from .base import LoaderBase, MediaSpec, ResourceSpec

VALID_MIME_TYPE_PREFIXES = ("audio", "video")

# Quality name->value map, as per VK's .mpd file format.
QUALITIES = {
    "mobile": 144,
    "lowest": 240,
    "low": 360,
    "medium": 480,
    "high": 720,
    "fullhd": 1080,
}


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

    def _replay(self) -> None:
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

    def _get_urls_from_network_logs(
        self,
    ) -> dict[int, list[str]] | str | Literal[False]:
        mpd, urls, count = None, {}, 0
        network_logs = self.driver.execute_script(
            "return window.performance.getEntriesByType('resource');",
        )
        for network_log in network_logs:
            initiator_type = network_log.get("initiatorType", "")
            if initiator_type == "fetch":
                name = network_log.get("name", "")
                query = urlparser.parse_qs(urlparser.urlparse(name).query)

                # Media Presentation Description (MPD) file
                # which contains URLs for all available qualities
                if name.endswith(".mpd"):
                    mpd = name
                    break

                # URLs with byte ranges
                if "bytes" in query and query["bytes"][0].startswith("0"):
                    query_type = query["type"][0]
                    if query_type not in urls:
                        urls[query_type] = []

                    urls[query_type].append(name)
                    count += 1

        if mpd and urls:
            raise AmbiguousUrlsError(mpd, urls)

        if mpd:
            return mpd

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
        self._replay()
        return False

    def _get_urls_by_bytes(self, url: str) -> Iterable[str]:
        bytes_pos = url.find("bytes") + 6
        bytes_start, bytes_num = 0, self.rate * 1024
        while True:  # OK as this is a generator
            # TODO: consider constructing URL from the previously parsed one
            bytes_end = bytes_start + bytes_num - 1
            url = url[:bytes_pos] + f"{bytes_start}-{bytes_end}"
            self.logger.debug("URL: %s", url)
            yield url

            content_length = getattr(self, "_content_length", None)
            if content_length is None:
                raise GeneratorExitError(details="No content length was provided.")

            # Last loaded packet is smaller than required => file is exhausted
            if self._content_length < bytes_num:
                break

            self._content_length = None
            bytes_start += bytes_num

    def _get_urls_by_numbers(
        self,
        base_url: str,
        init_url: str,
        media_url: str,
        nums: Iterable[int],
    ) -> Iterable[str]:
        # First, yield the init segment
        url = base_url + init_url
        self.logger.debug("Init segment: %s", url[url.rfind("/") + 1 :])
        yield url

        # Find the '$Number$' placeholder and replace it
        # with actual number for each media segment.
        j = media_url.rfind("$")
        i = media_url[:j].rfind("$")
        for num in nums:
            url = base_url + media_url[:i] + str(num) + media_url[j + 1 :]
            self.logger.debug("Segment #%s: %s", num, url[url.rfind("/") + 1 :])
            yield url

    def _get_resource_from_mpd(
        self,
        mpd_url: str,
        mpd: Any,
        directory: pathlib.Path,
        *,
        video: bool,
    ) -> ResourceSpec:
        av_type = "video" if video else "audio"
        # TODO: consider getting rid of namespace {*} notion
        adapt = mpd.find(
            f"{{*}}Period/{{*}}AdaptationSet[@contentType='{av_type}']",
        )
        # TODO: sort by quality; Representation entries order is not guaranteed
        reps = adapt.findall("{*}Representation")
        _repr = None
        if video:
            for r in reps:
                width = int(r.get("width"))
                height = int(r.get("height"))
                if min(width, height) == self.target_quality:
                    _repr = r
        else:
            # Find the first audio track that has quality >= video quality
            q_prev = None
            for r in reps:
                q_str = r.get("quality")
                q = QUALITIES[q_str]
                if (q_prev or 0) < self.target_quality <= q:
                    _repr = r
                q_prev = q

            # If no match was found, pick the highest audio quality
            if _repr is None:
                _repr = reps[-1]

        # TODO: add distinct exceptions for audio and video errors
        if _repr is None:
            raise QualityContentNotFoundError(
                self._get_quality_with_units(self.target_quality),
            )

        _type = _repr.get("mimeType")
        file = _type.replace("/", ".")

        segtemp = _repr.find("{*}SegmentTemplate")
        start_num = int(segtemp.get("startNumber"))
        init_url = segtemp.get("initialization")
        media_url = segtemp.get("media")

        # Get the number of .m4s segments via SegmentTimeline
        count = 0
        segtime = segtemp.find("{*}SegmentTimeline")
        for s in segtime.findall("{*}S"):
            count += 1
            if r := s.get("r"):
                count += int(r)
        self.logger.debug("%s segments count: %s", av_type.capitalize(), count)

        base_url = mpd_url[: mpd_url.rfind("/") + 1]
        return ResourceSpec(
            source=self._get_urls_by_numbers(
                base_url,
                init_url,
                media_url,
                nums=range(start_num, count + 1),
            ),
            target=directory / file,
        )

    def _get_media_from_mpd(
        self,
        mpd_url: str,
        session: requests.Session,
        directory: pathlib.Path,
    ) -> MediaSpec:
        self.logger.debug("MPD URL: %s", mpd_url)
        response = self._download_resource(session, mpd_url)
        mpd = etree.fromstring(response.content, parser=etree.get_default_parser())
        return MediaSpec(
            video=self._get_resource_from_mpd(mpd_url, mpd, directory, video=True),
            audio=self._get_resource_from_mpd(mpd_url, mpd, directory, video=False),
        )

    def _get_media_from_types_map(
        self,
        types_map: Mapping[int, Iterable[str]],
        session: requests.Session,
        directory: pathlib.Path,
    ) -> MediaSpec:
        for urls_type, urls in types_map.items():
            self.logger.debug("URLs, type %s: %s", urls_type, urls)

        medias = {k: {} for k in types_map}
        media = None
        for urls_type, urls in types_map.items():
            for url in urls:
                # TODO: consider getting full file size from Content-Range header
                response = self._download_resource(session, url)

                content_type = response.headers.get("Content-Type", "")
                if not content_type.startswith(VALID_MIME_TYPE_PREFIXES):
                    raise InvalidMimeTypeError(content_type)

                file = content_type.replace("/", f".type{urls_type}.")
                path = directory / file
                self.logger.debug("Filepath: %s", path)

                self._write_file(response, path)

                file = content_type.replace("/", ".")
                if content_type.startswith("audio"):
                    medias[urls_type]["audio"] = ResourceSpec(
                        source=self._get_urls_by_bytes(url),
                        target=directory / file,
                    )
                else:
                    medias[urls_type]["video"] = ResourceSpec(
                        source=self._get_urls_by_bytes(url),
                        target=directory / file,
                    )

                    # Don't check duration, as it may not be recognized
                    # for incomplete files.
                    infos = ffmpeg_parse_infos(str(path), check_duration=False)
                    self.logger.debug("Infos: %s", infos)

                    # Here, we take the minimum of width and height to also handle
                    # non-standard aspect ratios.
                    # In other words, 144p, 240p, etc. can also stand for width
                    # rather than height only.
                    quality = min(infos["video_size"])
                    if quality == self.target_quality:
                        media = medias[urls_type]

            if media:
                break

        if not media:
            raise QualityContentNotFoundError(
                self._get_quality_with_units(self.target_quality),
            )

        return MediaSpec(audio=media["audio"], video=media["video"])

    @override
    def get_media(
        self,
        session: requests.Session,
        directory: pathlib.Path,
    ) -> MediaSpec:
        res = WebDriverWait(self.driver, self.timeout).until(
            lambda _: self._get_urls_from_network_logs(),
        )
        if isinstance(res, str):
            return self._get_media_from_mpd(res, session, directory)
        if isinstance(res, dict):
            return self._get_media_from_types_map(res, session, directory)

        raise TypeError(type(res).__name__)
