import argparse
import os
import pathlib
import requests
import tempfile
import urllib.parse as urlparser
import validators
from moviepy import VideoFileClip, AudioFileClip
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
from selenium import webdriver
from selenium.webdriver.support.wait import WebDriverWait
from selenium.common import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec

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
    WebDriverWait(drv, 10).until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_btn_settings']"))).click()
    WebDriverWait(drv, 10).until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_settings_menu_list_item_quality']"))).click()
    quality_sublist = WebDriverWait(drv, 10).until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_settings_menu_sublist_item']")))
    quality_items = quality_sublist.find_element(By.XPATH, './..').find_elements(By.CSS_SELECTOR, "div[data-setting='quality']")
    qualities = set()
    for quality_item in quality_items:
        q = int(quality_item.get_attribute('data-value'))
        if q > 0:
            qualities.add(q)

    # TODO: add control with --verbose
    print("Qualities:", ', '.join(f"{q}p" for q in sorted(qualities)))
    
    # At this point all audio/video resources must be loaded.
    # Since we know the number of quality presets (say, N), we have to retrieve 2*N URLs in total.
    network_logs = drv.execute_script("return window.performance.getEntriesByType('resource');")
    links = []
    for network_log in network_logs:
        initiator_type = network_log.get('initiatorType', '')
        name = network_log.get('name', '')
        if initiator_type == 'fetch' and (bytes_pos := name.find('bytes=0-')) > 0:
            links.append((name, bytes_pos))
    
    return links if len(links) >= 2 * len(qualities) else False

def download_file(args, session, url):
    response = session.get(url)
    if args.verbose:
        print("Response:", response)
        print("Headers:", response.headers, end='\n\n')

    return response

def write_file(response, filepath):
    with open(filepath, 'wb+') as f:
        for chunk in response.iter_content(chunk_size=128):
            f.write(chunk)

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
        urls = WebDriverWait(driver, 120).until(get_av_urls)
        if args.verbose:
            print("URLs:")
            for url in urls:
                print(url)
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

    with tempfile.TemporaryDirectory() as temp_dir:
        if args.verbose:
            print("Temporary directory:", temp_dir)

        # Determine which (pair) of the URLs is to be downloaded
        pairs = {}
        target_pair_type = None
        for url, bytes_pos in urls:
            # Get the URL query params as a dict
            parsed = urlparser.urlparse(url)
            query = urlparser.parse_qs(parsed.query)

            if query['type'][0] not in pairs.keys():
                pairs[query['type'][0]] = {}
            
            response = download_file(args, session, url)

            content_type = response.headers.get('Content-Type', '')
            if not content_type.startswith(('audio', 'video')):
                raise ValueError("Inappropriate MIME-type.")
            
            filepath = pathlib.Path(temp_dir) / content_type.replace('/', f".type{query['type'][0]}.")
            if args.verbose:
                print("Filepath:", str(filepath), end='\n\n')

            write_file(response, filepath)
            
            if content_type.startswith('audio'):
                pairs[query['type'][0]]['audio'] = { 'url': url, 'bytes_pos': bytes_pos }
            else:
                quality = ffmpeg_parse_infos(str(filepath))['video_size'][1]
                pairs[query['type'][0]]['video'] = { 'url': url, 'bytes_pos': bytes_pos, 'quality': quality }

                if not target_pair_type or pairs[target_pair_type]['video']['quality'] < quality:
                    target_pair_type = query['type'][0]

        # Download the target audio and video files, 
        # with byte range step of 1 MB, into a temporary directory
        filepaths = {}
        bytes_start = 0
        # TODO: move bytes magic to console args
        bytes_end = 1024 ** 2
        for filetype in ('audio', 'video'):
            info = pairs[target_pair_type][filetype]
            url, bytes_pos = info['url'], info['bytes_pos']
            url = url[:bytes_pos] + f'bytes={bytes_start}-{bytes_end}'
            if args.verbose:
                print("URL:", url, end='\n\n')

            response = download_file(args, session, url)

            content_type = response.headers.get('Content-Type', '')
            filepath = pathlib.Path(temp_dir) / content_type.replace('/', '.')
            if args.verbose:
                print("Filepath:", str(filepath), end='\n\n')

            write_file(response, filepath)

            filepaths[filetype] = filepath

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
