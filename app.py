from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup
from flask import Flask, jsonify, request


app = Flask(__name__)


BASE_URL = "https://carma.com.au"
FORM_PATH = "/forms/-/trade-in-enquiry"
HOME_PATH = "/sell-or-trade-in-my-car"


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except Exception:
        value = default
    return max(minimum, value)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


MAX_BROWSER_SESSIONS = _env_int("MAX_BROWSER_SESSIONS", 1, minimum=1)
BROWSER_SLOT_ACQUIRE_TIMEOUT_SECONDS = _env_int(
    "BROWSER_SLOT_ACQUIRE_TIMEOUT_SECONDS",
    5,
    minimum=0,
)
BROWSER_QUEUE_SIZE = _env_int(
    "BROWSER_QUEUE_SIZE",
    max(1, MAX_BROWSER_SESSIONS * 4),
    minimum=1,
)
BROWSER_RESULT_GRACE_SECONDS = _env_int(
    "BROWSER_RESULT_GRACE_SECONDS",
    10,
    minimum=1,
)
DEFAULT_USE_HEADLESS_BROWSER = _env_bool("DEFAULT_USE_HEADLESS_BROWSER", False)
FORCE_HEADED_BROWSER = _env_bool("FORCE_HEADED_BROWSER", True)
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = _env_int("API_PORT", 5000, minimum=1)
API_DEBUG = _env_bool("API_DEBUG", False)
API_TOKEN = str(os.getenv("API_TOKEN", "")).strip()
REQUIRE_API_TOKEN = _env_bool("REQUIRE_API_TOKEN", bool(API_TOKEN))
TOKEN_AUTH_PROTECTED_PATHS = {"/lookup", "/browser-status"}


def _extract_request_token() -> str:
    auth_header = str(request.headers.get("Authorization", "")).strip()
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header[7:].strip()
        if bearer:
            return bearer
    return str(request.headers.get("X-API-Token", "")).strip()


def _is_token_auth_required(path: str) -> bool:
    return path in TOKEN_AUTH_PROTECTED_PATHS


def _check_token_auth() -> tuple[bool, str]:
    if not REQUIRE_API_TOKEN:
        return True, ""

    if not API_TOKEN:
        return False, "Token auth is enabled but API_TOKEN is not configured on the server"

    presented = _extract_request_token()
    if not presented:
        return False, "Missing API token. Send Authorization: Bearer <token> or X-API-Token"

    if not secrets.compare_digest(presented, API_TOKEN):
        return False, "Invalid API token"

    return True, ""


@app.before_request
def _enforce_token_auth() -> Any:
    if request.method == "OPTIONS":
        return None

    if not _is_token_auth_required(request.path or ""):
        return None

    is_valid, message = _check_token_auth()
    if is_valid:
        return None

    status_code = 503 if "not configured" in message else 401
    return jsonify({"ok": False, "error": message}), status_code


class CarmaLookupError(Exception):
    def __init__(
        self,
        message: str,
        *,
        trace: list[dict[str, Any]] | None = None,
        response_snippet: str | None = None,
        status_code: int = 422,
    ):
        super().__init__(message)
        self.trace = trace or []
        self.response_snippet = response_snippet
        self.status_code = status_code


def _load_playwright_api() -> tuple[Any, Any, Any]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - environment dependent
        raise CarmaLookupError(
            "Browser fallback requires Playwright. Install with: pip install playwright && playwright install chromium"
        ) from exc

    return PlaywrightError, PlaywrightTimeoutError, sync_playwright


@dataclass
class BrowserJob:
    search_type: str
    headless: bool
    max_wait_seconds: int
    rego: str = ""
    state: str = ""
    vin: str = ""
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: CarmaLookupError | None = None


def _build_form_url(
    *,
    search_type: str,
    rego: str = "",
    state: str = "",
    vin: str = "",
) -> str:
    params: dict[str, str] = {}
    if search_type == "byVin":
        params["vin"] = vin
        params["searchType"] = search_type
    else:
        params["rego"] = rego
        params["state"] = state
        params["searchType"] = search_type

    q = urllib.parse.urlencode(params)
    return f"{BASE_URL}{FORM_PATH}?{q}"


