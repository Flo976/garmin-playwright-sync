"""Navigate Garmin Connect pages and intercept API responses."""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable

try:
    from patchright.sync_api import BrowserContext, Page, Response
except ImportError:
    from playwright.sync_api import BrowserContext, Page, Response

logger = logging.getLogger(__name__)

# All available pages that can be scraped
ALL_PAGES = {"daily", "sleep", "training-status", "body-composition", "activities", "personal-records"}
DEFAULT_PAGES = {"daily", "sleep", "training-status", "body-composition", "activities", "personal-records"}

_CAPTURE_PATTERNS: list[tuple[str, str]] = [
    # Daily summary page
    ("usersummary-service/usersummary/daily", "usersummary/daily"),
    ("wellness-service/wellness/dailyStress", "dailyStress"),
    ("bodybattery-service/bodybattery", "bodybattery"),
    ("wellness-service/wellness/daily/respiration", "respiration"),
    ("wellness-service/wellness/daily/spo2", "spo2"),
    ("wellness-service/wellness/daily/im", "intensityMinutes"),
    ("wellness-service/wellness/floorsChartData/daily", "floors"),
    ("wellness-service/wellness/bodyBattery/events", "bodyBatteryEvents"),
    # Sleep page
    ("wellness-service/wellness/dailySleepData", "dailySleepData"),
    ("hrv-service/hrv", "hrv"),
    # Fitness stats pages
    ("metrics-service/metrics/maxmet/daily", "maxmet"),
    ("fitnessStats-service/trainingReadiness", "trainingReadiness"),
    ("metrics-service/metrics/trainingstatus/aggregated", "trainingStatus"),
    ("metrics-service/metrics/endurancescore", "enduranceScore"),
    ("metrics-service/metrics/hillscore", "hillScore"),
    ("metrics-service/metrics/racepredictions", "racePredictions"),
    ("fitnessage-service/fitnessage", "fitnessAge"),
    # Body composition page
    ("weight-service/weight/dateRange", "bodyComposition"),
    ("weight-service/weight/range", "bodyComposition"),
    # Activities page
    ("activitylist-service/activities", "activities"),
    # Personal records page
    ("personalrecord-service/personalrecord/prs", "personalRecords"),
]


_CF_MARKERS = ("just a moment", "checking your browser")
_CF_RESOLVE_POLL_S = 30
_CF_CONFIRM_WAIT_S = 2


def _handle_cloudflare_challenge(page: Page) -> bool:
    """Detect and handle a Cloudflare challenge page encountered during navigation.

    When Cloudflare intercepts a request it shows either a JS auto-solve page
    ("Just a moment") or an interactive Turnstile checkbox.  This function:

    1. Returns immediately if no CF challenge is present.
    2. Waits a short random jitter (1–5 s) to look more human-like.
    3. Tries to click the Turnstile checkbox inside the CF iframe if visible.
    4. Polls up to 30 s for the challenge to disappear.

    Returns True if a challenge was detected (resolved or timed out), False otherwise.
    """
    content = page.content().lower()
    if not any(m in content for m in _CF_MARKERS):
        return False

    jitter = random.uniform(1.0, 5.0)  # nosec B311 — jitter delay, not cryptographic
    logger.info("Cloudflare challenge detected — waiting %.1fs before attempting solve", jitter)
    time.sleep(jitter)

    # Attempt to click the interactive Turnstile checkbox (inside CF iframe)
    try:
        cf_frame = page.frame_locator("iframe[src*='challenges.cloudflare.com']")
        checkbox = cf_frame.locator("input[type='checkbox'], [role='checkbox'], .ctp-checkbox-label")
        if checkbox.count() > 0:
            checkbox.first.click(timeout=5_000)
            logger.info("Clicked Cloudflare challenge checkbox")
    except Exception as e:
        logger.debug("Could not click Cloudflare checkbox (may auto-solve): %s", e)

    # Poll until the challenge page is gone.  After the markers first disappear,
    # wait briefly and re-check: a Cloudflare reload can make the page appear
    # challenge-free for an instant before the challenge reloads, causing a
    # false-positive resolution.
    for elapsed in range(_CF_RESOLVE_POLL_S):
        time.sleep(1)
        content = page.content().lower()
        if not any(m in content for m in _CF_MARKERS):
            time.sleep(_CF_CONFIRM_WAIT_S)
            content = page.content().lower()
            if not any(m in content for m in _CF_MARKERS):
                logger.info("Cloudflare challenge resolved after %ds", elapsed + 1)
                return True
            logger.debug("Cloudflare challenge reappeared after page reload — continuing to poll")

    logger.warning("Cloudflare challenge did not resolve within %ds", _CF_RESOLVE_POLL_S)
    return True


@dataclass
class SyncResult:
    """Holds captured API responses and tracks which pages loaded successfully."""

    responses: dict[str, dict | list] = field(default_factory=dict)
    pages_loaded: set[str] = field(default_factory=set)
    pages_failed: set[str] = field(default_factory=set)

    @property
    def is_complete(self) -> bool:
        """True if all attempted pages loaded without error."""
        return len(self.pages_failed) == 0 and len(self.pages_loaded) > 0


