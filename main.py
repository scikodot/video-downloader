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
DEFAULT_RATE, MINIMUM_RATE = 1024, 128
DEFAULT_QUALITY, MINIMUM_QUALITY = 720, 144

class ArgumentParserCustom(argparse.ArgumentParser):
    def add_argument(self, *args, **kwargs):
        # Add empty line after every help message to visually separate entries
        if 'help' in kwargs:
            kwargs['help'] += "\n \n"
        return super().add_argument(*args, **kwargs)

def validate_url(url):
    if not validators.url(url):
        raise ValueError("Invalid URL.")
    
    return url

def get_default_output_path():
    directory = pathlib.Path(__file__).parent.resolve()
    return os.path.join(directory, DEFAULT_OUTPUT_SUBPATH)

def validate_output_path(output_path):
    if not pathlib.Path(output_path).is_absolute():
        output_path = os.path.join(get_default_output_path(), output_path)
    
    return output_path

def validate_rate(rate):
    rate = int(rate)
    if rate < MINIMUM_RATE:
        raise ValueError(f"Too small value, must be at least {MINIMUM_RATE} KBs per request.")
    
    return rate

def validate_quality(quality):
    quality = int(quality)
    if quality < MINIMUM_QUALITY:
        raise ValueError(f"Too small value, must be at least {MINIMUM_QUALITY}p.")
    
    return quality

def get_av_urls(drv, count):    
    # At this point all audio/video resources must be loaded.
    # Since we know the number of quality values (say, N), we have to retrieve 2*N URLs in total.
    network_logs = drv.execute_script("return window.performance.getEntriesByType('resource');")
    links = []
    for network_log in network_logs:
        initiator_type = network_log.get('initiatorType', '')
        name = network_log.get('name', '')
        if initiator_type == 'fetch' and (bytes_pos := name.find('bytes=0-')) > 0:
            links.append((name, bytes_pos + 6))
    
    return links if len(links) >= 2 * count else False

def download_file(args, session, url):
    response = session.get(url)
    if args.verbose:
        print("Response:", response)
        print("Headers:", response.headers, end='\n\n')

    return response

def write_file(response, filepath):
    with open(filepath, 'wb') as f:
        for chunk in response.iter_content(chunk_size=128):
            f.write(chunk)

