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

def read_metafile(distpath: str) -> Tuple[Optional[str], Optional[str], str]:
    """Read metadata from a distribution path.

    Args:
        distpath: Path to the distribution directory

    Returns:
        Tuple of (package_name, version, distpath)
    """
    name = None
    version = None
    try:
        with open(distpath+'/METADATA') as f:
            meta = f.readlines()[:5]
        for line in meta:
            if 'Name:' in line:
                name = line.split(':')[1].strip()
            if 'Version:' in line:
                version = line.split(':')[1].strip()

    except Exception as e:
        print(f'error reading {distpath}: {e} {type(e)}')
    return name, version, distpath

def get_local_libs(libpath: str) -> List[Dict[str, str]]:
    """Get list of installed packages from a local library path.

    Args:
        libpath: Path to the local library directory

    Returns:
        List of dictionaries with package metadata
    """
    alldirs = sorted([k for k in glob.glob(libpath+'**',recursive=False, include_hidden=True) if os.path.isdir(k)])
    dists_found = [k for k in alldirs if os.path.exists(k+'/METADATA')]
    print(f'alldirs: {len(alldirs)} dists_found: {len(dists_found)} in {libpath}')
    name_list = []
    nodist_list = []
    for dist in dists_found:
        distname,version,distpath = read_metafile(dist)
        if distname:
            name_list.append({'name':distname,'version':version, 'distpath':distpath})
        else:
            print(f'no name found in {dist}')
    # make a list of folders with no METADATA file
    tmplist = sorted([k for k in alldirs if 'dist-info' not in k])
    nodistlist = [k for k in tmplist if k.split('/')[-1] in [x['name'] for x in name_list]]
    nodist_list = [k for k in alldirs if k.split('/')[-1] not in dists_found]
    return name_list

async def check_pypi_version(libname: str, client: httpx.AsyncClient, max_retries: int = 3) -> Tuple[Optional[str], Optional[str]]:
    """Check PyPI for package version information with retry logic.

    Args:
        libname: Name of the package to check
        client: HTTP client to use for requests
        max_retries: Maximum number of retry attempts (default: 3)

    Returns:
        Tuple of (package_name, version) or (None, None) on error
    """
    baseurl = f'https://pypi.org/project/{libname}'
    pkg_name = None
    pkg_version = None

    for attempt in range(max_retries):
        try:
            # Use timeout parameter to avoid hanging requests
            r = await client.get(baseurl, follow_redirects=True, timeout=10.0)
            soup = BeautifulSoup(r.text, "html.parser")
            pkgheader = soup.select_one('h1[class*="package-header__name"]').text.strip()
            pkg_name, pkg_version = pkgheader.split(' ')
            break  # Success, exit the retry loop
        except httpx.ConnectTimeout as e:
            backoff_time = 0.5 * (2 ** attempt)  # Exponential backoff: 0.5s, 1s, 2s
            logger.warning(f'ConnectTimeout checking {libname} (attempt {attempt+1}/{max_retries}): {e} baseurl={baseurl}')
            if attempt < max_retries - 1:  # If not the last attempt
                logger.info(f'Retrying in {backoff_time:.1f} seconds')
                await asyncio.sleep(backoff_time)
            else:
                logger.warning(f'Failed to connect to {baseurl} after {max_retries} attempts')
        except Exception as e:
            logger.error(f'Error checking {libname}: {e} {type(e)} baseurl={baseurl}')
            break  # Don't retry for non-timeout errors

    # Always return a tuple
    await asyncio.sleep(0.1)  # To avoid hitting the server too hard
    return pkg_name, pkg_version

async def check_local_libs(
    libpath: str,
    args: argparse.Namespace,
    config: Any
) -> Tuple[List[str], List[Dict[str, str]]]:
    """Check local libraries for updates against PyPI.

    Args:
        libpath: Path to the local library directory
        args: Command-line arguments
        config: Configuration object

    Returns:
        Tuple of (outdated_libs, error_list)
    """
    client = await get_session(args, config)
    local_libs = get_local_libs(libpath)
    outdated_libs = []
    error_list = []

    # Use gather to check all versions concurrently
    tasks = []
    for lib in local_libs:
        tasks.append(check_pypi_version(lib["name"], client))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, result in enumerate(results):
        lib = local_libs[i]
        if isinstance(result, Exception):
            logger.error(f'Error checking {lib["name"]}: {result}')
            error_list.append(lib)
            continue

        # Handle None results or other unexpected types
        if result is None:
            logger.warning(f'Received None result for {lib["name"]}')
            error_list.append(lib)
            continue

        # Make sure result is a tuple/list with at least 2 elements
        if not isinstance(result, (tuple, list)) or len(result) < 2:
            logger.warning(f'Unexpected result type for {lib["name"]}: {type(result)}')
            error_list.append(lib)
            continue

        pypi_name, pypi_version = result
        if pypi_name is None or pypi_version is None:
            # logger.warning(f'Missing name or version for {lib["name"]}')
            error_list.append(lib)
            continue

        print(f'pypi: {pypi_name} {pypi_version} local: {lib["name"]} {lib["version"]}')
        if pypi_version != lib["version"]:
            print(f'\tupgrade {lib["name"]} from {lib["version"]} to {pypi_version}')
            outdated_libs.append(lib["name"])
        else:
            pass  # print(f'{lib["name"]} is up to date')

    print(f'outdated libs: {len(outdated_libs)} error list: {len(error_list)}')
    await client.aclose()  # Added proper cleanup
    return outdated_libs, error_list

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
    r = await client.get(config.api_url, params=params, headers=headers)

    # Get script.js url
    pattern = re.compile(r"/(.*)/script.js")
    path = pattern.findall(r.text)[0]
    script_url = f"https://pypi.org/{path}/script.js"

    r = await client.get(script_url)

    # Find the PoW data from script.js
    pattern = re.compile(
        r'init\(\[\{"ty":"pow","data":\{"base":"(.+?)","hash":"(.+?)","hmac":"(.+?)","expires":"(.+?)"\}\}\], "(.+?)"'
    )
    base, hash, hmac, expires, token = pattern.findall(r.text)[0]

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
    back_url = f"https://pypi.org/{path}/fst-post-back"
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
