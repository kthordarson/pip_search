#!/usr/bin/env python3
import argparse
import sys
import asyncio
from typing import List, Any
from urllib.parse import urlencode
from loguru import logger

from rich.console import Console
from rich.table import Table

try:
    from pip_search.pip_search import Config, search, Package
except (ModuleNotFoundError, ImportError) as e:
    # logger.warning(f"pip_search module not found: {e} {type(e)}")
    from pip_search import Config, search, Package
try:
    from . import __version__
except (ModuleNotFoundError, ImportError) as e:
    # logger.warning(f"pip_search __version__  module not found: {e} {type(e)}")
    __version__ = "0.0.0"
try:
    from .utils import check_version, check_local_libs, get_args
except (ModuleNotFoundError, ImportError) as e:
    # logger.warning(f"pip_search utils module not found: {e} {type(e)}")
    from utils import check_version, check_local_libs, get_args


def text_output(result: List[Package], query: str, args: argparse.Namespace) -> None:
    """Output search results as plain text.

    Args:
        result: List of Package objects
        query: Search query
        args: Command-line arguments
    """
    for package in result:
        if package.info_set:
            print(f'{package.name} l:{package.link} ver:{package.version} rel:{package.released_date_str(args.date_format)} gh:{package.github_link} s:{package.stars} f:{package.forks} w:{package.watchers}')
        else:
            print(f'{package.name} l:{package.link} ver:{package.version} rel:{package.released_date_str(args.date_format)}')
        print(f'\tdescription: {package.description}')

def table_output(result: List[Package], query: str, args: argparse.Namespace, config: Config) -> None:
    """Output search results as a formatted table.

    Args:
        result: List of Package objects
        query: Search query
        args: Command-line arguments
        config: Configuration object
    """
    table = Table(title=(f"[not italic]:snake:[/] [bold][magenta] {config.api_url}?{urlencode({'q': query})} [/] [not italic]:snake:[/]"))
    table.add_column("Package", style="cyan", no_wrap=True)
    table.add_column("Version", style="bold yellow")
    table.add_column("Released", style="bold green")
    table.add_column("Description", style="bold blue")
    if args.links:
        table.add_column("Link", style="bold blue")
    if args.extra:
        table.add_column("GH info", style="bold blue")

    for package in result:
        checked_version = check_version(package.name)
        if checked_version == package.version:
            package.version = f"[bold cyan]{package.version} ==[/]"
        elif checked_version is not False:
            package.version = (f"{package.version} > [bold purple]{checked_version}[/]")

        if args.links and args.extra:
            table.add_row(f"{package.name}",package.version,package.released_date_str(args.date_format),package.description, package.link, f's:{package.stars} f:{package.forks} w:{package.watchers}')
        elif args.links:
            table.add_row(f"{package.name}",package.version,package.released_date_str(args.date_format),package.description, package.link)
        elif args.extra:
            table.add_row(f"{package.name}",package.version,package.released_date_str(args.date_format),package.description, f's:{package.stars} f:{package.forks} w:{package.watchers}')
        else:
            table.add_row(f"{package.name}",package.version,package.released_date_str(args.date_format),package.description)
    console = Console()
    console.print(table)

async def async_main() -> int:
    """Main async entry point.

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    config = Config()
    ap, args = get_args()
    if args.locallibs:
        print(f'checking local libs in {args.locallibs}')
        outdated_libs, error_list = await check_local_libs(args.locallibs, args, config)
        print(f'outdated libs: {len(outdated_libs)} errors: {len(error_list)} \n')
        print(f'\noutdated libs: {outdated_libs}\n')
        print(f'\nerrors: {error_list}\n')
        return 0

    if not args.query:
        ap.print_help()
        return 1

    query = " ".join(args.query)

    try:
        res = await search(args, config, opts=args)
    except Exception as e:
        logger.error(f"Error during search: {e} {type(e)}")
        return 1

    if args.debug:
        logger.debug(f'results: {res}')

    if args.sort:
        if args.sort == 'released':
            res = sorted(res, key=lambda s: s.released)
        if args.sort == 'name':
            res = sorted(res, key=lambda s: s.name)
        if args.sort == 'version':
            res = sorted(res, key=lambda s: s.version)
        if args.sort == 'stars':
            res = sorted(res, key=lambda s: s.stars)
        if args.sort == 'watchers':
            res = sorted(res, key=lambda s: s.watchers)
        if args.sort == 'forks':
            res = sorted(res, key=lambda s: s.forks)

    table_output(res, query, args, config)
    return 0

def main() -> int:
    """Main entry point.

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    return asyncio.run(async_main())

if __name__ == "__main__":
    sys.exit(main())
