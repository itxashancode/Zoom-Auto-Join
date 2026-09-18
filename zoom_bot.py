#!/usr/bin/env python3
"""
Silent Zoom web-client attendee.

Joins meetings from the Zoom *browser* client (no desktop app), with camera off
and mic muted, never speaks, never reacts, leaves when the window ends, and
rejoins automatically if it gets dropped or removed.

Usage:
    python zoom_bot.py                 # run scheduler forever (reads config.json)
    python zoom_bot.py --once "Math"   # join meeting named "Math" right now
    python zoom_bot.py --test "Math"   # join for 2 minutes, dump screenshots, exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import re
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, Page, BrowserContext, Error as PWError

# --------------------------------------------------------------------------- #
# Config / logging
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("ZOOM_BOT_CONFIG", BASE_DIR / "config.json"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "zoom_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("zoom-bot")

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

CHROME_ARGS = [
    # Give Chromium a fake camera (black) and fake mic (silence) so there is no
    # real hardware to leak even if something toggles on.
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    "--mute-audio",
    "--autoplay-policy=no-user-gesture-required",
    # Stability inside containers / tiny VMs
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-setuid-sandbox",
    # Look a bit less like an automated browser
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--window-size=1280,800",
]

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error("Config file not found: %s", CONFIG_PATH)
        sys.exit(1)
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("timezone", "Asia/Karachi")
    cfg.setdefault("display_name", "Attendee")
    cfg.setdefault("headless", False)
    cfg.setdefault("join_computer_audio", True)
    cfg.setdefault("check_interval_seconds", 20)
    cfg.setdefault("rejoin_backoff_seconds", 30)
    cfg.setdefault("screenshot_dir", "screenshots")
    return cfg


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #

def to_web_client_url(url: str) -> str:
    """
    Turn any Zoom invite link into a direct *browser* client link so the page
    never tries to hand off to the desktop app.

    https://us05web.zoom.us/j/12345678901?pwd=XYZ
        -> https://app.zoom.us/wc/12345678901/join?pwd=XYZ&fromPWA=1
    """
    url = url.strip()
    if "/wc/" in url:
        return url if "fromPWA" in url else url + ("&" if "?" in url else "?") + "fromPWA=1"

    m = re.search(r"/(?:j|s)/(\d+)", url)
    if not m:
        log.warning("Could not parse a meeting ID out of %s - using URL as-is", url)
        return url
    meeting_id = m.group(1)

    pwd = ""
    p = re.search(r"[?&]pwd=([^&]+)", url)
    if p:
        pwd = p.group(1)

    new = f"https://app.zoom.us/wc/{meeting_id}/join?fromPWA=1&browser=chrome"
    if pwd:
        new += f"&pwd={pwd}"
    return new


# --------------------------------------------------------------------------- #
# Small DOM helpers (Zoom changes its markup often -> always try several)
# --------------------------------------------------------------------------- #

async def click_first(page: Page, selectors: list[str], timeout: int = 4000) -> bool:
    """Click the first selector that appears. Returns True if something was clicked."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click(timeout=timeout)
            log.debug("clicked %s", sel)
            return True
        except Exception:
            continue
    return False


