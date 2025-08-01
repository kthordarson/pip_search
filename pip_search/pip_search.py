import json
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

async def get_snippets(args: Namespace, config: Config, client: httpx.AsyncClient) -> List[Tag]:
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
        r = await client.get(f'{config.api_url}?q={args.query}', params=params)
        soup = BeautifulSoup(r.text, "html.parser")
        snippets += soup.select('a[class*="package-snippet"]')
        if args.debug:
            logger.debug(f'[s] p:{page} snippets={len(snippets)} query={query} from {config.api_url} params={params} r.status_code={r.status_code} HTML (first 500 chars): {r.text[:500]}')
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

async def oldsearch(args: Namespace, config: Config, opts: Union[Dict[str, Any], Namespace] = {}) -> List[Package]:
    """Search for packages on PyPI.

    Args:
        args: Command-line arguments
        config: Configuration object
        opts: Additional options

    Returns:
        List of Package objects
    """
    try:
        # client = await get_session(args, config)
        client = await search(args, config)
    except Exception as e:
        logger.error(f"[s] Error creating HTTP client: {e} {type(e)}")
        return []
    try:
        snippets = await get_snippets(args, config, client)
    except Exception as e:
        logger.error(f"[s] Error getting snippets: {e} {type(e)}")
        return []
    if args.debug:
        logger.debug(f"[s] Snippets found: {len(snippets)} for query: {args.query}")
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

async def search_pypi(query: str, page_count: int = 2, args: Namespace = None, config: Config = None) -> List[dict]:
    """Search PyPI using multiple strategies to bypass challenges.

    Args:
        query: Search query string
        page_count: Number of pages to search
        args: Command-line arguments
        config: Configuration object

    Returns:
        List of dictionaries with package information
    """
    if args is None:
        # Create minimal args object for logging if none provided
        class MinimalArgs:
            debug = False
            query = query
        args = MinimalArgs()

    if config is None:
        config = Config()

    results = []

    # Create a client with focused JSON API headers
    headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://pypi.org/",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",}
    # client = httpx.AsyncClient(follow_redirects=True, timeout=30.0,headers=headers)
    client = await get_session(args, config)
    try:
        # Approach 1: Try direct package lookup first
        if args.debug:
            logger.debug(f"Trying direct package lookup for '{query}'")

        json_url = f"https://pypi.org/pypi/{query}/json"
        response = await client.get(json_url)

        if response.status_code == 200:
            # Found exact package match
            data = response.json()
            info = data.get("info", {})
            name = info.get("name", query)
            version = info.get("version", "unknown")

            # Get release date
            release_date = "unknown"
            releases = data.get("releases", {})
            if version in releases and releases[version]:
                for release in releases[version]:
                    if "upload_time" in release:
                        release_date = release["upload_time"]
                        break

            results.append({
                "name": name,
                "version": version,
                "description": info.get("summary", ""),
                "released": release_date,
                "link": f"https://pypi.org/project/{name}/"
            })

            if args.debug:
                logger.debug(f"Found exact package match: {name}")

        # Approach 2: Use PyPI Simple API to find similar packages
        if not results or query != results[0]['name'].lower():
            if args.debug:
                logger.debug("Using PyPI Simple API to find matching packages")

            # The Simple API is much less protected than the search page
            simple_url = "https://pypi.org/simple/"
            response = await client.get(simple_url)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                matching_packages = []

                # Find packages that match or contain our query string
                for link in soup.find_all("a"):
                    pkg_name = link.text.strip()
                    if query.lower() in pkg_name.lower():
                        matching_packages.append(pkg_name)

                if args.debug:
                    logger.debug(f"Found {len(matching_packages)} potential matches")

                # Limit to a reasonable number to avoid excessive API calls
                max_matches = min(20, len(matching_packages))

                # Sort by relevance - exact matches first, then startswith, then contains
                sorted_matches = []
                exact_matches = [pkg for pkg in matching_packages if pkg.lower() == query.lower()]
                starts_with_matches = [pkg for pkg in matching_packages if pkg.lower().startswith(query.lower()) and pkg.lower() != query.lower()]
                contains_matches = [pkg for pkg in matching_packages if query.lower() in pkg.lower() and not pkg.lower().startswith(query.lower())]

                sorted_matches = exact_matches + starts_with_matches + contains_matches

                # Get details for the top matches using the JSON API
                for pkg_name in sorted_matches[:max_matches]:
                    try:
                        pkg_json_url = f"https://pypi.org/pypi/{pkg_name}/json"
                        pkg_response = await client.get(pkg_json_url)

                        if pkg_response.status_code == 200:
                            pkg_data = pkg_response.json()
                            pkg_info = pkg_data.get("info", {})

                            # Skip if this package is already in our results
                            if any(r["name"] == pkg_info.get("name") for r in results):
                                continue

                            version = pkg_info.get("version", "unknown")

                            # Get release date
                            released = "unknown"
                            pkg_releases = pkg_data.get("releases", {})
                            if version in pkg_releases and pkg_releases[version]:
                                for release in pkg_releases[version]:
                                    if "upload_time" in release:
                                        released = release["upload_time"]
                                        break

                            # Add to our results
                            results.append({
                                "name": pkg_info.get("name", pkg_name),
                                "version": version,
                                "description": pkg_info.get("summary", ""),
                                "released": released,
                                "link": f"https://pypi.org/project/{pkg_name}/"
                            })
                    except Exception as e:
                        if args.debug:
                            logger.debug(f"Error getting JSON details for {pkg_name}: {e}")

        # Approach 3: Use PyPI warehouse API (undocumented but works well)
        if not results or len(results) < 5:  # If we have few or no results
            try:
                # This is an internal API used by PyPI's search interface
                warehouse_api_url = f"https://pypi.org/search/api/?q={query}"
                warehouse_response = await client.get(warehouse_api_url)
                if warehouse_response.status_code == 200:
                    if 'Client Challenge' in warehouse_response.text:
                        logger.warning(f"Warehouse API {warehouse_api_url} returned challenge page, skipping. returing results: {results}")
                    else:
                        try:
                            data = warehouse_response.json()
                        except json.decoder.JSONDecodeError as e:
                            logger.error(f"Error decoding JSON from warehouse API: {e} {type(e)}")
                            data = {}
                        for pkg in data.get("results", []):
                            if args.debug:
                                logger.debug(f"Found package via warehouse API: pkg: {pkg}")
                            # Skip if this package is already in our results
                            if any(r["name"] == pkg.get("name") for r in results):
                                continue
                            results.append({
                                "name": pkg.get("name", ""),
                                "version": pkg.get("version", "unknown"),
                                "description": pkg.get("description", ""),
                                "released": pkg.get("upload_time", "unknown"),
                                "link": f"https://pypi.org/project/{pkg.get('name', '')}/"
                            })

                    if args.debug:
                        logger.debug(f"Warehouse API returned {len(data.get('results', []))} results")
            except Exception as e:
                if args.debug:
                    logger.error(f"Warehouse API search failed: {e} {type(e)} datalen: {len(data)} {type(data)} {data.keys()}")
    except Exception as e:
        logger.error(f"Error searching PyPI: {e} {type(e)}")

    finally:
        await client.aclose()

    return results