def _make_response_handler(captured: dict[str, dict | list]) -> Callable:
    """Create a response handler that captures matched API responses."""

    def handler(response: Response) -> None:
        if response.status != 200:
            return
        url = response.url
        for pattern, key in _CAPTURE_PATTERNS:
            if pattern in url:
                try:
                    data = response.json()
                    captured[key] = data
                    logger.debug("Captured %s (%d bytes)", key, len(json.dumps(data)))
                except Exception:
                    logger.debug("Non-JSON response for %s, skipping", key)
                break

        if "graphql-gateway/graphql" in url:
            try:
                data = response.json()
                gql_data = data.get("data", {})
                for gql_key in gql_data:
                    captured[f"graphql/{gql_key}"] = gql_data[gql_key]
                    logger.debug("Captured GraphQL: %s", gql_key)
            except Exception:
                logger.debug("Non-JSON response for graphql-gateway on %s, skipping", url)

    return handler


def _is_page_crashed(page: Page) -> bool:
    """Check if the page has crashed and is no longer usable."""
    try:
        page.evaluate("1")
        return False
    except Exception:
        return True


def _recover_page(page: Page, context: BrowserContext) -> Page:
    """Close a crashed page and create a fresh one."""
    logger.info("Recovering from page crash — creating new page")
    try:
        page.close()
    except Exception:
        logger.debug("Could not close crashed page (already dead)")
    new_page = context.new_page()
    time.sleep(1)
    return new_page


def _navigate(
    page: Page,
    url: str,
    result: SyncResult,
    label: str,
    context: BrowserContext | None = None,
    response_handler: Callable | None = None,
    idle_timeout_ms: int = 5_000,
) -> Page:
    """Navigate to a page and track success/failure.

    Returns the page (may be a new page if recovery was needed).
    When a crash is detected and recovery creates a new page, response_handler
    is automatically reattached so subsequent captures are not lost.
    """
    if _is_page_crashed(page):
        if context is None:
            logger.error("Page crashed and no context for recovery — skipping %s", label)
            result.pages_failed.add(label)
            return page
        page = _recover_page(page, context)
        if response_handler is not None:
            page.on("response", response_handler)

    try:
        logger.info("Loading %s", label)
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        logger.warning("Page load failed for %s: %s", label, e)
        result.pages_failed.add(label)
        if context and _is_page_crashed(page):
            page = _recover_page(page, context)
            if response_handler is not None:
                page.on("response", response_handler)
        return page

    _handle_cloudflare_challenge(page)

    # Wait for API responses — networkidle timeout is non-fatal because
    # Garmin's SPA keeps background requests running indefinitely.
    # The response handler captures data regardless.
    try:
        page.wait_for_load_state("networkidle", timeout=idle_timeout_ms)
    except Exception:
        logger.debug("networkidle timeout for %s (non-fatal, data captured via handler)", label)
    result.pages_loaded.add(label)

    return page


def sync_day(
    page: Page,
    date_str: str,
    include_activities: bool = True,
    pages: set[str] | None = None,
    context: BrowserContext | None = None,
    idle_timeout_ms: int = 5_000,
) -> tuple[SyncResult, Page]:
    """Navigate daily pages and return all captured API responses.

    Args:
        page: The browser page to use.
        date_str: Date to sync (YYYY-MM-DD).
        include_activities: Whether to load the activities page (legacy flag).
        pages: Set of pages to load (e.g. {"daily", "sleep", "activities"}).
            When provided, this overrides include_activities.
            Defaults to ALL_PAGES if None.
        context: Browser context, used to recover from page crashes.
        idle_timeout_ms: How long to wait for networkidle after domcontentloaded.
            Garmin's SPA never reaches true idle, so this is a best-effort wait.
            Tune upward (e.g. NETWORK_IDLE_TIMEOUT_MS=10000) on slow connections.

    Returns:
        A tuple of (SyncResult, page) — page may differ from input if recovery occurred.
    """
    if pages is None:
        pages = DEFAULT_PAGES.copy()
        if not include_activities:
            pages.discard("activities")

    result = SyncResult()
    handler = _make_response_handler(result.responses)
    page.on("response", handler)

    try:
        if "daily" in pages:
            page = _navigate(
                page,
                f"https://connect.garmin.com/app/daily-summary/{date_str}",
                result,
                f"daily summary ({date_str})",
                context,
                handler,
                idle_timeout_ms,
            )

        if "sleep" in pages:
            page = _navigate(
                page,
                f"https://connect.garmin.com/app/sleep/{date_str}",
                result,
                f"sleep ({date_str})",
                context,
                handler,
                idle_timeout_ms,
            )

        if "training-status" in pages:
            page = _navigate(
                page,
                f"https://connect.garmin.com/app/health-stats/training-status/{date_str}",
                result,
                f"training status ({date_str})",
                context,
                handler,
                idle_timeout_ms,
            )

        if "body-composition" in pages:
            page = _navigate(
                page,
                "https://connect.garmin.com/app/body-composition",
                result,
                "body composition",
                context,
                handler,
                idle_timeout_ms,
            )

        if "activities" in pages:
            page = _navigate(
                page,
                "https://connect.garmin.com/app/activities",
                result,
                "activities",
                context,
                handler,
                idle_timeout_ms,
            )

        if "personal-records" in pages:
            page = _navigate(
                page,
                "https://connect.garmin.com/app/personal-records",
                result,
                "personal records",
                context,
                handler,
                idle_timeout_ms,
            )

    finally:
        try:
            page.remove_listener("response", handler)
        except Exception:
            logger.debug("Could not remove response handler (page may have crashed)")

    logger.info(
        "Captured %d API responses (pages: %d ok, %d failed)",
        len(result.responses),
        len(result.pages_loaded),
        len(result.pages_failed),
    )
    return result, page
