"""Playwright fallback for pages whose body is rendered client-side.

Scope, deliberately narrow:

* It runs only when :func:`detect_needs_javascript` found positive evidence
  that the HTML we already have is an application shell.
* It renders the publisher's own article page. It is never pointed at a
  search engine results page -- driving a browser through Google results is
  exactly the pattern this project avoids.
* It waits on explicit locators and load states. There are no blind
  ``sleep`` calls: a fixed sleep is both slower than it needs to be on a
  fast page and wrong on a slow one.
* It launches a fresh, empty browser context every time. No personal Chrome
  profile, no stored cookies, no session reuse, no stealth plugin, no
  fingerprint patching. The browser identifies itself with the same
  User-Agent as the HTTP client.

If Playwright or its Chromium build is not installed, this degrades to a
clear "unavailable" result instead of an import error, so a checkout without
browsers still crawls everything else.
"""

from __future__ import annotations

from dataclasses import dataclass

from tw_cdc_influenza_news.config import AppConfig
from tw_cdc_influenza_news.errors import ErrorCode
from tw_cdc_influenza_news.logging_utils import get_logger

logger = get_logger("fetching.playwright")


@dataclass(slots=True)
class RenderOutcome:
    html: str | None = None
    final_url: str | None = None
    status_code: int | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    used: bool = False


def playwright_available() -> tuple[bool, str]:
    """Whether the Playwright package imports. Does not launch a browser."""
    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        return False, f"playwright package not importable: {exc}"
    return True, "playwright package importable"


async def chromium_installed() -> tuple[bool, str]:
    """Whether a Chromium build is actually present.

    ``doctor`` calls this: the package being importable and the browser being
    downloaded are two different things, and the second is the one that fails
    on a fresh checkout.
    """
    available, detail = playwright_available()
    if not available:
        return False, detail
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        return False, str(exc)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            version = browser.version
            await browser.close()
    except Exception as exc:
        return False, (
            f"chromium could not be launched: {exc}. "
            "Run: python -m playwright install chromium"
        )
    return True, f"chromium {version}"


class PlaywrightRenderer:
    """Lazily-started Chromium renderer with a hard per-run page budget."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.settings = config.extraction.playwright
        self._playwright = None
        self._browser = None
        self._rendered = 0

    @property
    def budget_remaining(self) -> int:
        return max(0, self.settings.max_pages_per_run - self._rendered)

    async def _ensure_browser(self) -> str | None:
        if self._browser is not None:
            return None
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            return f"playwright not installed: {exc}"
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.settings.headless
            )
        except Exception as exc:
            return (
                f"chromium failed to launch: {exc}. "
                "Run: python -m playwright install chromium"
            )
        return None

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def render(self, url: str) -> RenderOutcome:
        outcome = RenderOutcome()
        if not self.settings.enabled:
            outcome.error_message = "playwright fallback disabled in config"
            return outcome
        if self.budget_remaining == 0:
            outcome.error_message = "playwright page budget for this run is exhausted"
            return outcome

        problem = await self._ensure_browser()
        if problem:
            outcome.error_code = ErrorCode.PROVIDER_ERROR
            outcome.error_message = problem
            return outcome

        # A fresh context per page: nothing is carried between sites, and no
        # profile from this machine is ever loaded.
        context = await self._browser.new_context(
            user_agent=self.config.project.user_agent(),
            locale="zh-TW",
            java_script_enabled=True,
        )
        page = await context.new_page()
        timeout_ms = int(self.settings.navigation_timeout_seconds * 1000)

        try:
            response = await page.goto(
                url, wait_until=self.settings.wait_until, timeout=timeout_ms
            )
            outcome.status_code = response.status if response is not None else None

            # Wait for the first content locator that actually appears. This
            # is a state-driven wait; if none appears we take what rendered
            # by the load state rather than sleeping hopefully.
            for selector in self.settings.content_locators:
                locator = page.locator(selector).first
                try:
                    await locator.wait_for(state="attached", timeout=timeout_ms // 3)
                    break
                except Exception:
                    continue

            outcome.html = await page.content()
            outcome.final_url = page.url
            outcome.used = True
            self._rendered += 1
        except Exception as exc:
            outcome.error_code = ErrorCode.TIMEOUT
            outcome.error_message = f"playwright render failed: {exc}"
        finally:
            await page.close()
            await context.close()

        if outcome.status_code == 403:
            outcome.error_code = ErrorCode.HTTP_403
            outcome.error_message = "HTTP 403 during render; stopping this page"
            outcome.html = None
        elif outcome.status_code == 429:
            outcome.error_code = ErrorCode.HTTP_429
            outcome.error_message = "HTTP 429 during render; stopping this page"
            outcome.html = None

        return outcome