async def old_search_pypi(query: str, page_count: int = 2, args: Namespace = None, config: Config = None) -> List[dict]:
    """Search PyPI using httpx with enhanced challenge handling.

    Args:
        query: Search query string
        page_count: Number of pages to search
        args: Command-line arguments
        config: Configuration object

    Returns:
        List of dictionaries with package information
    """
    if args is None:
        # Create minimal args object for logging if none provided
        class MinimalArgs:
            debug = False
            query = query
        args = MinimalArgs()

    if config is None:
        config = Config()

    results = []
    max_retries = 3

    for attempt in range(max_retries):
        try:
            # Get a session with challenge handling
            client = await get_session(args, config)

            # Test if session works by making a simple search request first
            test_url = f"{config.api_url}?q=test"
            test_response = await client.get(test_url)

            if "Client Challenge" in test_response.text:
                if args.debug:
                    logger.debug(f"Attempt {attempt+1}/{max_retries}: Still hitting challenge page")

                # If we're not on the last attempt, try again with delay
                if attempt < max_retries - 1:
                    await client.aclose()
                    await asyncio.sleep(2 * (attempt + 1))  # Exponential backoff
                    continue
            else:
                # Session appears to be working, proceed with the search
                for page_num in range(1, page_count + 1):
                    params = {"q": query, "page": page_num}
                    response = await client.get(config.api_url, params=params, follow_redirects=True)

                    if args.debug:
                        logger.debug(f"Search page {page_num} status: {response.status_code}")

                    if "Client Challenge" in response.text:
                        logger.warning(f"Challenge page detected for page {page_num}")
                        continue

                    # Parse the response with BeautifulSoup
                    soup = BeautifulSoup(response.text, "html.parser")

                    # Try different selectors for package listings
                    packages = soup.select('a[class*="package-snippet"]')
                    if not packages:
                        packages = soup.select('[data-qa="package-snippet"]')
                    if not packages:
                        packages = soup.select('article[class*="package-snippet"]')

                    if args.debug:
                        logger.debug(f"Found {len(packages)} packages on page {page_num}")

                    # Process each package
                    for pkg in packages:
                        try:
                            # Try multiple selectors for each field
                            name_elem = pkg.select_one('span[class*="package-snippet__name"]') or pkg.select_one('[data-qa="package-name"]') or pkg.select_one('h3')
                            desc_elem = pkg.select_one('p[class*="package-snippet__description"]') or pkg.select_one('[data-qa="package-description"]') or pkg.select_one('p')
                            time_elem = pkg.select_one('span[class*="package-snippet__created"] time') or pkg.select_one('time') or pkg.select_one('[datetime]')

                            if name_elem and desc_elem:
                                name = name_elem.text.strip()
                                description = desc_elem.text.strip()

                                href = pkg.get("href", "")
                                released = None
                                if time_elem:
                                    released = time_elem.get("datetime") or time_elem.text.strip()

                                results.append({
                                    'name': name,
                                    'description': description,
                                    'released': released or 'unknown',
                                    'link': f"https://pypi.org{href}" if href else ""
                                })
                        except Exception as e:
                            if args.debug:
                                logger.debug(f"Error extracting package info: {e}")

                # If we got here without hitting challenges, break the retry loop
                break

        except Exception as e:
            logger.error(f"Error during search (attempt {attempt+1}/{max_retries}): {e}")
            await asyncio.sleep(1 * (attempt + 1))
        finally:
            # Always clean up the client
            if 'client' in locals():
                await client.aclose()

    return results

