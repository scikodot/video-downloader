"""Various utilities for loaders."""

import time
from collections.abc import Iterator
from enum import StrEnum, auto
from logging import Logger
from typing import Any, Self

from lxml import etree
from requests import Response
from typing_extensions import override

# Import whole module instead of specific exceptions.
# Otherwise it would cause a circular import error,
# as this module is referenced by exceptions' module.
from loaders import exceptions


class MediaType(StrEnum):
    """Enumeration class of known media types."""

    AUDIO = auto()
    VIDEO = auto()

    @classmethod
    def from_mime_type(cls, mime_type: str) -> Self:
        for media_type in cls:
            if mime_type.startswith(media_type):
                return media_type

        raise exceptions.InvalidMimeTypeError(mime_type)


class MpdElement(etree._Element):  # noqa: SLF001
    # TODO: use identifiers in docstrings, like :class:, etc.?
    """Wrapper class for ``lxml.etree._Element``.

    Raises exceptions instead of returning ``None`` when nothing is found.
    """

    def __init__(self, element: etree._Element) -> None:
        """Create a new wrapper for ``element``."""
        self.element = element

    @override
    def find(self, path, namespaces=None) -> "MpdElement":  # noqa: ANN001
        res = self.element.find(path, namespaces)
        if res is None:
            raise exceptions.InvalidMpdError
        return MpdElement(res)

    # Ignore override typing; base method returns
    # a specific type (list[etree._Element], which is invariant)
    # instead of a more general one, hence no opportunity for typesafe subtyping.
    @override
    def findall(self, path, namespaces=None) -> "list[MpdElement]":  # type: ignore[override] # noqa: ANN001
        res = self.element.findall(path, namespaces)
        if not res:
            raise exceptions.InvalidMpdError
        return [MpdElement(elem) for elem in res]

    # Ignore override typing; base methods are @overload'ed,
    # @override cannot determine the right version,
    # and overriding the base implementation (with 'default' param) is unnecessary.
    @override
    def get(self, key) -> str:  # type: ignore[override]  # noqa: ANN001
        res = self.element.get(key)
        if not res:
            raise exceptions.InvalidMpdError
        return res

    # Re-route all attribute access to the underlying element,
    # save for the element itself.
    @override
    def __getattribute__(self, name: str) -> Any:
        element = super().__getattribute__("element")
        if name == "element":
            return element
        return element.__getattribute__(name)


DEFAULT_SPEED_LIMIT = 1024
DEFAULT_CHUNK_SIZE = 1024**2
DEFAULT_SEGMENTS_COUNT = 1024
DEFAULT_SLEEP_THRESHOLD = 0.005
BYTES_PER_MEGABIT = 128 * 1024
BYTES_PER_KILOBYTE = 1024


class LimitedResponse(Response):
    """Wrapper class for ``requests.Response``.

    Allows limiting the connection speed
    with a simple amortized traffic balancing algorithm.
    """

    def __init__(self, response: Response) -> None:
        """Create a new wrapper for ``response``."""
        self.response = response

    def iter_content(  # type: ignore[override]
        self,
        *,
        # chunk_size: int | None = 1,
        decode_unicode: bool = False,
        speed_limit: int = DEFAULT_SPEED_LIMIT,
        segments_count: int = DEFAULT_SEGMENTS_COUNT,
        sleep_threshold: float = DEFAULT_SLEEP_THRESHOLD,
        logger: Logger | None = None,
    ) -> Iterator[Any]:
        """Iterate over the response data, same as ``requests.Response.iter_content``.

        This method, however, does not have its regular ``chunk_size`` parameter,
        as it is calculated automatically.

        Other parameters can be used for limiting the download speed.
        """
        # Instead of messing with sockets, let's use a naive approach:
        # 1. Let `r` -- max download speed.
        # 2. Partition every 1 second time interval into `k` segments,
        #    of `t0 = 1 / k` seconds duration each.
        # 3. Load data in `r / k` chunks, suspending further download
        #    until the elapsed time for the current segment hits `t0`.
        #
        # This ensures the download speed per second does not exceed `r`,
        # and also somewhat averages the speed over every second.
        #
        # This algorithm, however, *does not* limit the *actual* download speed
        # (i. e., network bandwidth); it only ensures the number of bytes
        # loaded per `1 / k` seconds is no more than `r / k`.
        # That is why we call it "amortized".

        # Use short names for better readability.
        r, k, s = speed_limit * BYTES_PER_KILOBYTE, segments_count, sleep_threshold

        r0 = int(r // k)  # bytes per segment
        t0 = 1 / k  # segment duration

        # Clamp the number of bytes per segment to the max chunk size.
        # This ensures the chunks do not occupy too much memory for high speed limits.
        c = min(r0, DEFAULT_CHUNK_SIZE)

        if logger:
            logger.debug(
                "iter_content: r = %i; k = %i; s = %f; r0 = %i; t0 = %f; c = %i",
                r,
                k,
                s,
                r0,
                t0,
                c,
            )

        b = 0  # total number of bytes downloaded
        tc = 0  # time per chunk download
        tl = 0  # cumulative time lag
        tc_start, tc_end = time.perf_counter(), 0
        tl_start, tl_end = time.perf_counter(), 0
        start = time.perf_counter()
        for chunk in self.response.iter_content(c, decode_unicode):
            # This counter only measures the actual (raw) time
            # it takes iter_content() to download a chunk of data.
            tc_end = time.perf_counter()

            yield chunk
            b += len(chunk)
            tc = tc_end - tc_start

            tl_end = time.perf_counter()

            # The lag is cumulative, so add the discrepancy to it.
            tl += (tl_end - tl_start) - t0

            # Just add the end time instead of calling perf_counter() again.
            # This does not include few arithmetic operations from the prev line,
            # but on the other hand skips a (potentially costly) function call.
            tl_start = tl_end

            if logger:
                logger.debug(
                    "Speed: %.3f Mbps; Raw download time: %f; Lag time: %f",
                    b / (tc_end - start) / BYTES_PER_MEGABIT,
                    tc,
                    tl,
                )

            # If the lag is too significant, suspend proceeding
            # to the next segment until the lag is eliminated.
            if tl < -s:
                time.sleep(-tl)

            tc_start = time.perf_counter()
