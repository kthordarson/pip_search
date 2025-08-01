import json
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
from playwright.async_api import async_playwright

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

async def get_session_with_playwright(args: argparse.Namespace, config: Any) -> httpx.AsyncClient:
    """Create session using browser automation to handle JavaScript challenges."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Navigate to PyPI search page and let it complete the challenge
        await page.goto("https://pypi.org/search/?q=requests", wait_until="networkidle")

        # Wait for the challenge to complete - try multiple approaches
        challenge_completed = False

        # First, wait for the challenge page to potentially resolve itself
        await asyncio.sleep(5)

        # Check if we're still on challenge page
        page_content = await page.content()
        if "Client Challenge" in page_content:
            logger.debug("Still on challenge page, waiting longer...")
            # Wait up to 30 seconds for challenge to complete
            try:
                await page.wait_for_function(
                    "!document.title.includes('Client Challenge')",
                    timeout=30000
                )
                challenge_completed = True
            except Exception as e:
                logger.warning(f"Challenge did not complete automatically: {e}")
        else:
            challenge_completed = True

        if challenge_completed:
            # Try to find search results or at least confirm we're on the real search page
            try:
                # Wait for either search results or the main search form
                await page.wait_for_selector('form[action="/search/"]', timeout=5000)
                logger.debug("Found search form - challenge appears to be solved")
            except Exception as e:
                logger.error(f"{e} {type(e)} No search form found, but proceeding anyway")

        # Extract cookies from the browser session
        cookies = await context.cookies()
        await browser.close()

        # Create httpx client with the cookies from the browser
        client = httpx.AsyncClient()
        for cookie in cookies:
            client.cookies.set(
                cookie['name'],
                cookie['value'],
                domain=cookie.get('domain', 'pypi.org')
            )

        # Log the cookies for debugging
        if args.debug:
            logger.debug(f"Extracted cookies: {[c['name'] for c in cookies]}")

        return client

async def get_session(args: argparse.Namespace, config: Any) -> httpx.AsyncClient:
    """Create and initialize an HTTP client session with PyPI authentication.

    Args:
        args: Command-line arguments
        config: Configuration object

    Returns:
        Initialized HTTP client
    """
    # query = args.query
    # query = "".join(query)
    qurl = config.api_url + f"?q={args.query}"
    # f'{config.api_url}?q={args.query}'

    client = httpx.AsyncClient()
    headers1 = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    params = {"q": args.query}
    try:
        # response = await client.get(config.api_url, params=params, headers=headers)
        response = await client.get(qurl, params=params)
        await asyncio.sleep(1)  # Give some time for the session to be established
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
            script_url = f"https://pypi.org/{rematch.group(1)}/script.js?reload=true"
        else:
            logger.error("Could not find script.js URL in the response.")
            raise ValueError("Invalid response from PyPI, script.js URL not found.")
    except IndexError:
        logger.error("Could not find script.js URL in the response.")
        raise ValueError("Invalid response from PyPI, script.js URL not found.")
    script_resp = None
    try:
        script_resp = await client.get(script_url)
        await asyncio.sleep(1)  # Give some time for the session to be established
    except Exception as e:
        logger.error(f"Error fetching script.js: {e} {type(e)}")
        raise
    if script_resp.status_code != 200:
        logger.warning(f"Failed to fetch script.js, status code: {script_resp.status_code} from {script_url}")
        # raise ValueError(f"Invalid response from PyPI, status code: {script_resp.status_code}")
    if script_resp:
        challenge_pattern = r'init\(\[(.*?)\],\s*"(.+?)",\s*"(.+?)",?\s*(true|false)?\);'
        challenge_regex = re.compile(challenge_pattern, re.DOTALL)
        try:
            challenge_match = challenge_regex.search(script_resp.text)
            if not challenge_match:
                raise IndexError("Challenge pattern not found")
            challenge_json, token, script_path, *_ = challenge_match.groups()
            # Parse the challenge JSON
            challenge_list = json.loads(f"[{challenge_json}]")
            challenge = challenge_list[0]
            challenge_type = challenge.get("ty")
            challenge_data = challenge.get("data", {})
        except (ValueError, IndexError, json.JSONDecodeError) as e:
            logger.error(f"{e} {type(e)} Could not find challenge data in script.js from script_url: {script_url} \nscript_resp: {script_resp.text}\n")
            return httpx.AsyncClient()

        # Prepare the answer for the challenge
        answer_data = None
        if challenge_type == "pow":
            base = challenge_data["base"]
            hash = challenge_data["hash"]
            hmac = challenge_data["hmac"]
            expires = challenge_data["expires"]
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
            answer_data = {
                "ty": "pow",
                "base": base,
                "answer": answer,
                "hmac": hmac,
                "expires": expires
            }
        elif challenge_type == "pat":
            # "pat" challenge usually just needs an empty dict
            answer_data = {
                "ty": "pat",
                "auth": ""
            }
        else:
            logger.error(f"Unknown challenge type: {challenge_type}")
            return httpx.AsyncClient()

        # Send the challenge answer
        back_url = f"https://pypi.org{script_path}/fst-post-back"
        data = {
            "token": token,
            "data": [answer_data]
        }
        await client.post(back_url, json=data)
        await asyncio.sleep(1)  # Give some time for the session to be established

        if args.debug:
            logger.debug(f"Challenge answer sent to {back_url} with data: {data}")
            # Test the session with a search request to verify it works
            test_resp = await client.get("https://pypi.org/search/?q=requests")
            logger.debug(f"Test search response status: {test_resp.status_code}")
            logger.debug(f"Test search HTML (first 500 chars): {test_resp.text[:500]}")

            # If still getting challenge page, try to handle the redirect/completion
            if "Client Challenge" in test_resp.text:
                logger.warning("Still getting challenge page after solving challenge")
                # Try following any redirects or making another request
                await asyncio.sleep(2)
                test_resp = await client.get("https://pypi.org/search/?q=requests")
                logger.debug(f"Second test search response status: {test_resp.status_code}")
                logger.debug(f"Second test search HTML (first 500 chars): {test_resp.text[:500]}")

            # Log cookies to verify session state
            logger.debug(f"Session cookies after challenge: {client.cookies}")

        return client

    #     # Find the PoW data from script.js
    #     pow_pattern = r'init\(\[\{"ty":"pow","data":\{"base":"(.+?)","hash":"(.+?)","hmac":"(.+?)","expires":"(.+?)"\}\}\], "(.+?)"'
    #     pow_regex = re.compile(pow_pattern)
    #     try:
    #         base, hash, hmac, expires, token = pow_regex.findall(script_resp.text)[0]
    #     except (ValueError, IndexError) as e:
    #         logger.error(f"{e} {type(e)} Could not find PoW data in script.js from script_url: {script_url} \nscript_resp: {script_resp.text}\n")
    #         return httpx.AsyncClient()
    #         # raise ValueError("Invalid response from PyPI, PoW data not found.")

    #     # Compute the PoW answer
    #     answer = ""
    #     characters = string.ascii_letters + string.digits
    #     for c1 in characters:
    #         for c2 in characters:
    #             c = base + c1 + c2
    #             if hashlib.sha256(c.encode()).hexdigest() == hash:
    #                 answer = c1 + c2
    #                 break
    #         if answer:
    #             break

    #     # Send the PoW answer
    #     back_url = f"https://pypi.org/{script_path}/fst-post-back"
    #     data = {
    #         "token": token,
    #         "data": [
    #             {"ty": "pow", "base": base, "answer": answer, "hmac": hmac, "expires": expires}
    #         ],
    #     }
    #     await client.post(back_url, json=data)
    # return client

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
