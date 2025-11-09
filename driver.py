"""Web driver related functionality."""

import logging
import pathlib
from types import TracebackType
from typing import Any

from selenium import webdriver
from selenium.webdriver.remote.webdriver import WebDriver
from typing_extensions import override


def _get_driver_base_class() -> type[WebDriver]:
    return webdriver.Chrome


class CustomWebDriver(_get_driver_base_class()):
    """Same as ``WebDriver`` but with added functionality like logging, etc."""

    _logger: logging.Logger
    url: str

    @override
    def __init__(self, logger: logging.Logger, *args: Any, **kwargs: Any) -> None:
        self._logger = logger
        self._logger.info("Setting up driver...")
        super().__init__(*args, **kwargs)

    @override
    def get(self, url: str) -> None:
        self.url = url  # Cache URL for later use
        super().get(url)

    @override
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._logger.info("Closing driver...")
        super().__exit__(exc_type, exc, traceback)


def get_driver_options(
    *,
    user_profile: str | None,
    headless: bool,
) -> webdriver.ChromeOptions:
    """Get web driver options."""
    options = webdriver.ChromeOptions()
    if user_profile:
        path = pathlib.Path(user_profile)
        # options.add_experimental_option("excludeSwitches", CHROME_DEFAULT_SWITCHES)
        options.add_argument(f"--user-data-dir={path.parent}")
        options.add_argument(f"--profile-directory={path.name}")

    # Hide browser GUI
    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--mute-audio")  # Mute the browser
    # options.add_argument('--disable-gpu')  # Disable GPU hardware acceleration
    # options.add_argument('--disable-dev-shm-usage')  # Overcome limited resource problems
    # options.add_argument('--no-sandbox')  # Bypass OS security model
    # options.add_argument('--disable-web-security')  # Disable web security
    # options.add_argument('--allow-running-insecure-content')  # Allow running insecure content
    # options.add_argument('--disable-webrtc')  # Disable WebRTC
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return options
