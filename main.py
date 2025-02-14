import argparse
import os
import pathlib
import requests
import tempfile
import validators
from moviepy import VideoFileClip, AudioFileClip
from selenium import webdriver
from selenium.webdriver.support.wait import WebDriverWait
from selenium.common import TimeoutException

def main():
    def validate_url(url):
        if not isinstance(url, str):
            raise ValueError(f"URL must be a string, not {type(url)}.")

        if not validators.url(url):
            raise ValueError("Invalid URL.")
        
        return url

    def validate_output_path(path):
        if not isinstance(path, str):
            raise ValueError(f"Output path must be a string, not {type(path)}.")
        
        # TODO: add additional validation, like empty check, file extension, etc.
        
        dir = pathlib.Path(path).parent.resolve()
        os.makedirs(dir, exist_ok=True)
        return path

    parser = argparse.ArgumentParser(prog='video-downloader')
    parser.add_argument('url', help="Video URL", type=validate_url)
    parser.add_argument('-o', '--output-path', help="Output path", type=validate_output_path)
    parser.add_argument('-v', '--verbose', help="Show debug info", action='store_true')
    args = parser.parse_args()

    options = webdriver.ChromeOptions()
    options.add_argument('--headless')  # Hide browser GUI
    # options.add_argument('--disable-gpu')  # Disable GPU hardware acceleration
    # options.add_argument('--disable-dev-shm-usage')  # Overcome limited resource problems
    # options.add_argument('--no-sandbox')  # Bypass OS security model
    # options.add_argument('--disable-web-security')  # Disable web security
    # options.add_argument('--allow-running-insecure-content')  # Allow running insecure content
    # options.add_argument('--disable-webrtc')  # Disable WebRTC

    def get_av_urls(drv):
        network_logs = drv.execute_script("return window.performance.getEntriesByType('resource');")
        links = []
        for network_log in network_logs:
            initiator_type = network_log.get('initiatorType', '')
            name = network_log.get('name', '')
            if initiator_type == 'fetch' and name != '' and (bytes_pos := name.find('bytes=0-')) > 0:
                links.append((name, bytes_pos))
            
        return links[:2] if len(links) >= 2 else False

    # Request the URL and wait for 20 secs or until we get both audio and video URLs
    driver = webdriver.Chrome(options=options)
    driver.get(args.url)
    try:
        # TODO: move timeout magic to console args
        urls = WebDriverWait(driver, 20).until(get_av_urls)
        if args.verbose:
            print("URLs: ", urls, end='\n')
    except TimeoutException:
        print("Could not obtain the required URLs. Connection timed out.")
        return

    # Open a new session and copy user agent and cookies to it.
    # This is required so that this session is allowed to access the previously obtained URLs.
    session = requests.Session()
    selenium_user_agent = driver.execute_script("return navigator.userAgent;")
    if args.verbose:
        print("User agent: ", selenium_user_agent, end='\n')
    session.headers.update({'user-agent': selenium_user_agent})
    for cookie in driver.get_cookies():
        session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])

    # Download files from the obtained URLs, with byte range step of 1 MB, into a temporary directory
    with tempfile.TemporaryDirectory() as temp_dir:
        if args.verbose:
            print(f"Temporary directory: {temp_dir}")

        bytes_start = 0
        # TODO: move bytes magic to console args
        bytes_end = 1024 ** 2
        for url, bytes_pos in urls:
            url = url[:bytes_pos] + f'bytes={bytes_start}-{bytes_end}'
            if args.verbose:
                print("URL: ", url, end='\n')

            response = session.get(url)
            if args.verbose:
                print("Response: ", response, end='\n')

            headers = response.headers
            if args.verbose:
                print("Headers: ", headers, end='\n')

            content_type = headers.get('Content-Type', '')
            if content_type != 'video/mp4' and content_type != 'audio/mp4':
                raise ValueError("Inappropriate MIME-type.")
            
            with open(os.path.join(temp_dir, content_type.replace('/', '.')), 'wb+') as f:
                for chunk in response.iter_content(chunk_size=128):
                    f.write(chunk)

        # Merge the downloaded files into one (audio + video)
        audio_path = os.path.join(temp_dir, 'audio.mp4')
        video_path = os.path.join(temp_dir, 'video.mp4')
        with (
            AudioFileClip(audio_path) as audio, 
            VideoFileClip(video_path) as video
        ):
            video.with_audio(audio).write_videofile(args.output_path)
    
    return

if __name__ == '__main__':
    main()