async def fill_first(page: Page, selectors: list[str], value: str, timeout: int = 4000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.fill(value, timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def shot(page: Page, cfg: dict, tag: str) -> None:
    try:
        d = BASE_DIR / cfg["screenshot_dir"]
        d.mkdir(exist_ok=True, parents=True)
        path = d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{tag}.png"
        await page.screenshot(path=str(path), full_page=False)
        log.info("screenshot -> %s", path.name)
    except Exception as e:
        log.debug("screenshot failed: %s", e)


# --------------------------------------------------------------------------- #
# Mute / camera enforcement
# --------------------------------------------------------------------------- #

async def force_av_off(page: Page) -> None:
    """
    Make sure mic is muted and camera is off.

    Zoom labels the buttons by the ACTION they perform:
      aria-label "Mute"        -> currently UNMUTED  -> click it
      aria-label "Unmute"      -> already muted      -> leave alone
      aria-label "Stop Video"  -> camera ON          -> click it
      aria-label "Start Video" -> camera OFF         -> leave alone
    """
    js = """
    () => {
      const out = [];
      const nodes = document.querySelectorAll('button, [role="button"]');
      for (const b of nodes) {
        const label = (
          (b.getAttribute('aria-label') || '') + ' ' +
          (b.getAttribute('title') || '') + ' ' +
          (b.innerText || '')
        ).toLowerCase().trim();
        if (!label) continue;
        const isUnmute = label.includes('unmute');
        const isStartVideo = label.includes('start video');
        if (!isUnmute && /\\bmute\\b/.test(label)) { b.click(); out.push('muted-mic'); }
        if (!isStartVideo && label.includes('stop video')) { b.click(); out.push('stopped-cam'); }
      }
      return out;
    }
    """
    try:
        actions = await page.evaluate(js)
        for a in actions:
            log.info("enforced: %s", a)
    except Exception as e:
        log.debug("force_av_off failed: %s", e)


# --------------------------------------------------------------------------- #
# Join flow
# --------------------------------------------------------------------------- #

async def join_meeting(pw, cfg: dict, meeting: dict) -> tuple[BrowserContext, Page]:
    url = to_web_client_url(meeting["url"])
    name = meeting.get("display_name") or cfg["display_name"]
    log.info("Joining '%s' as '%s'", meeting["name"], name)
    log.info("URL: %s", url)

    browser = await pw.chromium.launch(headless=cfg["headless"], args=CHROME_ARGS)
    context = await browser.new_context(
        permissions=["camera", "microphone"],
        viewport={"width": 1280, "height": 800},
        user_agent=UA,
        locale="en-US",
    )
    # hide the obvious automation flag
    await context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )
    page = await context.new_page()
    page.set_default_timeout(20000)

    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    log.info("landed on: %s", page.url)
    await shot(page, cfg, "landed")

    if page.url.rstrip("/") in ("https://zoom.us", "https://app.zoom.us", "https://www.zoom.us"):
        log.warning("Redirected to the Zoom homepage instead of the join page - "
                     "the /wc/ join link may be malformed or blocked. Retrying "
                     "with the raw invite URL.")
        await page.goto(meeting["url"], wait_until="domcontentloaded", timeout=90000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        log.info("landed on (2nd try): %s", page.url)
        await shot(page, cfg, "landed-2nd-try")
        await click_first(page, [
            "a:has-text('Join from your browser')",
            "a:has-text('Join from Your Browser')",
            "text=Join from your browser",
            "a#launch_btn ~ a",
        ], timeout=6000)
        await page.wait_for_timeout(3000)
        log.info("landed on (after browser-join click): %s", page.url)
        await shot(page, cfg, "landed-after-click")

    # 1) "Launch Meeting" interstitial -> take the browser link instead
    await click_first(page, [
        "a:has-text('Join from your browser')",
        "a:has-text('Join from Your Browser')",
        "text=Join from your browser",
        "#zoom-ui-frame a.mt-1",
    ], timeout=3000)
    await page.wait_for_timeout(2000)

    # 2) Name + passcode screen
    await fill_first(page, [
        "#input-for-name",
        "input[placeholder*='name' i]",
        "input[aria-label*='name' i]",
    ], name, timeout=8000)

    if meeting.get("passcode"):
        await fill_first(page, [
            "#input-for-pwd",
            "input[type='password']",
            "input[placeholder*='passcode' i]",
        ], meeting["passcode"], timeout=4000)

    # 3) Turn camera/mic OFF on the preview screen BEFORE joining
    await force_av_off(page)
    await click_first(page, [
        "#preview-video-control-button[aria-label*='Stop' i]",
        "button[aria-label='Stop Video']",
    ], timeout=1500)
    await page.wait_for_timeout(500)

    # 4) Join
    await click_first(page, [
        "button:has-text('Join')",
        "#joinBtn",
        ".preview-join-button",
        "button[type='submit']",
    ], timeout=10000)

    await page.wait_for_timeout(8000)
    await shot(page, cfg, "after-join")

    # 5) Audio dialog: connect (so you show as a normal participant) then mute.
    if cfg["join_computer_audio"]:
        await click_first(page, [
            "button:has-text('Join Audio by Computer')",
            "button:has-text('Join with Computer Audio')",
            "button:has-text('Join Audio')",
        ], timeout=8000)
        await page.wait_for_timeout(2500)

    # 6) Dismiss any leftover modals/tooltips
    await click_first(page, [
        "button:has-text('Got it')",
        "button:has-text('Continue')",
        "button[aria-label='close']",
    ], timeout=2000)

    await force_av_off(page)
    await shot(page, cfg, "in-meeting")
    log.info("Joined '%s' (muted, camera off)", meeting["name"])
    return context, page


async def in_meeting(page: Page) -> bool:
    """Best-effort check that we are still inside the meeting."""
    try:
        if page.is_closed():
            return False
        body = (await page.inner_text("body"))[:4000].lower()
        dead_markers = [
            "has ended", "meeting ended", "removed from the meeting",
            "you have been removed", "host has ended", "rejoin",
            "this meeting has been ended",
        ]
        if any(m in body for m in dead_markers):
            return False
        # A footer control means we're in the call (or in the waiting room UI)
        controls = await page.locator(
            "button[aria-label*='ute' i], button[aria-label*='ideo' i], "
            "#foot-bar, .footer__btns-container"
        ).count()
        if controls > 0:
            return True
        if "waiting" in body or "host will let you in" in body:
            return True  # waiting room counts as "still trying"
        return False
    except PWError:
        return False
    except Exception:
        return True  # don't churn on transient errors


async def leave(context: BrowserContext | None) -> None:
    if context is None:
        return
    try:
        browser = context.browser
        await context.close()
        if browser:
            await browser.close()
    except Exception:
        pass


async def hold(page: Page, context: BrowserContext, cfg: dict, until: datetime, tz) -> str:
    """
    Sit in the meeting until `until`. Re-mutes every 15s.
    Returns 'ended' if the window finished, 'dropped' if we lost the meeting.
    """
    while datetime.now(tz) < until:
        await asyncio.sleep(15)
        if not await in_meeting(page):
            log.warning("No longer in the meeting")
            await shot(page, cfg, "dropped")
            return "dropped"
        await force_av_off(page)
    return "ended"


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #

def parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def window_for(meeting: dict, now: datetime, tz) -> tuple[datetime, datetime] | None:
    """Return (start, end) datetimes for today if the meeting runs today."""
    days = [DAYS[d.lower()[:3]] for d in meeting["days"]]
    if now.weekday() not in days:
        return None
    start = now.replace(**dict(zip(
        ("hour", "minute", "second", "microsecond"),
        (parse_hhmm(meeting["start"]).hour, parse_hhmm(meeting["start"]).minute, 0, 0))))
    end = now.replace(**dict(zip(
        ("hour", "minute", "second", "microsecond"),
        (parse_hhmm(meeting["end"]).hour, parse_hhmm(meeting["end"]).minute, 0, 0))))
    if end <= start:                      # window crosses midnight
        end += timedelta(days=1)
    return start, end


def active_meeting(cfg: dict, now: datetime, tz) -> tuple[dict, datetime] | None:
    for m in cfg["meetings"]:
        if not m.get("enabled", True):
            continue
        w = window_for(m, now, tz)
        if w and w[0] <= now < w[1]:
            return m, w[1]
    return None


async def attend(cfg: dict, meeting: dict, until: datetime, tz) -> None:
    """Join, stay until `until`, retrying forever inside the window."""
    backoff = cfg["rejoin_backoff_seconds"]
    async with async_playwright() as pw:
        while datetime.now(tz) < until:
            context = None
            try:
                context, page = await join_meeting(pw, cfg, meeting)
                result = await hold(page, context, cfg, until, tz)
                await leave(context)
                if result == "ended":
                    log.info("Window for '%s' finished - left cleanly", meeting["name"])
                    return
                log.info("Retrying '%s' in %ss", meeting["name"], backoff)
            except Exception as e:
                log.error("Join attempt failed: %s", e)
                await leave(context)
                log.info("Retrying in %ss", backoff)
            if datetime.now(tz) >= until:
                return
            await asyncio.sleep(backoff)


async def run_scheduler(cfg: dict) -> None:
    tz = ZoneInfo(cfg["timezone"])
    log.info("Scheduler started | timezone=%s | %d meeting(s)",
             cfg["timezone"], len(cfg["meetings"]))
    for m in cfg["meetings"]:
        log.info("  - %s | %s | %s-%s", m["name"], ",".join(m["days"]), m["start"], m["end"])

    while True:
        now = datetime.now(tz)
        found = active_meeting(cfg, now, tz)
        if found:
            meeting, until = found
            log.info("Window OPEN: '%s' until %s", meeting["name"], until.strftime("%H:%M"))
            await attend(cfg, meeting, until, tz)
            await asyncio.sleep(60)   # don't instantly re-trigger the same window
        else:
            await asyncio.sleep(cfg["check_interval_seconds"])


async def run_once(cfg: dict, name: str, minutes: int | None = None) -> None:
    tz = ZoneInfo(cfg["timezone"])
    match = [m for m in cfg["meetings"] if name.lower() in m["name"].lower()]
    if not match:
        log.error("No meeting matching '%s'", name)
        sys.exit(1)
    meeting = match[0]
    now = datetime.now(tz)
    if minutes:
        until = now + timedelta(minutes=minutes)
    else:
        w = window_for(meeting, now, tz)
        until = w[1] if w else now + timedelta(hours=1)
        if until <= now:
            until = now + timedelta(hours=1)
    log.info("One-shot: '%s' until %s", meeting["name"], until.strftime("%Y-%m-%d %H:%M"))
    await attend(cfg, meeting, until, tz)


def main() -> None:
    ap = argparse.ArgumentParser(description="Silent Zoom web-client attendee")
    ap.add_argument("--once", metavar="NAME", help="join this meeting now until its end time")
    ap.add_argument("--test", metavar="NAME", help="join this meeting for 2 minutes and exit")
    ap.add_argument("--headful", action="store_true", help="force a visible browser window")
    args = ap.parse_args()

    cfg = load_config()
    if args.headful:
        cfg["headless"] = False

    try:
        if args.test:
            asyncio.run(run_once(cfg, args.test, minutes=2))
        elif args.once:
            asyncio.run(run_once(cfg, args.once))
        else:
            asyncio.run(run_scheduler(cfg))
    except KeyboardInterrupt:
        log.info("Stopped by user")


if __name__ == "__main__":
    main()
