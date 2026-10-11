"""Log in once and save the session (cookies only) for the crawler.

Two ways to log in:
  * interactive (default): a browser window opens, you log in by hand, the session is saved.
  * automatic: set KB_USERNAME and KB_PASSWORD in the environment; the form is filled for you.
Either way the password is never written to disk, only the resulting session cookies.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from playwright.sync_api import sync_playwright

from .browser import launch
from .config import paths_for
from .urls import is_login_url

USER_FIELD = (
    "input[type=email], input[name*=user i], input[id*=user i], input[name*=login i], "
    "input[name*=email i], input[type=text]"
)


def _auto_fill(page, username: str, password: str) -> None:
    pwd = page.locator("input[type=password]").first
    pwd.wait_for(state="visible", timeout=15000)
    form = page.locator("form", has=page.locator("input[type=password]")).first
    scope = form if form.count() else page
    scope.locator(USER_FIELD).first.fill(username)
    pwd.fill(password)
    submit = scope.locator("button[type=submit], input[type=submit], button:not([type])").first
    if submit.count():
        submit.click()
    else:
        pwd.press("Enter")


def _logged_in(page, cfg: dict[str, Any]) -> bool:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
        return not is_login_url(page.url, cfg) and page.locator("input[type=password]").count() == 0
    except Exception:  # page mid-navigation
        return False


def login(cfg: dict[str, Any], headless: bool = False, wait_seconds: int = 300) -> None:
    paths = paths_for(cfg)
    username = os.environ.get("KB_USERNAME")
    password = os.environ.get("KB_PASSWORD")
    auto = bool(username and password)
    if not auto and headless:
        raise SystemExit("Headless login needs KB_USERNAME and KB_PASSWORD set in the environment.")

    with sync_playwright() as p:
        browser = launch(p, headless=headless)
        context = browser.new_context(user_agent=cfg["user_agent"])
        page = context.new_page()
        page.goto(cfg["login_url"], wait_until="domcontentloaded")

        if auto:
            print("Logging in with KB_USERNAME / KB_PASSWORD ...")
            _auto_fill(page, username, password)
        else:
            print(f"A browser window is open at {cfg['login_url']}.")
            print(f"Log in there; I'll wait up to {wait_seconds // 60} minutes.")

        # Automatic login either works within seconds or not at all.
        deadline = time.time() + (30 if auto else wait_seconds)
        while not _logged_in(page, cfg):
            if time.time() > deadline:
                if auto:
                    text = page.locator("body").inner_text(timeout=5000)
                    hint = re.search(r"(invalid|incorrect|wrong|failed)[^\n]{0,80}", text, re.I)
                    raise SystemExit("Login did not succeed" + (f": {hint.group(0)}" if hint else "."))
                raise SystemExit("Timed out waiting for login.")
            time.sleep(1)

        # Confirm the session actually opens a start page.
        page.goto(cfg["start_urls"][0], wait_until="domcontentloaded")
        if is_login_url(page.url, cfg):
            raise SystemExit("Logged in, but the start page still redirects to login. Check start_urls in config.yaml.")

        context.storage_state(path=str(paths.auth_state))
        try:
            os.chmod(paths.auth_state, 0o600)
        except OSError:
            pass
        print(f"Session saved to {paths.auth_state} (landed on {page.url}).")
        browser.close()
