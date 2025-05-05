import urllib.parse as urlparser
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.common import NoSuchElementException

from .base import LoaderBase

class VkVideoLoader(LoaderBase):
    def check_restrictions(self):
        # The video is only available for registered users and/or subscribers
        try:
            placeholder = self.driver.find_element(By.CSS_SELECTOR, "div[data-testid='placeholder_description']")
            return placeholder.get_attribute("innerText")
        except NoSuchElementException:
            pass
        
        # The video is blocked in the current geolocation
        # TODO: this must be done via response codes, not via HTML
        try:
            body = self.driver.find_element(By.CSS_SELECTOR, "body")
            elements_num = int(body.get_attribute("childElementCount"))
            if elements_num == 1:
                return body.get_attribute("innerText")
        except NoSuchElementException:
            pass
    
    def disable_autoplay(self):
        autoplay = (
            WebDriverWait(self.driver, self.timeout)
            .until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_btn_autoplay']")))
        )
        if autoplay.get_attribute("data-value-checked") == 'true':
            autoplay.click()

    def get_qualities(self):
        # Click the 'Settings' button
        if self.verbose:
            print("Waiting for Settings button to appear...")
        (
            WebDriverWait(self.driver, self.timeout)
            .until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_btn_settings']")))
            .click()
        )

        # Click the 'Quality' menu option
        if self.verbose:
            print("Waiting for Quality menu option to appear...")
        (
            WebDriverWait(self.driver, self.timeout)
            .until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_settings_menu_list_item_quality']")))
            .click()
        )

        # Get the list of available qualities
        if self.verbose:
            print("Waiting for quality options to appear...")
        quality_items = (
            WebDriverWait(self.driver, self.timeout)
            .until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_settings_menu_sublist_item']")))
            .find_element(By.XPATH, './..').find_elements(By.CSS_SELECTOR, "div[data-setting='quality']")
        )

        # Filter out the 'Auto' option with value of -1
        qualities = { q for q in (int(qi.get_attribute('data-value')) for qi in quality_items) if q > 0 }
        return qualities
    
    def get_urls(self, qualities_num):
        urls, count = {}, 0
        network_logs = self.driver.execute_script("return window.performance.getEntriesByType('resource');")
        for network_log in network_logs:
            initiator_type = network_log.get('initiatorType', '')
            if initiator_type == 'fetch':
                name = network_log.get('name', '')
                query = urlparser.parse_qs(urlparser.urlparse(name).query)
                if 'bytes' in query and query['bytes'][0].startswith('0'):
                    query_type = query['type'][0]
                    if query_type not in urls:
                        urls[query_type] = []
                    bytes_pos = name.find('bytes')
                    urls[query_type].append((name, bytes_pos + 6))
                    count += 1
        
        if self.verbose:
            urls_num = {k: len(v) for k, v in urls.items()}
            print(f"Number of URLs obtained by type: {urls_num}. Total number of performance entries: {len(network_logs)}.")
        
        if count >= 2 * qualities_num:
            return urls

        # If there was not enough URLs, try to replay the video.
        # If the video is too short, not all URLs may get requested on the first play.
        # The replay enables sending the absent URLs requests once again.
        #
        # Here, we first check if the video has ended,
        # and then locate the replay button to click on it.
        try:
            video_ui = self.driver.find_element(By.CSS_SELECTOR, "div[class='videoplayer_ui']")
            video_state = video_ui.get_attribute('data-state')
            # TODO: move magics to constants
            if video_state == 'ended':
                try:
                    replay_button = video_ui.find_element(By.CSS_SELECTOR, "div[class~='videoplayer_btn_play']")
                    replay_button.click()
                except NoSuchElementException:
                    print("Could not locate replay button to click.")
        except NoSuchElementException:
            print("Could not locate video UI element.")
        
        return False