async def search_with_json_api(query: str, page_count: int = 2, args: Namespace = None, config: Config = None) -> List[dict]:
    """Search PyPI using JSON API endpoints which bypass the challenge system entirely.

    Args:
        query: Search query string
        page_count: Number of pages to search (not used for JSON API)
        args: Command-line arguments
        config: Configuration object

    Returns:
        List of dictionaries with package information
    """
    if args is None:
        # Create minimal args object for logging if none provided
        class MinimalArgs:
            debug = False
            query = query
        args = MinimalArgs()

    if config is None:
        config = Config()

    results = []
    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
            # "User-Agent": f"pip_search/{__version__} (https://github.com/kthordarson/pip_search)",
            "Accept": "application/json"
        }
    )

    try:
        # First try: Use the PyPI XML-RPC API for search
        # This bypasses the challenge system completely
        if args.debug:
            logger.debug(f"Searching for '{query}' using PyPI XML-RPC API")

        import xmlrpc.client
        with xmlrpc.client.ServerProxy('https://pypi.org/pypi') as pypi:
            matches = pypi.search({'name': query})

            if not matches and ' ' in query:
                # Try searching with summary if name search returned nothing
                matches = pypi.search({'summary': query})

            if args.debug:
                logger.debug(f"XML-RPC API returned {len(matches)} matches")

            # Process the first batch of results (limit to reasonable number)
            max_results = min(30, len(matches))
            for i, result in enumerate(matches[:max_results]):
                name = result.get('name', '')

                # Get more details using the JSON API
                try:
                    json_url = f"https://pypi.org/pypi/{name}/json"
                    json_resp = await client.get(json_url)

                    if json_resp.status_code == 200:
                        data = json_resp.json()
                        info = data.get('info', {})
                        version = info.get('version', 'unknown')

                        # Get release date from releases data
                        release_date = 'unknown'
                        releases = data.get('releases', {})
                        if version in releases and releases[version]:
                            for release in releases[version]:
                                if 'upload_time' in release:
                                    release_date = release['upload_time']
                                    break

                        results.append({
                            'name': name,
                            'version': version,
                            'description': info.get('summary', result.get('summary', '')),
                            'released': release_date,
                            'link': f"https://pypi.org/project/{name}/"
                        })
                    else:
                        # Fallback to basic info if JSON API fails
                        results.append({
                            'name': name,
                            'version': result.get('version', 'unknown'),
                            'description': result.get('summary', ''),
                            'released': 'unknown',
                            'link': f"https://pypi.org/project/{name}/"
                        })
                except Exception as e:
                    if args.debug:
                        logger.debug(f"Error getting details for {name}: {e}")

                    # Still add the basic info we have
                    results.append({
                        'name': name,
                        'version': result.get('version', 'unknown'),
                        'description': result.get('summary', ''),
                        'released': 'unknown',
                        'link': f"https://pypi.org/project/{name}/"
                    })

        # If no results from XML-RPC API, try direct package lookup
        if not results:
            # Try direct package lookup - might be an exact package name
            json_url = f"https://pypi.org/pypi/{query}/json"
            json_resp = await client.get(json_url)

            if json_resp.status_code == 200:
                data = json_resp.json()
                info = data.get('info', {})
                name = info.get('name', query)
                version = info.get('version', 'unknown')

                # Get release date
                release_date = 'unknown'
                releases = data.get('releases', {})
                if version in releases and releases[version]:
                    for release in releases[version]:
                        if 'upload_time' in release:
                            release_date = release['upload_time']
                            break

                results.append({
                    'name': name,
                    'version': version,
                    'description': info.get('summary', ''),
                    'released': release_date,
                    'link': f"https://pypi.org/project/{name}/"
                })

                if args.debug:
                    logger.debug(f"Found exact package match: {name}")

    except Exception as e:
        logger.error(f"Error during JSON API search: {e}")

    finally:
        await client.aclose()

    return results

