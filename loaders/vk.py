import urllib.parse as urlparser
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec

from .base import LoaderBase

class VkVideoLoader(LoaderBase):
    def get_qualities(self):
        # Click the 'Settings' button
        (
            WebDriverWait(self.driver, self.timeout)
            .until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_btn_settings']")))
            .click()
        )

        # Click the 'Quality' menu option
        (
            WebDriverWait(self.driver, self.timeout)
            .until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_settings_menu_list_item_quality']")))
            .click()
        )

        # Get the list of available qualities
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
        
        return urls if count >= 2 * qualities_num else False
