"""Various utilities for loaders."""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum, auto
from logging import Logger
from typing import Any, Self, TypeVar

from lxml import etree
from requests import Response
from typing_extensions import override

# Import whole module instead of specific exceptions.
# Otherwise it would cause a circular import error,
# as this module is referenced by exceptions' module.
import constants
from exceptions import TooSmallValueError
from loaders import exceptions


class MediaType(StrEnum):
    """Enumeration class of known media types."""

    AUDIO = auto()
    VIDEO = auto()

    @classmethod
    def from_mime_type(cls, mime_type: str) -> Self:
        """Get ``MediaType`` object corresponding to the specified ``mime_type``."""
        for media_type in cls:
            if mime_type.startswith(media_type):
                return media_type

        raise exceptions.InvalidMimeTypeError(mime_type)


T = TypeVar("T")


def proxy_attr(proxy_name: str) -> Callable[[type[T]], type[T]]:
    """Proxy attribute access to the attribute with the specified name.

    Returns a new class with a patched ``__getattribute__`` method
    that uses the same method of the ``proxy_name`` attribute.

    In other words, ``@proxy_attr("obj")`` makes calls to ``self.attr``
    return ``self.obj.attr``, and calls to ``self.obj`` return the ``obj`` itself.
    """

    def decorator(cls: type[T]) -> type[T]:
        _getattribute = cls.__getattribute__

        def getattribute_proxy(self: T, name: str) -> Any:  # noqa: ANN401
            # Return source attribute if it is defined
            if name in cls.__dict__:
                return _getattribute(self, name)

            # Return proxy itself if it is requested
            proxy = _getattribute(self, proxy_name)
            if name == proxy_name:
                return proxy

            # Return proxy attribute otherwise
            return proxy.__getattribute__(name)

        cls.__getattribute__ = getattribute_proxy
        return cls

    return decorator


@proxy_attr("element")
class MpdElement(etree._Element):  # noqa: SLF001
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


MINIMUM_CHUNK_SIZE = 1
DEFAULT_SEGMENTS_COUNT, MINIMUM_SEGMENTS_COUNT = 1024, 1
DEFAULT_SLEEP_THRESHOLD, MINIMUM_SLEEP_THRESHOLD = 0.005, 0


@dataclass
class LimitedResponseOptions:
    """Options used in methods of ``LimitedResponse`` class."""

    speed_limit: float | None = None
    segments_count: int = DEFAULT_SEGMENTS_COUNT
    sleep_threshold: float = DEFAULT_SLEEP_THRESHOLD

    def _validate_speed_limit(self) -> None:
        if self.speed_limit is not None and self.speed_limit <= 0:
            raise TooSmallValueError(
                self.speed_limit,
                lower_bound=0,
                inclusive=False,
                units="Mibps",
            )

    def _validate_segments_count(self) -> None:
        if (
            self.segments_count is not None
            and self.segments_count < MINIMUM_SEGMENTS_COUNT
        ):
            raise TooSmallValueError(
                self.segments_count,
                lower_bound=MINIMUM_SEGMENTS_COUNT,
                inclusive=True,
                units="",
                indent="",
            )

    def _validate_sleep_threshold(self) -> None:
        if (
            self.sleep_threshold is not None
            and self.sleep_threshold < MINIMUM_SLEEP_THRESHOLD
        ):
            raise TooSmallValueError(
                self.sleep_threshold,
                lower_bound=MINIMUM_SLEEP_THRESHOLD,
                inclusive=True,
                units="second(-s)",
            )

    def __post_init__(self) -> None:
        """Post-init validation routine."""
        self._validate_speed_limit()
        self._validate_segments_count()
        self._validate_sleep_threshold()


@proxy_attr("response")
class LimitedResponse(Response):
    """Wrapper class for ``requests.Response``.

    Allows limiting the connection speed
    with a simple amortized traffic balancing algorithm.
    """

    def __init__(self, response: Response) -> None:
        """Create a new wrapper for ``response``."""
        self.response = response

    def _validate_chunk_size(self, chunk_size: int | None) -> None:
        if chunk_size is not None and chunk_size < MINIMUM_CHUNK_SIZE:
            raise TooSmallValueError(
                chunk_size,
                lower_bound=MINIMUM_CHUNK_SIZE,
                inclusive=True,
                units="byte(-s)",
            )

    def iter_content(
        self,
        chunk_size: int | None = None,
        decode_unicode: bool = False,  # noqa: FBT001, FBT002
        options: LimitedResponseOptions | None = None,
        logger: Logger | None = None,
    ) -> Iterator[Any]:
        """Iterate over the response data, same as ``requests.Response.iter_content``.

        This method, however, interprets ``chunk_size`` parameter
        as a **maximum** chunk size, as the actual chunk size is calculated in process,
        and hence uses a ``None`` default value to not limit the chunk size
        if the limit is not provided explicitly.

        ``options`` parameter can be used for limiting the download speed.
        """
        self._validate_chunk_size(chunk_size)

        if not options:
            options = LimitedResponseOptions()

        # Return the content as-is if no speed limit is provided.
        if options.speed_limit is None:
            return self.response.iter_content(chunk_size, decode_unicode)

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
        r = options.speed_limit * constants.BYTES_PER_MEBIBIT
        k = options.segments_count
        s = options.sleep_threshold

        r0 = int(r // k)  # bytes per segment
        t0 = 1 / k  # segment duration

        # Clamp the number of bytes per segment to the max chunk size.
        # This ensures the chunks do not occupy too much memory for high speed limits.
        c = r0 if not chunk_size else min(r0, chunk_size)

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
                    "Speed: %.3f Mibps; Raw download time: %f; Lag time: %f",
                    b / (tc_end - start) / constants.BYTES_PER_MEBIBIT,
                    tc,
                    tl,
                )

            # If the lag is too significant, suspend proceeding
            # to the next segment until the lag is eliminated.
            if tl < -s:
                time.sleep(-tl)

            tc_start = time.perf_counter()