async def old_search_with_json_api(query: str, page_count: int = 2, args: Namespace = None, config: Config = None) -> List[dict]:
    """Search PyPI using the JSON API endpoints which are less protected by challenges.

    Args:
        query: Search query string
        page_count: Number of pages to search (only used for XML API fallback)
        args: Command-line arguments
        config: Configuration object

    Returns:
        List of dictionaries with package information
    """
    if args is None:
        # Create minimal args object for logging if none provided
        class MinimalArgs:
            debug = False
            query = query
        args = MinimalArgs()

    if config is None:
        config = Config()

    results = []
    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": "pip_search/0.0.13 (+https://github.com/kthordarson/pip_search)",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
    )

    try:
        # First approach: Try PyPI's JSON API directly
        json_api_url = f"https://pypi.org/pypi/{query}/json"
        if args.debug:
            logger.debug(f"Trying direct package JSON API: {json_api_url}")

        response = await client.get(json_api_url)

        if response.status_code == 200:
            # Found exact package match
            data = response.json()
            info = data.get("info", {})

            if args.debug:
                logger.debug(f"Found exact package match: {info.get('name')}")

            version = info.get("version", "unknown")

            # Get release date from the latest release
            releases = data.get("releases", {})
            released = "unknown"
            if version in releases and releases[version]:
                upload_time = releases[version][0].get("upload_time")
                if upload_time:
                    released = upload_time

            results.append({
                "name": info.get("name", query),
                "version": version,
                "description": info.get("summary", ""),
                "released": released,
                "link": f"https://pypi.org/project/{info.get('name', query)}"
            })

        else:
            # Second approach: Use the PyPI Simple API with XML parsing
            simple_api_url = "https://pypi.org/simple/"
            if args.debug:
                logger.debug("No exact match, trying search with Simple API")

            # First get the list of all packages
            response = await client.get(simple_api_url)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")

                # Find all packages that contain our query string
                matching_packages = []
                for link in soup.find_all("a"):
                    pkg_name = link.text
                    if query.lower() in pkg_name.lower():
                        matching_packages.append(pkg_name)

                if args.debug:
                    logger.debug(f"Found {len(matching_packages)} potential matches")

                # Limit to reasonable number of results
                matching_packages = matching_packages[:20]

                # Get details for each package
                for pkg_name in matching_packages:
                    try:
                        pkg_json_url = f"https://pypi.org/pypi/{pkg_name}/json"
                        pkg_response = await client.get(pkg_json_url)

                        if pkg_response.status_code == 200:
                            pkg_data = pkg_response.json()
                            pkg_info = pkg_data.get("info", {})

                            # Get version
                            version = pkg_info.get("version", "unknown")

                            # Get release date
                            pkg_releases = pkg_data.get("releases", {})
                            released = "unknown"
                            if version in pkg_releases and pkg_releases[version]:
                                upload_time = pkg_releases[version][0].get("upload_time")
                                if upload_time:
                                    released = upload_time

                            results.append({
                                "name": pkg_info.get("name", pkg_name),
                                "version": version,
                                "description": pkg_info.get("summary", ""),
                                "released": released,
                                "link": f"https://pypi.org/project/{pkg_info.get('name', pkg_name)}"
                            })
                    except Exception as e:
                        if args.debug:
                            logger.debug(f"Error getting details for {pkg_name}: {e}")

            # Third approach: Try xmlrpc API if we still don't have results
            if not results:
                if args.debug:
                    logger.debug("No results from JSON/Simple APIs, trying XML-RPC API")

                # We'll use xmlrpc to search PyPI
                import xmlrpc.client

                # This needs to be synchronous unfortunately
                client = xmlrpc.client.ServerProxy('https://pypi.org/pypi')
                search_results = client.search({'name': query})

                for result in search_results[:20]:  # Limit results
                    results.append({
                        "name": result.get("name", ""),
                        "version": result.get("version", "unknown"),
                        "description": result.get("summary", ""),
                        "released": "unknown",  # XML-RPC doesn't provide release dates
                        "link": f"https://pypi.org/project/{result.get('name', '')}"
                    })

    except Exception as e:
        logger.error(f"Error during JSON API search: {e}")

    finally:
        await client.aclose()

    return results