def _safe_json_from_cookie(cookie_value: str) -> dict[str, Any]:
    try:
        decoded = urllib.parse.unquote(cookie_value)
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_details_from_html(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h3 = soup.find("h3")
    if h3:
        title = h3.get_text(strip=True)

    fields: dict[str, str] = {}
    for dt in soup.find_all("dt"):
        label = dt.get_text(strip=True)
        dd = dt.find_next_sibling("dd")
        if not label or not dd:
            continue
        value = dd.get_text(strip=True)
        if value:
            fields[label] = value

    normalized = {
        "title": title,
        "registration_plate": fields.get("Registration plate", ""),
        "vin": fields.get("VIN", ""),
        "state_of_issue": fields.get("State of issue", ""),
        "transmission": fields.get("Transmission", ""),
        "build_year": fields.get("Build year", ""),
        "fuel_type": fields.get("Fuel type", ""),
        "engine": fields.get("Engine", ""),
        "body_type": fields.get("Body type", ""),
    }

    if isinstance(normalized["build_year"], str) and normalized["build_year"].isdigit():
        normalized["build_year"] = int(normalized["build_year"])

    return normalized


def _looks_like_car_not_found_page(url: str, html: str) -> bool:
    if "/car-not-found" in (url or ""):
        return True
    probes = [
        "Please provide your car details",
        "Make",
        "Build year",
        "Model",
    ]
    return all(p in html for p in probes)


def _str_to_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


class _BrowserWorker(threading.Thread):
    def __init__(self, worker_id: int, job_queue: queue.Queue[BrowserJob | None]):
        super().__init__(name=f"browser-worker-{worker_id}", daemon=True)
        self.worker_id = worker_id
        self.job_queue = job_queue
        self.browser: Any = None
        self.launched_headless: bool | None = None

    def _close_browser(self) -> None:
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        finally:
            self.browser = None
            self.launched_headless = None

    def _browser_is_healthy(self, requested_headless: bool) -> bool:
        if self.browser is None or self.launched_headless != requested_headless:
            return False
        try:
            return bool(self.browser.is_connected())
        except Exception:
            return False

    def _ensure_browser(self, playwright: Any, requested_headless: bool) -> bool:
        if self._browser_is_healthy(requested_headless):
            return bool(self.launched_headless)

        self._close_browser()
        launch_errors: list[str] = []
        launch_attempts = [requested_headless]
        if not requested_headless:
            launch_attempts.append(True)

        for launch_headless in launch_attempts:
            try:
                self.browser = playwright.chromium.launch(headless=launch_headless)
                self.launched_headless = launch_headless
                return launch_headless
            except Exception as exc:  # pragma: no cover - environment dependent
                launch_errors.append(f"headless={launch_headless}: {exc}")

        raise CarmaLookupError(
            "Unable to launch Playwright browser for fallback lookup",
            response_snippet=" | ".join(launch_errors)[:1200],
        )

    def _should_reset_browser(self, requested_headless: bool, exc: CarmaLookupError | None = None) -> bool:
        if not self._browser_is_healthy(requested_headless):
            return True
        if exc is None:
            return False
        tokens = [
            "Browser fallback interaction failed:",
            "Browser page closed unexpectedly",
            "Unable to launch Playwright browser",
            "timed out",
        ]
        return any(token in str(exc) for token in tokens)

    def run(self) -> None:
        try:
            playwright_error_type, playwright_timeout_type, sync_playwright = _load_playwright_api()
        except CarmaLookupError as exc:
            while True:
                job = self.job_queue.get()
                if job is None:
                    self.job_queue.task_done()
                    break
                job.error = exc
                job.done.set()
                self.job_queue.task_done()
            return

        with sync_playwright() as playwright:
            while True:
                job = self.job_queue.get()
                if job is None:
                    self.job_queue.task_done()
                    break

                try:
                    launched_headless = self._ensure_browser(playwright, job.headless)
                    job.result = _lookup_carma_vehicle_browser_session(
                        browser=self.browser,
                        search_type=job.search_type,
                        headless=job.headless,
                        launched_headless=launched_headless,
                        max_wait_seconds=job.max_wait_seconds,
                        rego=job.rego,
                        state=job.state,
                        vin=job.vin,
                        play_error_type=playwright_error_type,
                        play_timeout_error_type=playwright_timeout_type,
                    )
                    if self._should_reset_browser(job.headless):
                        self._close_browser()
                except CarmaLookupError as exc:
                    job.error = exc
                    if self._should_reset_browser(job.headless, exc):
                        self._close_browser()
                except Exception as exc:
                    self._close_browser()
                    job.error = CarmaLookupError(
                        f"Browser worker failed unexpectedly: {exc}",
                        status_code=503,
                    )
                finally:
                    job.done.set()
                    self.job_queue.task_done()

        self._close_browser()


class _BrowserWorkerPool:
    def __init__(self, worker_count: int, queue_size: int):
        self.worker_count = worker_count
        self.job_queue: queue.Queue[BrowserJob | None] = queue.Queue(maxsize=queue_size)
        self._start_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._started = False
        self._active_jobs = 0
        self._workers: list[_BrowserWorker] = []

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._started:
                return
            for worker_id in range(1, self.worker_count + 1):
                worker = _BrowserWorker(worker_id, self.job_queue)
                self._workers.append(worker)
                worker.start()
            self._started = True

    def _mark_job_started(self) -> None:
        with self._state_lock:
            self._active_jobs += 1

    def _mark_job_finished(self) -> None:
        with self._state_lock:
            self._active_jobs = max(0, self._active_jobs - 1)

    def status(self) -> dict[str, Any]:
        self._ensure_started()
        with self._state_lock:
            active_jobs = self._active_jobs
        alive_workers = sum(1 for worker in self._workers if worker.is_alive())
        return {
            "started": self._started,
            "worker_count": self.worker_count,
            "alive_workers": alive_workers,
            "active_jobs": active_jobs,
            "queued_jobs": self.job_queue.qsize(),
            "queue_capacity": self.job_queue.maxsize,
        }

    def submit(
        self,
        *,
        search_type: str,
        headless: bool,
        max_wait_seconds: int,
        rego: str = "",
        state: str = "",
        vin: str = "",
    ) -> dict[str, Any]:
        self._ensure_started()

        job = BrowserJob(
            search_type=search_type,
            headless=headless,
            max_wait_seconds=max_wait_seconds,
            rego=rego,
            state=state,
            vin=vin,
        )

        enqueue_wait_seconds = max_wait_seconds + BROWSER_RESULT_GRACE_SECONDS
        try:
            self.job_queue.put(job, timeout=enqueue_wait_seconds)
        except queue.Full as exc:
            raise CarmaLookupError(
                (
                    "Browser worker queue remained full until request timeout "
                    f"(workers: {MAX_BROWSER_SESSIONS}, queue_size: {BROWSER_QUEUE_SIZE})"
                ),
                trace=[
                    {
                        "name": "browser_queue",
                        "method": "INFO",
                        "url": "",
                        "status": 503,
                        "ok": False,
                    }
                ],
                status_code=503,
            ) from exc

        self._mark_job_started()
        completed = job.done.wait(max_wait_seconds + BROWSER_RESULT_GRACE_SECONDS)
        self._mark_job_finished()
        if not completed:
            raise CarmaLookupError(
                "Browser worker did not finish before request timeout",
                trace=[
                    {
                        "name": "browser_worker_timeout",
                        "method": "INFO",
                        "url": "",
                        "status": 503,
                        "ok": False,
                    }
                ],
                status_code=503,
            )

        if job.error is not None:
            raise job.error
        if job.result is None:
            raise CarmaLookupError(
                "Browser worker returned no result",
                status_code=503,
            )
        return job.result


_BROWSER_WORKER_POOL = _BrowserWorkerPool(
    worker_count=MAX_BROWSER_SESSIONS,
    queue_size=BROWSER_QUEUE_SIZE,
)
_BROWSER_WORKER_POOL._ensure_started()


def _fill_from_cookie_if_missing(current: dict[str, Any], cookie_data: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)

    title_fallback = cookie_data.get("description", "")
    if not merged.get("title") and isinstance(title_fallback, str):
        merged["title"] = title_fallback

    mapping = {
        "registration_plate": "rego",
        "vin": "vin",
        "state_of_issue": "state",
        "transmission": "transmission",
        "build_year": "model_year",
        "fuel_type": "fuel_type",
        "engine": "engine_size",
        "body_type": "body_type",
    }

    for out_key, cookie_key in mapping.items():
        if merged.get(out_key):
            continue
        value = cookie_data.get(cookie_key)
        if value is None:
            continue
        if out_key == "engine" and isinstance(value, int):
            merged[out_key] = f"{value}cc"
        else:
            merged[out_key] = value

    return merged


def _validate_final_details(
    details: dict[str, Any],
    *,
    search_type: str,
    rego: str = "",
    state: str = "",
    vin: str = "",
) -> None:
    required = [
        "title",
        "registration_plate",
        "vin",
        "state_of_issue",
        "transmission",
        "build_year",
        "fuel_type",
        "engine",
        "body_type",
    ]
    missing = [k for k in required if not details.get(k)]
    if missing:
        raise CarmaLookupError(f"Missing required fields: {', '.join(missing)}")

    if search_type == "byVin":
        if str(details["vin"]).upper() != vin.upper():
            raise CarmaLookupError("Returned vin does not match requested vin")
    else:
        if str(details["registration_plate"]).upper() != rego.upper():
            raise CarmaLookupError("Returned registration_plate does not match requested rego")

        if str(details["state_of_issue"]).upper() != state.upper():
            raise CarmaLookupError("Returned state_of_issue does not match requested state")


def _lookup_carma_vehicle_browser_session(
    *,
    browser: Any,
    search_type: str,
    headless: bool,
    launched_headless: bool,
    max_wait_seconds: int = 120,
    rego: str = "",
    state: str = "",
    vin: str = "",
    play_error_type: Any,
    play_timeout_error_type: Any,
) -> dict[str, Any]:
    home_url = f"{BASE_URL}{HOME_PATH}"
    form_url = _build_form_url(search_type=search_type, rego=rego, state=state, vin=vin)
    trace: list[dict[str, Any]] = []
    started = time.time()
    last_trace_at = started

    def _append_trace(name: str, method: str, url: str, status: int, ok: bool) -> None:
        nonlocal last_trace_at
        now = time.time()
        trace.append(
            {
                "name": name,
                "method": method,
                "url": url,
                "status": status,
                "ok": ok,
                "elapsed_ms": int((now - started) * 1000),
                "step_ms": int((now - last_trace_at) * 1000),
            }
        )
        last_trace_at = now

    def _remaining_seconds() -> float:
        return max(0.0, max_wait_seconds - (time.time() - started))

    def _ensure_not_timed_out() -> None:
        if _remaining_seconds() <= 0:
            raise CarmaLookupError(
                f"Browser fallback timed out after {max_wait_seconds}s",
                trace=trace,
            )

    def _timeout_ms(preferred_ms: int) -> int:
        remaining_ms = int(_remaining_seconds() * 1000)
        return max(1000, min(preferred_ms, remaining_ms))

    def _short_timeout_ms(preferred_ms: int) -> int:
        remaining_ms = int(_remaining_seconds() * 1000)
        return max(1000, min(preferred_ms, remaining_ms, 15000))

    context = None
    page = None

    try:
        context = browser.new_context()
        page = context.new_page()
        _append_trace("browser_context_ready", "INFO", "", 0, True)

        _ensure_not_timed_out()
        page.goto(home_url, wait_until="domcontentloaded", timeout=_short_timeout_ms(15000))
        _append_trace("home_page_browser", "GET", home_url, 200, True)

        _ensure_not_timed_out()
        page.goto(form_url, wait_until="domcontentloaded", timeout=_short_timeout_ms(15000))
        _append_trace("landing_page_browser", "GET", form_url, 200, True)

        target_fragment = "/forms/-/trade-in-enquiry/car-found"
        if target_fragment not in page.url:
            if page.is_closed():
                raise CarmaLookupError(
                    "Browser page closed unexpectedly during fallback lookup",
                    trace=trace,
                )

            # Give the form a brief moment to hydrate, then submit immediately.
            page.wait_for_timeout(300)
            _append_trace("form_ready_browser", "WAIT", page.url, 0, True)

            if search_type == "byVin":
                for selector in [
                    "input[name='_1_vin']",
                    "input[name='vin']",
                    "input[placeholder*='vin' i]",
                ]:
                    loc = page.locator(selector)
                    if loc.count() > 0:
                        loc.first.fill(vin)
                        break
            else:
                for selector in [
                    "input[name='_1_rego']",
                    "input[name='rego']",
                    "input[placeholder*='rego' i]",
                    "input[placeholder*='registration' i]",
                ]:
                    loc = page.locator(selector)
                    if loc.count() > 0:
                        loc.first.fill(rego)
                        break

                state_filled = False
                for selector in [
                    "input[name='_1_state']",
                    "input[name='state']",
                ]:
                    loc = page.locator(selector)
                    if loc.count() > 0:
                        loc.first.fill(state)
                        state_filled = True
                        break
                if not state_filled:
                    sel = page.locator("select[name='_1_state'], select[name='state']")
                    if sel.count() > 0:
                        sel.first.select_option(value=state)

            for selector in [
                "button:has-text('Find')",
                "button:has-text('Search')",
                "button:has-text('Continue')",
                "button[type='submit']",
            ]:
                btn = page.locator(selector)
                if btn.count() > 0:
                    btn.first.click(timeout=_short_timeout_ms(1500))
                    _append_trace("submit_lookup_browser", "POST", page.url, 0, True)
                    break

        try:
            page.wait_for_url(
                "**/forms/-/trade-in-enquiry/car-found**",
                timeout=_short_timeout_ms(7000),
            )
            _append_trace("car_found_navigation_browser", "WAIT", page.url, 200, True)
        except play_timeout_error_type:
            _append_trace("car_found_navigation_browser", "WAIT", page.url, 0, False)

        def _read_cookie_details() -> dict[str, Any]:
            for c in context.cookies():
                if c.get("name") == "tradeInVehicleData":
                    return _safe_json_from_cookie(c.get("value", ""))
            return {}

        def _extract_observed_detail_labels(html_now: str) -> list[str]:
            soup = BeautifulSoup(html_now, "html.parser")
            labels: list[str] = []
            for dt in soup.find_all("dt"):
                label = dt.get_text(strip=True)
                if label:
                    labels.append(label)
            return labels

        observed_detail_labels: set[str] = set()
        observed_cookie_keys: set[str] = set()
        observed_snapshot_traced = False

        def _collect_details() -> tuple[str, dict[str, Any], dict[str, Any]]:
            html_now = page.content()
            details_now = _extract_details_from_html(html_now)
            cookie_now = _read_cookie_details()
            merged_now = _fill_from_cookie_if_missing(details_now, cookie_now)

            nonlocal observed_snapshot_traced
            current_labels = set(_extract_observed_detail_labels(html_now))
            current_cookie_keys = {str(key) for key in cookie_now.keys() if str(key)}

            if current_labels:
                observed_detail_labels.update(current_labels)
            if current_cookie_keys:
                observed_cookie_keys.update(current_cookie_keys)

            if not observed_snapshot_traced and (observed_detail_labels or observed_cookie_keys):
                _append_trace(
                    "browser_observations",
                    "INFO",
                    page.url,
                    0,
                    True,
                )
                trace[-1]["detail_labels"] = sorted(observed_detail_labels)[:8]
                trace[-1]["cookie_keys"] = sorted(observed_cookie_keys)[:8]
                observed_snapshot_traced = True

            return html_now, cookie_now, merged_now

        def _wait_for_hydration(max_ms: int = 12000) -> tuple[str, dict[str, Any], dict[str, Any]]:
            bounded_ms = min(max_ms, int(_remaining_seconds() * 1000))
            end = time.time() + (max(1000, bounded_ms) / 1000)
            last_html = ""
            last_cookie: dict[str, Any] = {}
            last_merged: dict[str, Any] = {}
            detail_selectors = [
                "dt",
                "dd",
                "h3",
                "text=Registration plate",
                "text=State of issue",
                "text=VIN",
            ]

            while time.time() < end:
                _ensure_not_timed_out()
                if page.is_closed():
                    raise CarmaLookupError(
                        "Browser page closed unexpectedly while waiting for vehicle details",
                        trace=trace,
                        response_snippet=last_html[:1200] if last_html else None,
                    )

                html_now, cookie_now, merged_now = _collect_details()
                last_html, last_cookie, last_merged = html_now, cookie_now, merged_now

                if merged_now.get("registration_plate") and merged_now.get("state_of_issue"):
                    return last_html, last_cookie, last_merged

                visible_detail_found = False
                for selector in detail_selectors:
                    try:
                        if page.locator(selector).count() > 0:
                            visible_detail_found = True
                            break
                    except Exception:
                        continue

                if visible_detail_found and (last_html or last_cookie):
                    page.wait_for_timeout(200)
                    continue

                page.wait_for_timeout(450)

            return last_html, last_cookie, last_merged

        html = ""
        final_details: dict[str, Any] = {}

        html, _, final_details = _wait_for_hydration(
            max_ms=min(5000, int(_remaining_seconds() * 1000))
        )
        _append_trace("car_found_browser", "GET", page.url, 200, True)

        if not (final_details.get("registration_plate") and final_details.get("state_of_issue")):
            _ensure_not_timed_out()
            page.goto(
                f"{BASE_URL}{FORM_PATH}/car-found",
                wait_until="domcontentloaded",
                timeout=_short_timeout_ms(8000),
            )
            _append_trace("car_found_retry_navigation_browser", "GET", page.url, 200, True)
            html, _, final_details = _wait_for_hydration(
                max_ms=min(7000, int(_remaining_seconds() * 1000))
            )
            _append_trace("car_found_retry_browser", "GET", page.url, 200, True)

        try:
            _validate_final_details(
                final_details,
                search_type=search_type,
                rego=rego,
                state=state,
                vin=vin,
            )
        except CarmaLookupError as exc:
            if _looks_like_car_not_found_page(page.url, html):
                raise CarmaLookupError(
                    "Carma redirected to car-not-found and did not return full vehicle details for this rego/state",
                    trace=trace,
                    response_snippet=html[:1200],
                ) from exc
            raise CarmaLookupError(
                str(exc),
                trace=trace,
                response_snippet=html[:1200],
            ) from exc

        return {
            "ok": True,
            "mode": "browser_fallback",
            "browser": {
                "requested_headless": headless,
                "launched_headless": launched_headless,
                "launched_mode": "headless" if launched_headless else "headed",
            },
            "requested": {
                "search_type": search_type,
                "rego": rego,
                "state": state,
                "vin": vin,
            },
            "details": final_details,
            "trace": trace,
        }
    except play_error_type as exc:
        raise CarmaLookupError(
            f"Browser fallback interaction failed: {exc}",
            trace=trace,
        ) from exc
    finally:
        try:
            if page is not None and not page.is_closed():
                page.close()
        except Exception:
            pass
        try:
            if context is not None:
                context.close()
        except Exception:
            pass


def lookup_carma_vehicle_browser(
    *,
    search_type: str,
    headless: bool = False,
    max_wait_seconds: int = 120,
    rego: str = "",
    state: str = "",
    vin: str = "",
) -> dict[str, Any]:
    return _BROWSER_WORKER_POOL.submit(
        search_type=search_type,
        headless=headless,
        max_wait_seconds=max_wait_seconds,
        rego=rego,
        state=state,
        vin=vin,
    )


@app.post("/lookup")
def lookup() -> Any:
    payload = request.get_json(silent=True) or {}

    search_type = str(payload.get("search_type", "")).strip() or "byRego"
    rego = str(payload.get("rego", "")).strip().upper()
    state = str(payload.get("state", "")).strip().upper()
    vin = str(payload.get("vin", "")).strip().upper()
    use_headless_input = payload.get("use_headless_browser")
    if use_headless_input is None:
        use_headless_browser = DEFAULT_USE_HEADLESS_BROWSER
    else:
        use_headless_browser = _str_to_bool(
            use_headless_input,
            default=DEFAULT_USE_HEADLESS_BROWSER,
        )

    # In production, headed mode is more reliable for this target site.
    if FORCE_HEADED_BROWSER:
        use_headless_browser = False
    max_browser_seconds = int(payload.get("max_browser_seconds", 120) or 120)
    if max_browser_seconds < 30:
        max_browser_seconds = 30
    if max_browser_seconds > 300:
        max_browser_seconds = 300

    if search_type not in {"byRego", "byVin"}:
        return jsonify({"ok": False, "error": "search_type must be byRego or byVin"}), 400

    if search_type == "byVin":
        if not vin:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "vin is required",
                    }
                ),
                400,
            )
    else:
        if not rego or not state:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "rego and state are required",
                    }
                ),
                400,
            )

    try:
        def _run_browser_lookup_with_retry() -> dict[str, Any]:
            if search_type == "byVin":
                mode_attempts = [False, True]
            else:
                mode_attempts = [use_headless_browser, not use_headless_browser]

            last_exc: CarmaLookupError | None = None
            lookup_started = time.time()
            attempt_meta: list[dict[str, Any]] = []

            for idx, attempt_headless in enumerate(mode_attempts):
                elapsed = time.time() - lookup_started
                remaining_total = max_browser_seconds - int(elapsed)
                if remaining_total <= 0:
                    break

                if search_type == "byVin":
                    per_attempt_seconds = min(18, remaining_total)
                else:
                    per_attempt_seconds = min(20, remaining_total)
                per_attempt_seconds = max(10, per_attempt_seconds)

                try:
                    result = lookup_carma_vehicle_browser(
                        search_type=search_type,
                        headless=attempt_headless,
                        max_wait_seconds=per_attempt_seconds,
                        rego=rego,
                        state=state,
                        vin=vin,
                    )
                    attempt_meta.append(
                        {
                            "attempt": idx + 1,
                            "mode": "headless" if attempt_headless else "headed",
                            "ok": True,
                        }
                    )
                    result["browser_attempts"] = attempt_meta
                    if idx > 0:
                        result["warning"] = (
                            f"Browser fallback succeeded on attempt {idx + 1} "
                            f"using {'headless' if attempt_headless else 'headed'} mode"
                        )
                        if last_exc is not None:
                            result["initial_browser_error"] = str(last_exc)
                            result["initial_browser_trace"] = last_exc.trace
                    return result
                except CarmaLookupError as browser_exc:
                    last_exc = browser_exc
                    msg = str(browser_exc)
                    attempt_meta.append(
                        {
                            "attempt": idx + 1,
                            "mode": "headless" if attempt_headless else "headed",
                            "ok": False,
                            "error": msg,
                        }
                    )
                    retriable_tokens = [
                        "Target page, context or browser has been closed",
                        "Missing required fields:",
                        "did not return full vehicle details",
                        "timed out",
                    ]
                    should_retry = idx < (len(mode_attempts) - 1) and any(t in msg for t in retriable_tokens)
                    if not should_retry:
                        browser_exc.trace = [
                            *browser_exc.trace,
                            {"name": "browser_attempts", "method": "INFO", "url": "", "status": 0, "ok": False},
                        ]
                        raise

            if last_exc is not None:
                last_exc.trace = [
                    *last_exc.trace,
                    {"name": "browser_attempts", "method": "INFO", "url": "", "status": 0, "ok": False},
                ]
                raise last_exc

            raise CarmaLookupError("Browser fallback failed in all attempted modes")

        result = _run_browser_lookup_with_retry()
        return jsonify(result), 200
    except CarmaLookupError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "trace": exc.trace,
                    "response_snippet": exc.response_snippet,
                }
            ),
            exc.status_code,
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Unexpected error: {exc}"}), 500


