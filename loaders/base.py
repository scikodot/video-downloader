import os
import pathlib
import requests
import tempfile
from abc import ABCMeta, abstractmethod
from moviepy import VideoFileClip, AudioFileClip
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
from selenium import webdriver
from selenium.webdriver.support.wait import WebDriverWait
from selenium.common import TimeoutException

class LoaderBase(metaclass=ABCMeta):
    def __init__(self, **kwargs):
        options = webdriver.ChromeOptions()
        options.add_argument('--headless=new')  # Hide browser GUI
        options.add_argument("--mute-audio")  # Mute the browser
        # options.add_argument('--disable-gpu')  # Disable GPU hardware acceleration
        # options.add_argument('--disable-dev-shm-usage')  # Overcome limited resource problems
        # options.add_argument('--no-sandbox')  # Bypass OS security model
        # options.add_argument('--disable-web-security')  # Disable web security
        # options.add_argument('--allow-running-insecure-content')  # Allow running insecure content
        # options.add_argument('--disable-webrtc')  # Disable WebRTC

        self.driver = webdriver.Chrome(options=options)
        self.output_path = kwargs['output_path']
        self.rate = kwargs['rate']
        self.quality = kwargs['quality']
        self.timeout = kwargs['timeout']
        self.verbose = kwargs['verbose']

    def _copy_cookies(self, session):
        selenium_user_agent = self.driver.execute_script("return navigator.userAgent;")
        if self.verbose:
            print("User agent:", selenium_user_agent, end='\n\n')
        
        session.headers.update({'user-agent': selenium_user_agent})
        for cookie in self.driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])

    def _download_file(self, session, url):
        response = session.get(url)
        if self.verbose:
            print("Response:", response)
            print("Headers:", response.headers, end='\n\n')

        return response
    
    def _download_file_by_info(self, session, info):
        url, bytes_pos = info['url'], info['bytes_pos']
        with open(info['path'], 'ab') as file:
            bytes_start, bytes_num = 0, self.rate * 1024
            while True:
                # TODO: consider constructing URL from the previously parsed one
                url = url[:bytes_pos] + f'{bytes_start}-{bytes_start + bytes_num - 1}'
                if self.verbose:
                    print("URL:", url, end='\n\n')
                
                response = self._download_file(session, url)
                if response.status_code < 200 or response.status_code >= 300:
                    break

                for chunk in response.iter_content(chunk_size=128):
                    file.write(chunk)
                
                bytes_start += bytes_num
    
    def _write_file(self, response, filepath):
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=128):
                f.write(chunk)

    def _filter_urls(self, session, directory, urls, target_quality):
        pairs = { k: {} for k in urls.keys() }
        target_urls_type = None
        for urls_type, urls_list in urls.items():
            for url, bytes_pos in urls_list:
                response = self._download_file(session, url)

                content_type = response.headers.get('Content-Type', '')
                if not content_type.startswith(('audio', 'video')):
                    raise ValueError("Inappropriate MIME-type.")
                
                filepath = pathlib.Path(directory) / content_type.replace('/', f".type{urls_type}.")
                if self.verbose:
                    print("Filepath:", str(filepath), end='\n\n')

                self._write_file(response, filepath)
                
                if content_type.startswith('audio'):
                    pairs[urls_type]['audio'] = { 
                        'url': url, 
                        'bytes_pos': bytes_pos, 
                        'path': pathlib.Path(directory) / content_type.replace('/', ".")
                    }
                else:
                    infos = ffmpeg_parse_infos(str(filepath))
                    if self.verbose:
                        print("Infos:", infos, end='\n\n')
                    
                    quality = infos['video_size'][1]
                    pairs[urls_type]['video'] = { 
                        'url': url, 
                        'bytes_pos': bytes_pos, 
                        'path': pathlib.Path(directory) / content_type.replace('/', "."), 
                        'quality': quality
                    }

                    if quality == target_quality:
                        target_urls_type = urls_type

        if not target_urls_type:
            raise ValueError(f"Could not find content with the quality value of {target_quality}p.")
        
        return pairs[target_urls_type]

    # Returns a list of available qualities.
    @abstractmethod
    def get_qualities(self):
        ...

    # Returns a list of direct URLs for the audio/video content.
    # This method must return at most 2*Q_n URLs, where Q_n is the number of available qualities.
    # It is assumed that at the moment this method is called, all audio/video resources are already loaded.
    @abstractmethod
    def get_urls(self, qualities_num):
        ...

    def get(self, url):
        self.driver.get(url)
        try:
            qualities = self.get_qualities()
            target_quality = 0
            for q in qualities:
                if target_quality < q <= self.quality:
                    target_quality = q

            if self.verbose:
                print("Qualities:", ', '.join(f"{q}p" for q in sorted(qualities)))
                if target_quality < self.quality:
                    print(f"Could not find quality value {self.quality}p. Using the nearest lower quality: {target_quality}p.")

            urls = WebDriverWait(self.driver, self.timeout).until(lambda _: self.get_urls(len(qualities)))
            if self.verbose:
                print("URLs:")
                for urls_type, urls_list in urls.items():
                    print(f"{urls_type}:", urls_list)
                print()

        except TimeoutException:
            print("Could not obtain the required URLs due to a timeout.")
            return
        
        # Open a new session and copy user agent and cookies to it.
        # This is required so that this session is allowed to access the previously obtained URLs.
        with requests.Session() as session:
            self._copy_cookies(session)
            with tempfile.TemporaryDirectory() as directory:
                if self.verbose:
                    print("Temporary directory:", directory, end='\n\n')

                target = self._filter_urls(session, directory, urls, target_quality)

                audio_info = target['audio']
                video_info = target['video']
                self._download_file_by_info(session, audio_info)
                self._download_file_by_info(session, video_info)

                # Append the video file name if the output path is a directory
                # TODO: get rid of os.path.join, use pathlib.Path instead
                if not pathlib.Path(self.output_path).suffix:
                    self.output_path = os.path.join(self.output_path, video_info['path'].name)

                # Merge the downloaded files into one (audio + video)
                with (
                    AudioFileClip(audio_info['path']) as audio, 
                    VideoFileClip(video_info['path']) as video
                ):
                    video.with_audio(audio).write_videofile(self.output_path, codec='libx264')
    