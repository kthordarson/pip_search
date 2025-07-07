import asyncio
import re
import string
import hashlib
from typing import Union, Tuple, List, Dict, Any, Optional, Sequence
import argparse
import glob
import os
import httpx
from bs4 import BeautifulSoup, Tag
from loguru import logger

try:
    from importlib.metadata import PackageNotFoundError, distribution
except ImportError as e:
    # logger.warning(f"pip_search importlib.metadata module not found: {e} {type(e)}")
    from pkg_resources import DistributionNotFound as PackageNotFoundError
    from pkg_resources import get_distribution as distribution


try:
    from . import __version__
except (ModuleNotFoundError, ImportError) as e:
    # logger.warning(f"pip_search module not found: {e} {type(e)}")
    __version__ = "0.0.0"


def check_version(package_name: str) -> Union[str, bool]:
    """Check if package is installed and return version.

    Returns:
        str | bool: Version of package if installed, False otherwise.
    """
    try:
        installed = distribution(package_name)
    except PackageNotFoundError:
        return False
    else:
        return installed.version


async def get_session(args: argparse.Namespace, config: Any) -> httpx.AsyncClient:
    """Create and initialize an HTTP client session with PyPI authentication.

    Args:
        args: Command-line arguments
        config: Configuration object

    Returns:
        Initialized HTTP client
    """
    query = args.query
    query = "".join(query)
    qurl = config.api_url + f"?q={query}"

    client = httpx.AsyncClient()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    }
    params = {"q": query}
    try:
        response = await client.get(config.api_url, params=params, headers=headers)
    except httpx.ConnectError as e:
        logger.error(f'{e} {type(e)} url: {config.api_url} params: {params}')
        raise
    except Exception as e:
        logger.error(f"Error creating HTTP client session: {e} {type(e)} url: {config.api_url} params: {params}")
        raise
    # Get script.js url
    try:
        # pattern = re.compile(r"/(.*)/script.js")
        # script_path = pattern.findall(response.text)[0]
        # script_url = f"https://pypi.org/{script_path}/script.js?reload=true"
        rematch = re.search(r'/([^/]+)/script\.js\?reload=true', response.text, re.UNICODE)
        if rematch:
            script_path = rematch.group(1)
            script_url = f"https://pypi.org/{script_path}/script.js'  # ?reload=true"
        else:
            logger.error("Could not find script.js URL in the response.")
            raise ValueError("Invalid response from PyPI, script.js URL not found.")
    except IndexError:
        logger.error("Could not find script.js URL in the response.")
        raise ValueError("Invalid response from PyPI, script.js URL not found.")
    try:
        script_resp = await client.get(script_url)
    except Exception as e:
        logger.error(f"Error fetching script.js: {e} {type(e)}")
        raise
    if script_resp.status_code != 200:
        logger.error(f"Failed to fetch script.js, status code: {script_resp.status_code}")
        # raise ValueError(f"Invalid response from PyPI, status code: {script_resp.status_code}")
    # Find the PoW data from script.js
    pow_pattern = r'init\(\[\{"ty":"pow","data":\{"base":"(.+?)","hash":"(.+?)","hmac":"(.+?)","expires":"(.+?)"\}\}\], "(.+?)"'
    pow_regex = re.compile(pow_pattern)
    try:
        base, hash, hmac, expires, token = pow_regex.findall(script_resp.text)[0]
    except (ValueError, IndexError) as e:
        logger.error(f"{e} {type(e)} Could not find PoW data in script.js from script_url: {script_url} \nscript_resp: {script_resp.text}\n")
        raise ValueError("Invalid response from PyPI, PoW data not found.")

    # Compute the PoW answer
    answer = ""
    characters = string.ascii_letters + string.digits
    for c1 in characters:
        for c2 in characters:
            c = base + c1 + c2
            if hashlib.sha256(c.encode()).hexdigest() == hash:
                answer = c1 + c2
                break
        if answer:
            break

    # Send the PoW answer
    back_url = f"https://pypi.org/{script_path}/fst-post-back"
    data = {
        "token": token,
        "data": [
            {"ty": "pow", "base": base, "answer": answer, "hmac": hmac, "expires": expires}
        ],
    }
    await client.post(back_url, json=data)
    return client

def get_args() -> Tuple[argparse.ArgumentParser, argparse.Namespace]:
    """Parse command line arguments.

    Returns:
        Tuple of (argument_parser, parsed_args)
    """
    ap = argparse.ArgumentParser(prog="pip_search", description="Search for packages on PyPI")
    ap.add_argument("-s","--sort",type=str, const="name",nargs="?",choices=['name', 'version', 'released', 'stars','watchers','forks'],help="sort results by package name, version or release date (default: %(const)s)")
    ap.add_argument("query", nargs="*", type=str, help="terms to search pypi.org package repository")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--date_format", type=str, default="%d-%m-%Y", nargs="?", help="format for release date, (default: %(default)s)")
    ap.add_argument("-e", "--extra", action="store_true", default=False, help="get extra github info")
    ap.add_argument("-d", "--debug", action="store_true", default=False, help="debugmode")
    ap.add_argument("-l", "--links", action="store_true", default=False, help="show links")
    ap.add_argument("--locallibs", action="store", default=False, help="check local libs ~/lib/pythonxxx/site-packages", dest="locallibs")
    args = ap.parse_args()
    return ap, args