@app.get("/lookup")
def lookup_help() -> Any:
    return (
        jsonify(
            {
                "ok": False,
                "error": "Use POST /lookup with JSON body for byRego or byVin mode",
                "example": {
                    "search_type": "byRego",
                    "rego": " ",
                    "state": "ACT",
                    "use_headless_browser": True,
                    "max_browser_seconds": 120,
                },
                "example_vin": {
                    "search_type": "byVin",
                    "vin": "MRHGM26409P050012",
                    "use_headless_browser": False,
                    "max_browser_seconds": 120,
                },
                "auth": {
                    "required": REQUIRE_API_TOKEN,
                    "headers": ["Authorization: Bearer <token>", "X-API-Token: <token>"],
                },
            }
        ),
        405,
    )


@app.get("/")
def index() -> Any:
    return jsonify(
        { 
            "ok": True,
            "service": "carma-lookup",
            "routes": {
                "health": "GET /health",
                "browser_status": "GET /browser-status",
                "lookup": "POST /lookup",
            },
        }
    )


@app.get("/browser-status")
def browser_status() -> Any:
    return jsonify({"ok": True, "browser_pool": _BROWSER_WORKER_POOL.status()})


@app.get("/health")
def health() -> Any:
    return jsonify({"ok": True, "browser_pool": _BROWSER_WORKER_POOL.status()})


if __name__ == "__main__":
    app.run(host=API_HOST, port=API_PORT, debug=API_DEBUG, threaded=True)
