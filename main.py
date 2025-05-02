import argparse
import os
import pathlib
import validators

from loaders.vk import VkVideoLoader

DEFAULT_OUTPUT_SUBPATH = "output"
DEFAULT_RATE, MINIMUM_RATE = 1024, 128
DEFAULT_QUALITY, MINIMUM_QUALITY = 720, 144
DEFAULT_TIMEOUT, MINIMUM_TIMEOUT = 10, 1

class ArgumentParserCustom(argparse.ArgumentParser):
    def add_argument(self, *args, **kwargs):
        # Add empty line after every help message to visually separate entries
        if 'help' in kwargs:
            kwargs['help'] += "\n \n"
        return super().add_argument(*args, **kwargs)

def validate_url(url):
    if not validators.url(url):
        raise argparse.ArgumentTypeError("Invalid URL.")
    
    return url

def get_default_output_path():
    directory = pathlib.Path(__file__).parent.resolve()
    return os.path.join(directory, DEFAULT_OUTPUT_SUBPATH)

def validate_output_path(output_path):
    path = pathlib.Path(output_path)
    if not path.is_absolute():
        output_path = os.path.join(get_default_output_path(), output_path)
    elif path.drive and not os.path.exists(path.drive):
        raise argparse.ArgumentTypeError(f"No such drive: {path.drive}.")
    
    return output_path

def validate_rate(rate):
    rate = int(rate)
    if rate < MINIMUM_RATE:
        raise argparse.ArgumentTypeError(f"Too small value, must be at least {MINIMUM_RATE} KB(-s).")
    
    return rate

def validate_quality(quality):
    quality = int(quality)
    if quality < MINIMUM_QUALITY:
        raise argparse.ArgumentTypeError(f"Too small value, must be at least {MINIMUM_QUALITY}p.")
    
    return quality

def validate_timeout(timeout):
    timeout = int(timeout)
    if timeout < MINIMUM_TIMEOUT:
        raise argparse.ArgumentTypeError(f"Too small value, must be at least {MINIMUM_TIMEOUT} second(-s).")

    return timeout

def validate_user_profile(user_profile):
    if not os.path.isdir(user_profile):
        raise argparse.ArgumentTypeError(f"No such directory: {user_profile}.")
    
    return user_profile
    

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
                            "Where to put the downloaded video. May be absolute or relative.\n"
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

    parser.add_argument('-t', '--timeout', 
                        help=(
                            "How many seconds to wait for every operation on the page to complete.\n"
                            "Few tens of seconds is usually enough."
                        ), 
                        default=DEFAULT_TIMEOUT, 
                        type=validate_timeout)
    
    parser.add_argument('--user-profile', 
                        help="Path to the user profile to launch Chrome with.",
                        type=validate_user_profile)
    
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

    return

    loader = VkVideoLoader(**vars(args))
    loader.get(args.url)
    
    return

if __name__ == '__main__':
    main()