async def search_with_web_api(query: str, page_count: int = 2, args: Namespace = None, config: Config = None) -> List[dict]:
    """Search PyPI using web API with enhanced browser emulation.

    Args:
        query: Search query string
        page_count: Number of pages to search
        args: Command-line arguments
        config: Configuration object

    Returns:
        List of dictionaries with package information
    """
    if args is None:
        # Create minimal args object for logging if none provided
        class MinimalArgs:
            debug = False
            query = query
        args = MinimalArgs()

    if config is None:
        config = Config()

    results = []

    # Create a client with very realistic browser headers
    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://pypi.org/",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
    )

    try:
        # First try: Direct JSON API lookup for exact package match
        try:
            json_url = f"https://pypi.org/pypi/{query}/json"
            response = await client.get(json_url)

            if response.status_code == 200:
                data = response.json()
                info = data.get("info", {})
                name = info.get("name", query)
                version = info.get("version", "unknown")

                # Get release date
                release_date = "unknown"
                releases = data.get("releases", {})
                if version in releases and releases[version]:
                    for release in releases[version]:
                        if "upload_time" in release:
                            release_date = release["upload_time"]
                            break

                results.append({
                    "name": name,
                    "version": version,
                    "description": info.get("summary", ""),
                    "released": release_date,
                    "link": f"https://pypi.org/project/{name}/"
                })

                if args.debug:
                    logger.debug(f"Found exact package match: {name}")

                # Return early if we found an exact match
                return results

        except Exception as e:
            if args.debug:
                logger.debug(f"Direct JSON lookup failed: {e}")

        # Second try: Use PyPI Simple API to find matching packages
        # This API endpoint has fewer protections and can be used to find packages
        if not results:
            try:
                # Initialize session by visiting the homepage first
                await client.get("https://pypi.org/")
                await asyncio.sleep(1)

                # First try a search on the search page (this might hit challenges)
                for page in range(1, page_count + 1):
                    search_url = f"{config.api_url}?q={query}&page={page}"
                    response = await client.get(search_url)

                    # Check if we hit a challenge
                    if "Client Challenge" not in response.text:
                        # Parse search results
                        soup = BeautifulSoup(response.text, "html.parser")

                        # Look for package snippets
                        packages = soup.select('a[class*="package-snippet"]')
                        if not packages:
                            packages = soup.select('[data-qa="package-snippet"]')
                        if not packages:
                            packages = soup.select('article[class*="package-snippet"]')

                        if args.debug:
                            logger.debug(f"Found {len(packages)} packages on page {page}")

                        # Process each package
                        for pkg in packages:
                            try:
                                # Extract package info from search result
                                name_elem = pkg.select_one('span[class*="package-snippet__name"]') or pkg.select_one('[data-qa="package-name"]') or pkg.select_one('h3')
                                desc_elem = pkg.select_one('p[class*="package-snippet__description"]') or pkg.select_one('[data-qa="package-description"]') or pkg.select_one('p')
                                time_elem = pkg.select_one('span[class*="package-snippet__created"] time') or pkg.select_one('time') or pkg.select_one('[datetime]')

                                if name_elem and desc_elem:
                                    name = name_elem.text.strip()
                                    description = desc_elem.text.strip()

                                    href = pkg.get("href", "")
                                    released = "unknown"
                                    if time_elem:
                                        released = time_elem.get("datetime") or time_elem.text.strip()

                                    # Add to results
                                    results.append({
                                        "name": name,
                                        "description": description,
                                        "released": released,
                                        "link": f"https://pypi.org{href}" if href else f"https://pypi.org/project/{name}/"
                                    })
                            except Exception as e:
                                if args.debug:
                                    logger.debug(f"Error extracting package info: {e}")
                    else:
                        if args.debug:
                            logger.debug(f"Hit challenge page on search page {page}")
            except Exception as e:
                if args.debug:
                    logger.debug(f"Search page approach failed: {e}")

        # Third try: Use PyPI Simple API to find all packages containing the query
        if not results:
            try:
                simple_url = "https://pypi.org/simple/"
                response = await client.get(simple_url)

                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, "html.parser")
                    matching_packages = []

                    # Find packages that match our query
                    for link in soup.find_all("a"):
                        pkg_name = link.text
                        if query.lower() in pkg_name.lower():
                            matching_packages.append(pkg_name)

                    if args.debug:
                        logger.debug(f"Simple API found {len(matching_packages)} potential matches")

                    # Limit results to a reasonable number
                    matching_packages = matching_packages[:20]

                    # Get details for each package via JSON API
                    for pkg_name in matching_packages:
                        try:
                            pkg_json_url = f"https://pypi.org/pypi/{pkg_name}/json"
                            pkg_response = await client.get(pkg_json_url)

                            if pkg_response.status_code == 200:
                                pkg_data = pkg_response.json()
                                pkg_info = pkg_data.get("info", {})

                                # Get version
                                version = pkg_info.get("version", "unknown")

                                # Get release date
                                released = "unknown"
                                releases = pkg_data.get("releases", {})
                                if version in releases and releases[version]:
                                    for release in releases[version]:
                                        if "upload_time" in release:
                                            released = release["upload_time"]
                                            break

                                results.append({
                                    "name": pkg_info.get("name", pkg_name),
                                    "version": version,
                                    "description": pkg_info.get("summary", ""),
                                    "released": released,
                                    "link": f"https://pypi.org/project/{pkg_name}/"
                                })
                        except Exception as e:
                            if args.debug:
                                logger.debug(f"Error getting details for {pkg_name}: {e}")
            except Exception as e:
                if args.debug:
                    logger.debug(f"Simple API approach failed: {e}")

    except Exception as e:
        logger.error(f"Error during web API search: {e}")

    finally:
        await client.aclose()

    return results

