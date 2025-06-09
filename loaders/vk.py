"""Contains functionality for loading videos from vkvideo.ru."""

import pathlib
import urllib.parse as urlparser
from collections.abc import Iterable, Mapping
from typing import Literal

import requests
from lxml import etree
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
from selenium.common import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.wait import WebDriverWait
from typing_extensions import override

import constants
from loaders.base import (
    LoaderBase,
    MediaSpec,
    ResourceSpec,
)
from loaders.exceptions import (
    AmbiguousUrlsError,
    MediaNotFoundError,
    MimeTypeNotFoundError,
)
from loaders.utils import MediaType, MpdElement

HTTP_BLOCKED = 451
HTTP_BLOCKED_NAME = "Unavailable For Legal Reasons"
# Attribute names the are allowed to be kept in main MPD tag.
MPD_ATTR_WHITELIST = {"mediaPresentationDuration"}
# Quality name->value map, as per VK's .mpd file format.
MPD_QUALITIES = {
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
        status_code = self._get_status_code()
        if status_code == HTTP_BLOCKED:
            try:
                body = self.driver.find_element(By.CSS_SELECTOR, "body")
                return body.get_attribute("innerText")
            except NoSuchElementException:
                return HTTP_BLOCKED_NAME

        return None

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

    def _get_urls_by_bytes(self, url: str) -> Iterable[tuple[str, int | None]]:
        url_parsed = urlparser.urlparse(url)
        url_query = urlparser.parse_qs(url_parsed.query)
        bytes_start, bytes_num = 0, self.chunk_size * constants.BYTES_PER_KIBIBYTE
        while True:
            bytes_end = bytes_start + bytes_num - 1
            url_query["bytes"][0] = f"{bytes_start}-{bytes_end}"
            url = url_parsed._replace(
                query=urlparser.urlencode(url_query, doseq=True),
            ).geturl()
            self.logger.debug("URL: %s", url)
            yield url, bytes_num

            bytes_start += bytes_num

    def _get_urls_by_numbers(
        self,
        base_url: str,
        init_url: str,
        media_url: str,
        nums: Iterable[int],
    ) -> Iterable[tuple[str, int | None]]:
        # First, yield the init segment.
        url = base_url + init_url
        self.logger.debug("Init segment: %s", url[url.rfind("/") + 1 :])
        yield url, None

        # Find the '$Number$' placeholder and replace it
        # with actual number for each media segment.
        j = media_url.rfind("$")
        i = media_url[:j].rfind("$")
        for num in nums:
            url = base_url + media_url[:i] + str(num) + media_url[j + 1 :]
            self.logger.debug("Segment #%s: %s", num, url[url.rfind("/") + 1 :])
            yield url, None

    def _get_quality_from_representation(self, r: MpdElement) -> int:
        q_str = r.get("quality")
        return MPD_QUALITIES[q_str]

    def _get_audio_representation(
        self,
        rs: list[MpdElement],
    ) -> MpdElement | None:
        rs_map = {self._get_quality_from_representation(r): r for r in rs}
        rs_sorted = sorted(rs_map.items())

        # Find the first audio track of quality >= video quality
        q_prev = None
        for q, r in rs_sorted:
            if (q_prev or 0) < self.target_quality <= q:
                return r
            q_prev = q

        # If no match was found, pick the highest audio quality
        return rs_sorted[-1][1] if rs else None

    def _get_video_representation(
        self,
        rs: list[MpdElement],
    ) -> MpdElement | None:
        for r in rs:
            if self._get_quality_from_representation(r) == self.target_quality:
                return r
        return None

    def _get_resource_from_mpd(
        self,
        root: MpdElement,
        media_type: MediaType,
        base_url: str,
        directory: pathlib.Path,
    ) -> ResourceSpec:
        self.logger.debug("Getting %s resource...", media_type)

        adapt = root.find(
            f"Period/AdaptationSet[@contentType='{media_type}']",
        )
        self.logger.debug("AdaptationSet: %s", adapt.attrib)

        reps = adapt.findall("Representation")
        self.logger.debug("Representations count: %s", len(reps))

        rep = None
        match media_type:
            case MediaType.VIDEO:
                rep = self._get_video_representation(reps)
            case MediaType.AUDIO:
                rep = self._get_audio_representation(reps)

        if rep is None:
            raise MediaNotFoundError(
                media_type,
                self.target_quality,
            )
        self.logger.debug("Representation: %s", rep.attrib)

        mime_type = rep.get("mimeType")
        file = mime_type.replace("/", ".")

        segtemp = rep.find("SegmentTemplate")
        self.logger.debug("SegmentTemplate: %s", segtemp.attrib)

        start_num = int(segtemp.get("startNumber"))
        init_url = segtemp.get("initialization")
        media_url = segtemp.get("media")

        # Get the number of .m4s segments via SegmentTimeline
        count = 0
        segtime = segtemp.find("SegmentTimeline")
        for s in segtime.findall("S"):
            count += 1
            if r := s.get("r"):
                count += int(r)
        self.logger.debug("Segments count: %s", count)

        return ResourceSpec(
            source=self._get_urls_by_numbers(
                base_url,
                init_url,
                media_url,
                nums=range(start_num, count + 1),
            ),
            target=directory / file,
        )

    def _remove_mpd_ns(self, xml: str) -> str:
        # Find indices where MPD attributes definition starts and ends.
        start = xml.find("<MPD")
        attr_end = start + xml[start:].find(">")
        attr_start = start + 5

        # No attributes present.
        if attr_end <= attr_start:
            return xml

        # Filter out all attributes that do not belong to the whitelist,
        # including primarily namespace declaring ones.
        attrs = xml[attr_start:attr_end].split()
        attrs_to_keep = [
            attr for attr in attrs if attr.split("=", 1)[0] in MPD_ATTR_WHITELIST
        ]
        return xml[:attr_start] + " ".join(attrs_to_keep) + xml[attr_end:]

    def _get_media_from_mpd(
        self,
        mpd_url: str,
        session: requests.Session,
        directory: pathlib.Path,
    ) -> MediaSpec:
        self.logger.debug("MPD URL: %s", mpd_url)
        response = self._download_resource(session, mpd_url)

        # Remove namespaces from the retrieved XML before building the tree.
        # This helps navigating the tree without specifying elements' namespaces,
        # as there is anyway only a single namespace.
        xml_no_ns = self._remove_mpd_ns(response.text)
        mpd_root = MpdElement(etree.fromstring(xml_no_ns.encode("utf8")))

        base_url = mpd_url[: mpd_url.rfind("/") + 1]
        return MediaSpec(
            video=self._get_resource_from_mpd(
                mpd_root,
                MediaType.VIDEO,
                base_url,
                directory,
            ),
            audio=self._get_resource_from_mpd(
                mpd_root,
                MediaType.AUDIO,
                base_url,
                directory,
            ),
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

                mime_type = response.headers.get("Content-Type")
                if not mime_type:
                    raise MimeTypeNotFoundError

                media_type = MediaType.from_mime_type(mime_type)

                file = mime_type.replace("/", f".type{urls_type}.")
                path = directory / file
                self.logger.debug("Filepath: %s", path)

                self._write_file(response, path)

                file = mime_type.replace("/", ".")
                medias[urls_type][media_type] = ResourceSpec(
                    source=self._get_urls_by_bytes(url),
                    target=directory / file,
                )
                if media_type == MediaType.VIDEO:
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
            raise MediaNotFoundError(
                MediaType.VIDEO,
                self.target_quality,
            )
        if MediaType.AUDIO not in media:
            raise MediaNotFoundError(
                MediaType.AUDIO,
                self.target_quality,
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
