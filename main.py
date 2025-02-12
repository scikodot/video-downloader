import os
import pathlib
import requests
from moviepy import VideoFileClip, AudioFileClip
from selenium import webdriver
from selenium.webdriver.support.wait import WebDriverWait

path = pathlib.Path(__file__).parent.resolve()
url = input("Enter video URL: ")

# Setup the output path
output_dir = os.path.join(path, 'output')
os.makedirs(output_dir, exist_ok=True)

options = webdriver.ChromeOptions()
options.add_argument('--headless')  # Hide browser GUI
# options.add_argument('--disable-gpu')  # Disable GPU hardware acceleration
# options.add_argument('--disable-dev-shm-usage')  # Overcome limited resource problems
# options.add_argument('--no-sandbox')  # Bypass OS security model
# options.add_argument('--disable-web-security')  # Disable web security
# options.add_argument('--allow-running-insecure-content')  # Allow running insecure content
# options.add_argument('--disable-webrtc')  # Disable WebRTC

def get_url(drv):
    network_logs = drv.execute_script("return window.performance.getEntriesByType('resource');")
    links = []
    for network_log in network_logs:
        type = network_log.get("initiatorType", "")
        name = network_log.get("name", "")
        if type == "fetch" and name != "" and (bytes_pos := name.find("bytes=0-")) > 0:
            links.append((name, bytes_pos))
        
    return links if len(links) >= 2 else False

driver = webdriver.Chrome(options=options)
driver.get(url)

# Request the URL and wait for 20 secs or until we get both audio and video URLs
print()
links = WebDriverWait(driver, 20).until(get_url)
print("Links:", links)
print()

session = requests.Session()

# Copy user agent and cookies to the new session.
# This is required so that the requests's session is allowed to access the previously obtained URLs.
selenium_user_agent = driver.execute_script("return navigator.userAgent;")
print("User agent: ", selenium_user_agent)
print()
session.headers.update({"user-agent": selenium_user_agent})
for cookie in driver.get_cookies():
    session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])

# Download audio and video files from the obtained URLs, with byte range step of 1 MB
bytes_start = 0
bytes_end = 1024 ** 2
for name, bytes_pos in links:
    url = name[:bytes_pos] + f"bytes={bytes_start}-{bytes_end}"
    print("URL:", url)
    print()
    response = session.get(url)
    print("Response: ", response)
    print()
    print("Content: ", response.content)
    print()
    headers = response.headers
    print("Headers:", headers)
    print()
    type = headers.get('Content-Type', '')
    print("Type:", type)
    print()
    if type != "video/mp4" and type != "audio/mp4":
        raise ValueError("Inappropriate MIME-type")
    with open(os.path.join(output_dir, type.replace('/', '.')), 'wb+') as f:
        for chunk in response.iter_content(chunk_size=128):
            f.write(chunk)

# Merge the downloaded files into one (audio + video)
audio_path = os.path.join(output_dir, 'audio.mp4')
video_path = os.path.join(output_dir, 'video.mp4')
output_path = os.path.join(output_dir, 'output.mp4')
with (
    AudioFileClip(audio_path) as audio, 
    VideoFileClip(video_path) as video
):
    video.with_audio(audio).write_videofile(output_path)
