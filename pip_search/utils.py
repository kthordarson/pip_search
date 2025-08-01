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
    # Create client with more realistic browser behavior
    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    )
    # Initialize the session by accessing the main page first
    try:
        initial_resp = await client.get("https://pypi.org/")
        if args.debug:
            logger.debug(f"Initial page status: {initial_resp.status_code}")
        await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Error accessing PyPI main page: {e} {type(e)}")

    # Now try to access the search page to trigger and solve the challenge
    query = getattr(args, 'query', 'test')
    if isinstance(query, list):
        query = " ".join(query)

    # Function to solve challenges
    async def solve_challenge(response_text):
        if args.debug:
            logger.debug(f"solve_challenge cookies: {client.cookies} headers {client.headers}")
        # Extract the challenge script URL
        try:
            rematch = re.search(r'/_fs-ch-([^/]+)/script\.js\?reload=true', response_text)
            if not rematch:
                rematch = re.search(r'/([^/]+)/script\.js\?reload=true', response_text)

            if rematch:
                script_path = rematch.group(1)
                script_url = f"https://pypi.org/_fs-ch-{script_path}/script.js?reload=true"
                if '/_fs-ch-' not in script_url:
                    script_url = f"https://pypi.org/{script_path}/script.js?reload=true"
            else:
                logger.error("Could not find script.js URL in the response.")
                return False
        except Exception as e:
            logger.error(f"Error extracting script URL: {e} {type(e)}")
            return False

        # Get the challenge script
        try:
            script_resp = await client.get(script_url)
            if args.debug:
                logger.debug(f"Script URL: {script_url}, Status: {script_resp.status_code}")
                logger.debug(f"Challenge script content (first 1500 chars): {script_resp.text[:1500]}")
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error fetching script.js: {e} {type(e)}")
            return False

        if script_resp.status_code != 200:
            logger.warning(f"Failed to fetch script.js, status code: {script_resp.status_code}")
            return False

        # Extract and solve the challenge
        try:
            # Try multiple regex patterns to find the challenge
            patterns = [
                r'init\(\[(.*?)\],\s*"(.+?)",\s*"(.+?)"',
                r'init\(\[(.*?)\],\s*"(.+?)",\s*"(.+?)",?\s*(true|false)?\);',
                r'init\(\[\{"ty":"pow","data":\{"base":"(.+?)","hash":"(.+?)","hmac":"(.+?)","expires":"(.+?)"\}\}\],\s*"(.+?)"'
            ]

            challenge_match = None
            for pattern in patterns:
                regex = re.compile(pattern, re.DOTALL)
                match = regex.search(script_resp.text)
                if match:
                    challenge_match = match
                    break

            if not challenge_match:
                logger.error("Challenge pattern not found in script.js")
                return False

            # Handle different challenge formats
            groups = challenge_match.groups()
            if len(groups) >= 3 and '{' in groups[0]:  # JSON format
                challenge_json, token, script_path, *_ = groups
                challenge_list = json.loads(f"[{challenge_json}]")
                challenge = challenge_list[0]
                challenge_type = challenge.get("ty")
                challenge_data = challenge.get("data", {})

                # Prepare the answer
                answer_data = None
                if challenge_type == "pow":
                    base = challenge_data["base"]
                    hash_val = challenge_data["hash"]
                    hmac = challenge_data["hmac"]
                    expires = challenge_data["expires"]

                    # Solve the proof of work challenge
                    answer = ""
                    characters = string.ascii_letters + string.digits
                    for c1 in characters:
                        for c2 in characters:
                            c = base + c1 + c2
                            if hashlib.sha256(c.encode()).hexdigest() == hash_val:
                                answer = c1 + c2
                                break
                        if answer:
                            break

                    if not answer:
                        logger.error("Failed to solve PoW challenge")
                        return False

                    answer_data = {
                        "ty": "pow",
                        "base": base,
                        "answer": answer,
                        "hmac": hmac,
                        "expires": expires
                    }
                elif challenge_type == "pat":
                    answer_data = {
                        "ty": "pat",
                        "auth": ""
                    }
                else:
                    logger.error(f"Unknown challenge type: {challenge_type}")
                    return False

                # Get the correct back URL
                back_url = f"https://pypi.org{script_path}/fst-post-back"
                if '/_fs-ch-' in script_url:
                    parts = script_url.split('/_fs-ch-')
                    base_domain = parts[0]
                    challenge_id = parts[1].split('/')[0]
                    back_url = f"{base_domain}/_fs-ch-{challenge_id}/fst-post-back"

                # Submit the challenge answer
                data = {
                    "token": token,
                    "data": [answer_data]
                }

                answer_resp = await client.post(back_url, json=data)
                if args.debug:
                    logger.debug(f"Challenge answer sent to {back_url}, status: {answer_resp.status_code}")
                await asyncio.sleep(2)  # Give more time for the session to be established
                return True

            else:  # Old format with direct regex groups
                base, hash_val, hmac, expires, token = groups

                # Solve the PoW challenge
                answer = ""
                characters = string.ascii_letters + string.digits
                for c1 in characters:
                    for c2 in characters:
                        c = base + c1 + c2
                        if hashlib.sha256(c.encode()).hexdigest() == hash_val:
                            answer = c1 + c2
                            break
                    if answer:
                        break

                if not answer:
                    logger.error("Failed to solve PoW challenge")
                    return False

                # Submit the challenge answer
                back_url = f"https://pypi.org/{script_path}/fst-post-back"
                data = {
                    "token": token,
                    "data": [
                        {"ty": "pow", "base": base, "answer": answer, "hmac": hmac, "expires": expires}
                    ]
                }

                answer_resp = await client.post(back_url, json=data)
                if args.debug:
                    logger.debug(f"Challenge answer sent to {back_url}, status: {answer_resp.status_code}")
                await asyncio.sleep(2)

                return True

        except Exception as e:
            logger.error(f"Error solving challenge: {e}")
            return False

        return False

    # Try to access the search page and handle challenges
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            qurl = f"{config.api_url}?q={query}"
            response = await client.get(qurl)
            await asyncio.sleep(1)

            if "Client Challenge" in response.text:
                if args.debug:
                    logger.warning(f"Challenge detected on attempt {attempt+1}")

                solved = await solve_challenge(response.text)
                if solved:
                    if args.debug:
                        logger.debug(f"Challenge solved successfully on attempt {attempt+1} cookies: {client.cookies} headers {client.headers}")
                    # Verify the challenge is solved
                    # test_resp = await client.get("https://pypi.org/search/?q=test")
                    test_resp = await client.get(f"https://pypi.org/search/?q={query}")
                    if args.debug:
                        logger.debug(f"Challenge test_resp cookies: {client.cookies} headers {client.headers}")
                    if "Client Challenge" not in test_resp.text:
                        if args.debug:
                            logger.debug("Challenge solved successfully")
                        break
                    else:
                        logger.warning(f"Still getting challenge page after attempt {attempt+1} solved: {solved} test_resp.text: {test_resp.text}")
                        # Increase the delay for next attempt
                        await asyncio.sleep(2 * (attempt + 1))
                else:
                    logger.warning(f"Failed to solve challenge on attempt {attempt+1}")
            else:
                # No challenge detected
                if args.debug:
                    logger.debug("No challenge detected, session ready")
                break

        except Exception as e:
            logger.error(f"Error creating HTTP client session: {e} {type(e)} url: {qurl}")
            # Increase the delay for next attempt
            await asyncio.sleep(2 * (attempt + 1))

    return client