def main():
    parser = ArgumentParserCustom(
        prog='video-downloader', 
        formatter_class=argparse.RawTextHelpFormatter, 
        add_help=False)
    
    parser.add_argument('url', 
                        help="Video URL.", 
                        type=validate_url)
    
    parser.add_argument('-h', '--help', 
                        help="Show this help message and exit.", 
                        action='help', 
                        default=argparse.SUPPRESS)

    parser.add_argument('-o', '--output-path', 
                        help=(
                            "Where to put the downloaded video. "
                            "May be absolute or relative.\n"
                            "If relative, the video will be saved at the specified path under the directory the program was run from.\n"
                            f"If omitted, the video will be saved to the '{DEFAULT_OUTPUT_SUBPATH}' path under the directory the program was run from."
                        ), 
                        default=get_default_output_path(),
                        type=validate_output_path)
    
    parser.add_argument('-r', '--rate', 
                        help=(
                            "How many kilobytes (KBs) to download on every request.\n"
                            "Higher rates are advised for longer videos."
                        ), 
                        default=DEFAULT_RATE, 
                        type=validate_rate)
    
    parser.add_argument('-q', '--quality', 
                        help=(
                            "Which quality the downloaded video must have.\n"
                            "This parameter determines the maximum quality.\n"
                            "That is, the first quality value lower than or equal to this parameter value will be used."
                        ), 
                        default=DEFAULT_QUALITY, 
                        type=validate_quality)
    
    parser.add_argument('-v', '--verbose', 
                        help="Show detailed information about performed actions.", 
                        action='store_true')
    
    args = parser.parse_args()

    if args.verbose:
        print("Args:", vars(args), end='\n\n')

    # Ensure the output path directory exists.
    # Use suffix to determine if the path points to a file or a directory.
    # This correctly assumes that entries like "folder/.ext" have no suffix, i. e. they are directories.
    output_path_dir = args.output_path if not pathlib.Path(args.output_path).suffix else os.path.dirname(args.output_path)
    os.makedirs(output_path_dir, exist_ok=True)

    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')  # Hide browser GUI
    options.add_argument("--mute-audio")  # Mute the browser
    # options.add_argument('--disable-gpu')  # Disable GPU hardware acceleration
    # options.add_argument('--disable-dev-shm-usage')  # Overcome limited resource problems
    # options.add_argument('--no-sandbox')  # Bypass OS security model
    # options.add_argument('--disable-web-security')  # Disable web security
    # options.add_argument('--allow-running-insecure-content')  # Allow running insecure content
    # options.add_argument('--disable-webrtc')  # Disable WebRTC

    driver = webdriver.Chrome(options=options)
    driver.get(args.url)
    try:
        # Determine which quality values are available
        WebDriverWait(driver, 10).until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_btn_settings']"))).click()
        WebDriverWait(driver, 10).until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_settings_menu_list_item_quality']"))).click()
        quality_sublist = WebDriverWait(driver, 10).until(ec.element_to_be_clickable((By.CSS_SELECTOR, "div[class~='videoplayer_settings_menu_sublist_item']")))
        quality_items = quality_sublist.find_element(By.XPATH, './..').find_elements(By.CSS_SELECTOR, "div[data-setting='quality']")
        quality_target, qualities = 0, set()
        for quality_item in quality_items:
            q = int(quality_item.get_attribute('data-value'))
            if q > 0:
                qualities.add(q)
                if quality_target < q <= args.quality:
                    quality_target = q

        if args.verbose:
            print("Qualities:", ', '.join(f"{q}p" for q in sorted(qualities)))
            if quality_target < args.quality:
                print(f"Could not find quality value {args.quality}p. Using the nearest lower quality: {quality_target}p.")

        # TODO: move timeout magic to console args
        urls = WebDriverWait(driver, 30).until(lambda d: get_av_urls(d, len(qualities)))
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
                pairs[query['type'][0]]['audio'] = { 
                    'url': url, 
                    'bytes_pos': bytes_pos, 
                    'path': pathlib.Path(temp_dir) / content_type.replace('/', ".")
                }
            else:
                quality = ffmpeg_parse_infos(str(filepath))['video_size'][1]
                pairs[query['type'][0]]['video'] = { 
                    'url': url, 
                    'bytes_pos': bytes_pos, 
                    'path': pathlib.Path(temp_dir) / content_type.replace('/', "."), 
                    'quality': quality
                }

                if quality == quality_target:
                    target_pair_type = query['type'][0]

        if not target_pair_type:
            raise ValueError(f"Could not find content with the quality value of {quality_target}p.")

        # Download the target audio and video files, 
        # with the rate from CLI args, into a temporary directory
        audio_info = pairs[target_pair_type]['audio']
        video_info = pairs[target_pair_type]['video']
        for info in (audio_info, video_info):
            url, bytes_pos = info['url'], info['bytes_pos']
            with open(info['path'], 'ab') as file:
                bytes_start, bytes_num = 0, args.rate * 1024
                while True:
                    url = url[:bytes_pos] + f'{bytes_start}-{bytes_start + bytes_num - 1}'
                    if args.verbose:
                        print("URL:", url, end='\n\n')
                    
                    response = download_file(args, session, url)
                    if response.status_code < 200 or response.status_code >= 300:
                        break

                    for chunk in response.iter_content(chunk_size=128):
                        file.write(chunk)
                    
                    bytes_start += bytes_num

        # Attach the video file name if the output path is a directory
        if not pathlib.Path(args.output_path).suffix:
            args.output_path = os.path.join(args.output_path, video_info['path'].name)

        # Merge the downloaded files into one (audio + video)
        with (
            AudioFileClip(audio_info['path']) as audio, 
            VideoFileClip(video_info['path']) as video
        ):
            video.with_audio(audio).write_videofile(args.output_path, codec='libx264')
    
    return

if __name__ == '__main__':
    main()
