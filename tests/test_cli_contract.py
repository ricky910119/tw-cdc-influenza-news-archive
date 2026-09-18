"""The CLI must be constructible, and every documented command must exist.

This suite exists because of a failure that no other test could have caught:
a flag declaration that click accepted at one version and rejected at the
next made the whole command group fail to build, so *every* command raised
before doing anything. Importing the module was not enough to catch it --
the group is built when the app is called.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tw_cdc_influenza_news.cli import app

runner = CliRunner()

DOCUMENTED_COMMANDS = [
    "doctor",
    "crawl",
    "resume",
    "import-urls",
    "export",
    "report",
    "list-runs",
]


def test_the_command_group_builds():
    """Building the group is what failed before; --help forces it."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert result.exception is None


@pytest.mark.parametrize("command", DOCUMENTED_COMMANDS)
def test_every_documented_command_exists_and_builds(command: str):
    result = runner.invoke(app, [command, "--help"])

    assert result.exit_code == 0, result.output
    assert result.exception is None


def test_import_urls_offers_no_fetch():
    result = runner.invoke(app, ["import-urls", "--help"])

    assert "--no-fetch" in result.output


def test_crawl_offers_the_documented_options():
    result = runner.invoke(app, ["crawl", "--help"])

    for option in ("--start-date", "--end-date", "--provider"):
        assert option in result.output


def test_resume_offers_the_documented_options():
    result = runner.invoke(app, ["resume", "--help"])

    for option in ("--run-id", "--refetch-url", "--refetch-from", "--refetch-to"):
        assert option in result.output


def test_an_unknown_command_fails_without_a_traceback():
    result = runner.invoke(app, ["nonesuch"])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_the_installed_click_is_compatible_with_typer():
    """typer 0.12 and click 8.2+ cannot build a command group together."""
    import click
    import typer

    click_major_minor = tuple(int(p) for p in click.__version__.split(".")[:2])

    assert click_major_minor < (8, 2), (
        f"click {click.__version__} with typer {typer.__version__}: "
        "reinstall from requirements.lock"
    )