async def xxxget_session(args: argparse.Namespace, config: Any) -> httpx.AsyncClient:
    """Create and initialize an HTTP client session with PyPI authentication.

    Args:
        args: Command-line arguments
        config: Configuration object

    Returns:
        Initialized HTTP client
    """
    # Create client with more realistic browser behavior
    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }
    )

    # Initialize the session by accessing the main page first
    try:
        initial_resp = await client.get("https://pypi.org/")
        await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Error accessing PyPI main page: {e} {type(e)}")

    # Now try to access the search page to trigger and solve the challenge
    query = getattr(args, 'query', 'test')
    if isinstance(query, list):
        query = " ".join(query)

    qurl = f"{config.api_url}?q={query}"

    try:
        response = await client.get(qurl)
        await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Error creating HTTP client session: {e} {type(e)} url: {qurl}")
        return client

    # Check if we need to solve a challenge
    if "Client Challenge" in response.text:
        # Extract the challenge script URL
        try:
            rematch = re.search(r'/([^/]+)/script\.js\?reload=true', response.text, re.UNICODE)
            if rematch:
                script_path = rematch.group(1)
                script_url = f"https://pypi.org/{script_path}/script.js?reload=true"
            else:
                logger.error("Could not find script.js URL in the response.")
                return client
        except Exception as e:
            logger.error(f"Error extracting script URL: {e} {type(e)}")
            return client

        # Get the challenge script
        try:
            script_resp = await client.get(script_url)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error fetching script.js: {e} {type(e)}")
            return client

        if script_resp.status_code != 200:
            logger.warning(f"Failed to fetch script.js, status code: {script_resp.status_code}")
            return client

        # Extract and solve the challenge
        try:
            challenge_pattern = r'init\(\[(.*?)\],\s*"(.+?)",\s*"(.+?)",?\s*(true|false)?\);'
            challenge_regex = re.compile(challenge_pattern, re.DOTALL)
            challenge_match = challenge_regex.search(script_resp.text)

            if not challenge_match:
                logger.error("Challenge pattern not found in script.js")
                return client

            challenge_json, token, script_path, *_ = challenge_match.groups()
            challenge_list = json.loads(f"[{challenge_json}]")
            challenge = challenge_list[0]
            challenge_type = challenge.get("ty")
            challenge_data = challenge.get("data", {})

            # Prepare the answer
            answer_data = None

            if challenge_type == "pow":
                base = challenge_data["base"]
                hash_val = challenge_data["hash"]
                hmac = challenge_data["hmac"]
                expires = challenge_data["expires"]

                # Solve the proof of work challenge
                answer = ""
                characters = string.ascii_letters + string.digits
                for c1 in characters:
                    for c2 in characters:
                        c = base + c1 + c2
                        if hashlib.sha256(c.encode()).hexdigest() == hash_val:
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
                answer_data = {
                    "ty": "pat",
                    "auth": ""
                }
            else:
                logger.error(f"Unknown challenge type: {challenge_type}")
                return client

            # Submit the challenge answer
            back_url = f"https://pypi.org{script_path}/fst-post-back"
            data = {
                "token": token,
                "data": [answer_data]
            }

            answer_resp = await client.post(back_url, json=data)
            await asyncio.sleep(2)  # Give more time for the session to be established

            if args.debug:
                logger.debug(f"Challenge answer sent to {back_url} Response status: {answer_resp.status_code}")

            # Make multiple attempts to verify the challenge is solved
            for i in range(3):
                test_resp = await client.get("https://pypi.org/search/?q=test")
                if "Client Challenge" not in test_resp.text:
                    if args.debug:
                        logger.debug(f"Challenge solved after {i+1} attempts")
                    break
                else:
                    if i < 2:  # Don't log warning on last attempt
                        logger.warning(f"Still getting challenge page after attempt {i+1} \ntest_resp.text: {test_resp.text}\n")
                    await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Error solving challenge: {e}")

    return client

async def old_get_session(args: argparse.Namespace, config: Any) -> httpx.AsyncClient:
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
                logger.debug(f"Second test search response status: {test_resp.status_code} Second test search HTML (first 1500 chars): {test_resp.text[:1500]}")

            # Log cookies to verify session state
            logger.debug(f"Session cookies after challenge: {client.cookies}")

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
