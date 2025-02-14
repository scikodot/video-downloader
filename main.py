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

DEFAULT_OUTPUT_SUBPATH = "output"

def validate_url(url):
    if not isinstance(url, str):
        raise ValueError(f"URL must be a string, not {type(url)}.")

    if not validators.url(url):
        raise ValueError("Invalid URL.")
    
    return url

def get_default_output_path():
    directory = pathlib.Path(__file__).parent.resolve()
    return os.path.join(directory, DEFAULT_OUTPUT_SUBPATH)

def validate_output_path(output_path):
    if not isinstance(output_path, str):
        raise ValueError(f"Output path must be a string, not {type(output_path)}.")
    
    if not pathlib.Path(output_path).is_absolute():
        output_path = os.path.join(get_default_output_path(), output_path)
    
    return output_path

def get_av_urls(drv):
        network_logs = drv.execute_script("return window.performance.getEntriesByType('resource');")
        links = []
        for network_log in network_logs:
            initiator_type = network_log.get('initiatorType', '')
            name = network_log.get('name', '')
            if initiator_type == 'fetch' and name != '' and (bytes_pos := name.find('bytes=0-')) > 0:
                links.append((name, bytes_pos))
            
        return links[:2] if len(links) >= 2 else False

def main():
    parser = argparse.ArgumentParser(prog='video-downloader')
    parser.add_argument('url', help="Video URL", type=validate_url)
    parser.add_argument('-o', '--output-path', help="Output path", default=get_default_output_path(), type=validate_output_path)
    parser.add_argument('-v', '--verbose', help="Show debug info", action='store_true')
    args = parser.parse_args()

    if args.verbose:
        print("Args:", vars(args), end='\n\n')

    # Ensure the output path directory exists.
    # Use suffix to determine if the path points to a file or a directory.
    # This correctly assumes that entries like "folder/.ext" have no suffix, i. e. they are directories.
    output_path_dir = args.output_path if pathlib.Path(args.output_path).suffix == '' else os.path.dirname(args.output_path)
    os.makedirs(output_path_dir, exist_ok=True)

    options = webdriver.ChromeOptions()
    options.add_argument('--headless')  # Hide browser GUI
    # options.add_argument('--disable-gpu')  # Disable GPU hardware acceleration
    # options.add_argument('--disable-dev-shm-usage')  # Overcome limited resource problems
    # options.add_argument('--no-sandbox')  # Bypass OS security model
    # options.add_argument('--disable-web-security')  # Disable web security
    # options.add_argument('--allow-running-insecure-content')  # Allow running insecure content
    # options.add_argument('--disable-webrtc')  # Disable WebRTC

    # Request the URL and wait for 20 secs or until we get both audio and video URLs
    driver = webdriver.Chrome(options=options)
    driver.get(args.url)
    try:
        # TODO: move timeout magic to console args
        urls = WebDriverWait(driver, 20).until(get_av_urls)
        if args.verbose:
            print("URLs:", urls, end='\n\n')
    except TimeoutException:
        print("Could not obtain the required URLs. Connection timed out.")
        return

    # Open a new session and copy user agent and cookies to it.
    # This is required so that this session is allowed to access the previously obtained URLs.
    session = requests.Session()
    selenium_user_agent = driver.execute_script("return navigator.userAgent;")
    if args.verbose:
        print("User agent:", selenium_user_agent, end='\n\n')
    session.headers.update({'user-agent': selenium_user_agent})
    for cookie in driver.get_cookies():
        session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])

    # Download files from the obtained URLs, with byte range step of 1 MB, into a temporary directory
    filepaths = {}
    with tempfile.TemporaryDirectory() as temp_dir:
        if args.verbose:
            print("Temporary directory:", temp_dir)

        bytes_start = 0
        # TODO: move bytes magic to console args
        bytes_end = 1024 ** 2
        for url, bytes_pos in urls:
            url = url[:bytes_pos] + f'bytes={bytes_start}-{bytes_end}'
            if args.verbose:
                print("URL:", url, end='\n\n')

            response = session.get(url)
            if args.verbose:
                print("Response:", response, end='\n\n')

            headers = response.headers
            if args.verbose:
                print("Headers:", headers, end='\n\n')

            content_type = headers.get('Content-Type', '')
            filepath = pathlib.Path(temp_dir) / content_type.replace('/', '.')
            if args.verbose:
                print("Filepath:", str(filepath), end='\n\n')
            
            if content_type.startswith('audio'):
                filepaths['audio'] = filepath
            elif content_type.startswith('video'):
                filepaths['video'] = filepath
            else:
                raise ValueError("Inappropriate MIME-type.")
            
            with open(filepath, 'wb+') as f:
                for chunk in response.iter_content(chunk_size=128):
                    f.write(chunk)

        # Attach the video file name if the output path is a directory
        if pathlib.Path(args.output_path).suffix == '':
            args.output_path = os.path.join(args.output_path, filepaths['video'].name)

        # Merge the downloaded files into one (audio + video)
        with (
            AudioFileClip(filepaths['audio']) as audio, 
            VideoFileClip(filepaths['video']) as video
        ):
            video.with_audio(audio).write_videofile(args.output_path, codec='libx264')
    
    return

if __name__ == '__main__':
    main()
