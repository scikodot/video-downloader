"""Contains functionality for loading videos from vkvideo.ru."""

import pathlib
import urllib.parse as urlparser
from abc import abstractmethod
from collections.abc import Iterable, Mapping
from typing import Literal, final

import requests
from lxml import etree
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
from selenium.common import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.shadowroot import ShadowRoot
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


class VkLoader(LoaderBase):
    """Base class for VK ecosystem."""

    @override
    def get_logger_name(self) -> str:
        return __name__

    @abstractmethod
    def replay(self) -> None:
        """Replay the video."""
        ...

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
        self.replay()
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
            if r := s.sget("r"):
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
                    self.logger.debug("FFMPEG infos: %s", infos)

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


@final
class VkVideoLoader(VkLoader):
    """Video loader for vkvideo.ru."""

    domain_url: str = "vkvideo.ru"

    def _get_shadow_root(self) -> ShadowRoot:
        return self.driver.find_element(
            By.CSS_SELECTOR,
            "vk-video-player .shadow-root-container",
        ).shadow_root

    @override
    def get_playlist_contents(self) -> list[str] | None:
        try:
            video_list = self.driver.find_element(
                By.CSS_SELECTOR,
                "div[id='video_all_list']",
            )
        except NoSuchElementException:
            self.logger.info("Could not find a playlist.")
            return None

        videos = video_list.find_elements(By.CSS_SELECTOR, "div[id^='video_item_']")
        res, i = [], 1
        for v in videos:
            try:
                a = v.find_element(By.CSS_SELECTOR, "a")
                href = a.get_attribute("href")
                if not href:
                    self.logger.debug(
                        "Could not find 'href' attribute for video #{i}.",
                    )
                    continue

                res.append(self.domain_url + href)

            except NoSuchElementException:
                self.logger.debug(
                    "Could not find subelement of type 'a' for video #{i}.",
                )

            i += 1

        return res

    # TODO: add wait
    @override
    def get_source_url(self) -> str | None:
        source = self._get_shadow_root().find_element(
            By.CSS_SELECTOR,
            "video > source",
        )

        src = source.get_attribute("src")
        if not src:
            return None

        return src.removeprefix("blob:")

    @override
    def check_restrictions(self) -> str | None:
        # The video is only available to registered users
        try:
            message = self.driver.find_element(
                By.CSS_SELECTOR,
                "span[class^='vkuiPlaceholder']",
            )
            return message.get_attribute("innerText")
        except NoSuchElementException:
            pass

        # The video is only available for subscribers
        try:
            overlay = self.driver.find_element(
                By.CSS_SELECTOR,
                "div[class^='vkitVideoCardRestrictionOverlay']",
            )
            message = overlay.find_element(By.CSS_SELECTOR, "div > span")
            return message.get_attribute("innerText")
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

    # TODO: add wait
    @override
    def disable_autoplay(self) -> None:
        autoplay = self._get_shadow_root().find_element(
            By.CSS_SELECTOR,
            "button[aria-label='Автовоспроизведение']",
        )
        if autoplay.get_attribute("aria-checked") == "true":
            autoplay.click()

    @override
    def get_title(self) -> str | None:
        title = WebDriverWait(self.driver, self.timeout).until(
            ec.visibility_of_element_located(
                (By.CSS_SELECTOR, "div[data-testid='video_modal_title']"),
            ),
        )
        return title.get_attribute("innerText")

    # TODO: add waits
    @override
    def get_qualities(self) -> set[int]:
        shadow = self._get_shadow_root()

        # Click the 'Settings' button
        self.logger.info("Waiting for Settings button to appear...")
        settings = shadow.find_element(
            By.CSS_SELECTOR,
            "button[aria-label='Настройки']",
        )
        settings.click()

        # Click the 'Quality' menu option
        self.logger.info("Waiting for Quality menu option to appear...")
        quality = shadow.find_element(By.CSS_SELECTOR, "li[aria-label^='Качество']")
        quality.click()

        # Click the 'Other' menu option
        self.logger.info("Waiting for Other menu option to appear...")
        quality_other = shadow.find_element(By.CSS_SELECTOR, "li[aria-label='Другое']")
        quality_other.click()

        # Get the list of available qualities
        self.logger.info("Waiting for quality options to appear...")
        qualities = shadow.find_elements(By.CSS_SELECTOR, "li[aria-label$='p']")

        return set(qualities)

    @override
    def replay(self) -> None:
        try:
            # First, check if the video has ended.
            # This would imply the presence of suggestions.
            suggestions = self.driver.find_element(
                By.CSS_SELECTOR,
                "div[class^='SuggestionsContainer']",
            )
            if suggestions:
                # Then, locate the replay button and click on it.
                try:
                    replay = self._get_shadow_root().find_element(
                        By.CSS_SELECTOR,
                        "button[aria-label='Начать заново']",
                    )
                    replay.click()
                except NoSuchElementException:
                    self.logger.exception("Could not locate replay button to click.")
        except NoSuchElementException:
            self.logger.exception("Could not locate suggestions element.")


@final
class OkLoader(VkLoader):
    """Video loader for ok.ru."""

    @override
    def get_playlist_contents(self) -> list[str] | None:
        raise NotImplementedError

    @override
    def get_source_url(self) -> str | None:
        try:
            wrapper = self.driver.find_element(
                By.CSS_SELECTOR,
                "vk-video-player > div > div",
            )
            self.logger.debug("Wrapper: %s", wrapper)
            shadow = wrapper.shadow_root
            self.logger.debug("Shadow: %s", shadow)
            source = shadow.find_element(By.CSS_SELECTOR, "video > source")
        except NoSuchElementException:
            return None

        src = source.get_attribute("src")
        if not src:
            return None

        return src.removeprefix("blob:")

    @override
    def check_restrictions(self) -> str | None:
        raise NotImplementedError

    @override
    def disable_autoplay(self) -> None:
        raise NotImplementedError

    @override
    def get_title(self) -> str | None:
        raise NotImplementedError

    @override
    def get_qualities(self) -> set[int]:
        raise NotImplementedError

    @override
    def replay(self) -> None:
        raise NotImplementedError