async def search(args: Namespace, config: Config, opts: Union[Dict[str, Any], Namespace] = {}) -> List[Package]:
    query = "".join(args.query)
    try:
        # Use the improved httpx-only search function
        search_results = await search_pypi(query, config.page_size, args, config)
        # search_results = await search_with_json_api(query, config.page_size, args, config)
        # search_results = await search_with_web_api(query, config.page_size, args, config)

        if args.debug:
            logger.debug(f"Found {len(search_results)} results for query: {args.query}")

        # Convert results to Package objects
        packages = []
        client = None
        try:
            # Get a session for version fetching if needed
            client = await get_session(args, config)

            for result in search_results:
                # Get version by scraping the individual package page if needed
                version = "unknown"
                if result['link']:
                    try:
                        version = await get_version_from_link(result['link'], client)
                    except Exception as e:
                        if args.debug:
                            logger.debug(f"Error fetching version for {result['name']}: {e}")

                pack = Package(
                    result['name'],
                    version,
                    result['released'],
                    result['description'],
                    result['link']
                )
                packages.append(pack)

        finally:
            # Always clean up the client
            if client:
                await client.aclose()

        return packages

    except Exception as e:
        logger.error(f"Error with search: {e}")
        return []


async def old2search(args: Namespace, config: Config, opts: Union[Dict[str, Any], Namespace] = {}) -> List[Package]:
    """Search for packages on PyPI."""
    query = "".join(args.query)
    try:
        search_results = await search(query, config.page_size, args, config)
        if args.debug:
            logger.debug(f"[s] found: {len(search_results)} results for query: {args.query}")
        packages = []
        for result in search_results:
            # Get version by scraping the individual package page if needed
            version = "unknown"
            if result['link']:
                # You could implement version fetching here if needed
                pass

            pack = Package(
                result['name'],
                version,
                result['released'],
                result['description'],
                result['link']
            )
            packages.append(pack)

        return packages

    except Exception as e:
        logger.error(f"[s] Error with search: {e} {type(e)}")
        return []

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

async def get_github_info(repolink: str, auth: Optional[str], client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
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
