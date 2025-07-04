import asyncio
import re
import os
from loguru import logger
from argparse import Namespace
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from typing import Union, List, Dict, Optional, Any, TypeVar
from urllib.parse import urljoin
from bs4 import BeautifulSoup, Tag
import socket
import httpx
from urllib3.connection import HTTPConnection
from utils import get_session

HTTPConnection.default_socket_options = HTTPConnection.default_socket_options + [
    (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    (socket.SOL_TCP, socket.TCP_KEEPIDLE, 45),
    (socket.SOL_TCP, socket.TCP_KEEPINTVL, 10),
    (socket.SOL_TCP, socket.TCP_KEEPCNT, 6),
]

DEBUG = True
user_agents = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 12_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (Linux; Android 11; SM-G960U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.72 Mobile Safari/537.36",
]


class Config:
    """Configuration class"""

    api_url: str = "https://pypi.org/search/"
    page_size: int = 2
    sort_by: str = "name"
    date_format: str = "%d-%-m-%Y"
    link_defualt_format: str = "https://pypi.org/project/{package.name}"


@dataclass
class Package:
    """Package class"""

    name: str
    version: str
    released: str
    description: str
    link: InitVar[Optional[str]] = None

    config: Config = field(init=False, repr=False)
    released_date: datetime = field(init=False, repr=False)
    stars: int = field(default=0, init=False)
    forks: int = field(default=0, init=False)
    watchers: int = field(default=0, init=False)
    github_link: str = field(default="", init=False)
    info_set: bool = field(default=False, init=False)

    def __post_init__(self, link: Optional[str] = None) -> None:
        self.config = Config()
        self.link = link or self.config.link_defualt_format.format(package=self)
        self.released_date = datetime.strptime(self.released, "%Y-%m-%dT%H:%M:%S%z")

    def released_date_str(self, date_format: str) -> str:
        """Return the released date as a string formatted
        according to date_formate ou Config.date_format (default)

        Returns:
                str: Formatted date string
        """
        return self.released_date.strftime(date_format)

    def set_gh_info(self, info: Dict[str, Any]) -> None:
        """Set GitHub repository information.

        Args:
            info: Dictionary containing GitHub repository information
        """
        self.stars = info["stars"]
        self.forks = info["forks"]
        self.watchers = info["watchers"]
        self.github_link = info["github_link"]
        self.info_set = True

async def get_snippets(
    args: Namespace,
    config: Config,
    client: httpx.AsyncClient
) -> List[Tag]:
    """Get package snippets from PyPI search results.

    Args:
        args: Command-line arguments
        config: Configuration object
        client: HTTP client

    Returns:
        List of BeautifulSoup Tag objects representing package snippets
    """
    query = "".join(args.query)
    snippets = []
    for page in range(1, config.page_size + 1):
        params = {"q": query, "page": page}
        r = await client.get(config.api_url, params=params)
        soup = BeautifulSoup(r.text, "html.parser")
        snippets += soup.select('a[class*="package-snippet"]')
        logger.debug(f'[s] p:{page} snippets={len(snippets)} query={query} ')
    return snippets

async def get_version_from_link(link: str, client: httpx.AsyncClient) -> str:
    """Extract version from the package link if available.

    Args:
        link: URL to package details page
        client: HTTP client

    Returns:
        Version string or "noversion" if not found
    """
    version = '[notfound]'
    try:
        r = await client.get(link, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        version = "noversion"
        version = soup.select_one('p[class="release__version"]').text.strip()
    except Exception as e:
        logger.error(f"[gvl] Error getting version from link {link}: {e} {type(e)}")
    finally:
        return version

async def search(
    args: Namespace,
    config: Config,
    opts: Union[Dict[str, Any], Namespace] = {}
) -> List[Package]:
    """Search for packages on PyPI.

    Args:
        args: Command-line arguments
        config: Configuration object
        opts: Additional options

    Returns:
        List of Package objects
    """
    client = await get_session(args, config)
    snippets = await get_snippets(args, config, client)

    auth = None
    if opts.extra:
        GITHUBAPITOKEN = os.getenv("GITHUBAPITOKEN")
        GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")
        if GITHUBAPITOKEN and GITHUB_USERNAME:
            import base64
            auth_str = f"{GITHUB_USERNAME}:{GITHUBAPITOKEN}"
            auth = base64.b64encode(auth_str.encode()).decode()

    # Create a helper function to process each snippet
    async def process_snippet(snippet: Tag) -> Package:
        info = {}
        link = urljoin(config.api_url, snippet.get("href"))
        package = re.sub(r"\s+", " ", snippet.select_one('span[class*="package-snippet__name"]').text.strip())

        version = await get_version_from_link(link, client)
        released = re.sub(r"\s+", " ", snippet.select_one('span[class*="package-snippet__created"]').find("time")["datetime"])
        description = re.sub(r"\s+", " ", snippet.select_one('p[class*="package-snippet__description"]').text.strip())

        pack = Package(package, version, released, description, link)

        if opts.extra:
            info = await get_github_info(link, auth, client)
            if info:
                pack.set_gh_info(info)

        if args.debug:
            logger.debug(f'[s] pack: {pack} link: {link} info: {info}')

        return pack

    # Process all snippets concurrently
    tasks = [process_snippet(snippet) for snippet in snippets]
    results = await asyncio.gather(*tasks)

    await client.aclose()
    return results

async def get_repo_info(
    repo: str,
    auth: Optional[str],
    client: httpx.AsyncClient
) -> Dict[str, Any]:
    """Get repository information from GitHub API.

    Args:
        repo: GitHub repository URL
        auth: GitHub API authentication string
        client: HTTP client

    Returns:
        Dictionary containing repository information
    """
    info = {"stars": 0, "forks": 0, "watchers": 0, "set": False, "github_link": ""}
    try:
        reponame = repo.split("github.com/")[1].rstrip("/")
    except IndexError as e:
        logger.error(f"[r] err:{e} repo:{repo}")
        return info

    apiurl = f"https://api.github.com/repos/{reponame}"

    headers = {}
    if auth:
        headers["Authorization"] = f"Basic {auth}"

    r = await client.get(apiurl, headers=headers)

    if r.status_code == 401:
        if DEBUG:
            logger.error(f"[r] autherr:401 repo: {repo} apiurl: {apiurl}")
        return info
    if r.status_code == 404:
        if DEBUG:
            logger.warning(f"[r] {r.status_code} url: {repo} r: {reponame} apiurl: {apiurl} not found")
        return info
    if r.status_code == 403:
        if DEBUG:
            logger.warning(f"[r] {r.status_code} r: {reponame} apiurl: {apiurl} API rate limit exceeded")
        return info
    if r.status_code == 200:
        try:
            json_data = r.json()
            info["stars"] = json_data.get("stargazers_count", 0)
            info["forks"] = json_data.get("forks_count", 0)
            info["watchers"] = json_data.get("watchers_count", 0)
            info["github_link"] = repo
            info["set"] = True
            return info
        except (KeyError, TypeError, AttributeError) as err:
            logger.error(f"[gri] {err} r:{r.status_code} apiurl:{apiurl}")
            logger.error(f"[gri] info:{info}")
            return info

async def get_links(pkg_url: str, client: httpx.AsyncClient) -> Optional[Dict[str, str]]:
    """Get homepage and GitHub links from package URL.

    Args:
        pkg_url: Package URL
        client: HTTP client

    Returns:
        Dictionary containing homepage and GitHub links, or None if not found
    """
    r = await client.get(pkg_url)
    soup = BeautifulSoup(r.text, "html.parser")
    homepage = ""
    githublink = ""
    csspath = ".vertical-tabs__tabs > div:nth-child(3) > ul:nth-child(4) > li:nth-child(1) > a:nth-child(1)"
    try:
        homepage = soup.select_one(csspath, href=True).attrs["href"]
    except Exception as e:
        logger.error(f'[err] err:{e} homepage not found pkg_url:{pkg_url}')
        return None
    try:
        if "issues" in homepage:
            try:
                issues_homepage = soup.select_one(".vertical-tabs__tabs > div:nth-child(2) > ul:nth-child(2) > li:nth-child(2) > a:nth-child(1)", href=True,).attrs["href"]
            except Exception as e:
                logger.error(f'[err] {e} {type(e)} issues_homepage not found pkg_url:{pkg_url} homepage:{homepage}')
                return None
        if "github" in homepage:
            githublink = homepage
            githublink = githublink.replace("/tags", "")
            return {"github": githublink, "homepage": homepage}
        else:
            return None
    except AttributeError as e:
        logger.warning(f"[err] err:{e} homepage not found pkg_url:{pkg_url}")
        return None

async def get_github_info(
    repolink: str,
    auth: Optional[str],
    client: httpx.AsyncClient
) -> Optional[Dict[str, Any]]:
    """Get GitHub repository information for a package.

    Args:
        repolink: Package URL
        auth: GitHub API authentication string
        client: HTTP client

    Returns:
        Dictionary containing GitHub repository information, or None if not found
    """
    gh_link = await get_links(repolink, client)
    if gh_link:
        info = await get_repo_info(repo=gh_link["github"], auth=auth, client=client)
        return info
    else:
        return None
