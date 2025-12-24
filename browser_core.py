from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

# IMPORTANT: set Playwright browsers path only in container.
if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and os.path.exists("/pw-browsers"):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/pw-browsers"

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/browser", tags=["browser"])


# -----------------------------
# Config
# -----------------------------
SESSION_TTL_SECONDS = int(os.environ.get("BROWSER_SESSION_TTL_SECONDS", "1800"))  # 30 min
TAB_TTL_SECONDS = int(os.environ.get("BROWSER_TAB_TTL_SECONDS", "1800"))
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("BROWSER_CLEANUP_INTERVAL_SECONDS", "30"))
STREAM_FPS = float(os.environ.get("BROWSER_STREAM_FPS", "6"))  # ~6 fps default
STREAM_JPEG_QUALITY = int(os.environ.get("BROWSER_STREAM_JPEG_QUALITY", "50"))
VIEWPORT = {"width": 1280, "height": 720}


# -----------------------------
# Helpers
# -----------------------------
_CHROME_INTERNAL_RE = re.compile(r"^chrome://", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_internal(url: str) -> bool:
    return bool(_CHROME_INTERNAL_RE.match(url or ""))


def _parse_internal_search(url: str) -> Optional[str]:
    """Returns query if url is chrome://search?q=..."""
    if not url:
        return None
    if not url.lower().startswith("chrome://search"):
        return None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    q = (qs.get("q") or [""])[0].strip()
    return q


async def _google_cse_search(query: str, start: int = 1, num: int = 10) -> Dict[str, Any]:
    api_key = os.environ.get("GOOGLE_CSE_API_KEY")
    cx = os.environ.get("GOOGLE_CSE_CX")
    if not api_key or not cx:
        raise RuntimeError("Missing GOOGLE_CSE_API_KEY or GOOGLE_CSE_CX")

    start = max(1, min(int(start), 91))
    num = max(1, min(int(num), 10))

    timeout = httpx.Timeout(6.0, connect=3.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": api_key, "cx": cx, "q": query, "start": start, "num": num},
        )
        r.raise_for_status()
        return r.json()


async def _render_search_results_html(query: str) -> str:
    """Render a lightweight internal HTML page with CSE JSON results.

    This keeps the existing UI intact (we still navigate inside the virtual browser),
    but avoids relying on google.com HTML rendering.
    """

    safe_q = (query or "").strip()

    def esc(s: str) -> str:
        return (
            (s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    try:
        data = await _google_cse_search(safe_q, start=1, num=10)
        items = data.get("items") or []
        total = ((data.get("searchInformation") or {}).get("totalResults") or "0")
    except Exception as e:
        logger.warning(f"CSE search failed, falling back to google.com: {e}")
        google_url = f"https://www.google.com/search?q={httpx.QueryParams({'q': safe_q}).get('q')}"
        return f"""<!doctype html>
<html>
<head>
  <meta charset=\\"utf-8\\" />
  <meta name=\\"viewport\\" content=\\"width=device-width, initial-scale=1\\" />
  <title>Search</title>
  <style>
    body {{ background:#0b0b0f; color:#e4e4e7; font-family: ui-sans-serif, system-ui; padding: 24px; }}
    a {{ color:#38bdf8; }}
    .card {{ background: rgba(39,39,42,.6); border: 1px solid rgba(63,63,70,.6); border-radius: 16px; padding: 16px; }}
    .muted {{ color:#a1a1aa; }}
  </style>
</head>
<body>
  <div class=\\"card\\">
    <h2 style=\\"margin:0 0 8px 0\\">Search temporarily unavailable</h2>
    <p class=\\"muted\\" style=\\"margin:0 0 12px 0\\">Google CSE API request failed. You can still open the web search directly:</p>
    <a href=\\"{esc(google_url)}\\" target=\\"_blank\\">Open Google search</a>
  </div>
</body>
</html>"""

    rows = []
    for it in items:
        title = esc(it.get("title") or "")
        link = esc(it.get("link") or "")
        snippet = esc(it.get("snippet") or "")
        display = esc(it.get("displayLink") or "")
        rows.append(
            f"""
      <div class=\\"result\\">
        <a class=\\"title\\" href=\\"{link}\\">{title}</a>
        <div class=\\"meta\\">{display}</div>
        <div class=\\"snippet\\">{snippet}</div>
      </div>
            """
        )

    results_html = "\
".join(rows) if rows else "<div class=\\"muted\\">No results</div>"

    return f"""<!doctype html>
<html>
<head>
  <meta charset=\\"utf-8\\" />
  <meta name=\\"viewport\\" content=\\"width=device-width, initial-scale=1\\" />
  <title>Search: {esc(safe_q)}</title>
  <style>
    body {{ background:#0b0b0f; color:#e4e4e7; font-family: ui-sans-serif, system-ui; margin:0; }}
    .topbar {{ padding: 16px 20px; border-bottom: 1px solid rgba(63,63,70,.6); background: rgba(24,24,27,.8); position: sticky; top: 0; }}
    .q {{ font-size: 18px; font-weight: 700; margin: 0; }}
    .sub {{ margin: 6px 0 0 0; color:#a1a1aa; font-size: 12px; }}
    .wrap {{ padding: 18px 20px 28px 20px; }}
    .result {{ padding: 14px 14px; border: 1px solid rgba(63,63,70,.6); border-radius: 14px; background: rgba(39,39,42,.35); margin-bottom: 12px; }}
    .title {{ display:block; font-weight: 700; color:#38bdf8; text-decoration:none; }}
    .title:hover {{ text-decoration: underline; }}
    .meta {{ color:#34d399; font-size: 12px; margin-top: 6px; overflow:hidden; text-overflow: ellipsis; white-space:nowrap; }}
    .snippet {{ color:#d4d4d8; font-size: 13px; margin-top: 8px; line-height: 1.35; }}
    .muted {{ color:#a1a1aa; }}
  </style>
</head>
<body>
  <div class=\\"topbar\\">
    <p class=\\"q\\">{esc(safe_q)}</p>
    <p class=\\"sub\\">About {esc(str(total))} results (via Custom Search)</p>
  </div>
  <div class=\\"wrap\\">
    {results_html}
  </div>
</body>
</html>"""


# -----------------------------
# Session management
# -----------------------------
class BrowserSessionManager:
    def __init__(self):
        self.sessions: Dict[str, dict] = {}
        self.playwright = None
        self.browser: Optional[Browser] = None
        self._lock = asyncio.Lock()
        self._cleanup_stop = asyncio.Event()
        self._cleanup_task: Optional[asyncio.Task] = None

    async def initialize(self):
        async with self._lock:
            if self.playwright is None:
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-web-security",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ],
                )
                logger.info("Playwright browser initialized")

            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_stop.clear()
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        try:
            while not self._cleanup_stop.is_set():
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                await self._cleanup_idle()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Cleanup loop stopped unexpectedly: {e}")

    async def _cleanup_idle(self):
        now = _utcnow()
        to_close_sessions = []

        for sid, sess in list(self.sessions.items()):
            tabs = sess.get("tabs", {})
            for tid, tab in list(tabs.items()):
                last_used = tab.get("last_used") or sess.get("last_used") or sess.get("created_at")
                if (now - last_used).total_seconds() > TAB_TTL_SECONDS and len(tabs) > 1:
                    try:
                        await tab["page"].close()
                    except Exception:
                        pass
                    tabs.pop(tid, None)
                    if sess.get("active_tab_id") == tid:
                        sess["active_tab_id"] = next(iter(tabs.keys()), None)

            last_used_sess = sess.get("last_used") or sess.get("created_at")
            if (now - last_used_sess).total_seconds() > SESSION_TTL_SECONDS:
                to_close_sessions.append(sid)

        for sid in to_close_sessions:
            try:
                await self.close_session(sid)
            except Exception as e:
                logger.warning(f"Failed to cleanup session {sid}: {e}")

    async def create_session(self) -> Tuple[str, str]:
        await self.initialize()
        if not self.browser:
            raise RuntimeError("Playwright browser not initialized")

        session_id = str(uuid.uuid4())
        context = await self.browser.new_context(
            viewport=VIEWPORT,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        first_tab_id = str(uuid.uuid4())
        page = await context.new_page()

        self.sessions[session_id] = {
            "context": context,
            "tabs": {
                first_tab_id: {
                    "page": page,
                    "created_at": _utcnow(),
                    "last_used": _utcnow(),
                    "history": [],
                    "history_index": -1,
                }
            },
            "active_tab_id": first_tab_id,
            "created_at": _utcnow(),
            "last_used": _utcnow(),
            "lock": asyncio.Lock(),
        }

        logger.info(f"Created browser session: {session_id} (tab {first_tab_id})")
        return session_id, first_tab_id

    async def get_session(self, session_id: str) -> Optional[dict]:
        return self.sessions.get(session_id)

    async def create_tab(self, session_id: str) -> str:
        session = await self.get_session(session_id)
        if not session:
            raise KeyError("Session not found")

        async with session["lock"]:
            context: BrowserContext = session["context"]
            tab_id = str(uuid.uuid4())
            page = await context.new_page()
            session["tabs"][tab_id] = {
                "page": page,
                "created_at": _utcnow(),
                "last_used": _utcnow(),
                "history": [],
                "history_index": -1,
            }
            session["active_tab_id"] = tab_id
            session["last_used"] = _utcnow()
            return tab_id

    async def close_tab(self, session_id: str, tab_id: str):
        session = await self.get_session(session_id)
        if not session:
            raise KeyError("Session not found")

        async with session["lock"]:
            tabs = session.get("tabs", {})
            if tab_id not in tabs:
                raise KeyError("Tab not found")

            tab = tabs.pop(tab_id)
            try:
                await tab["page"].close()
            except Exception:
                pass

            if not tabs:
                await self.close_session(session_id)
                return

            if session.get("active_tab_id") == tab_id:
                session["active_tab_id"] = next(iter(tabs.keys()))

            session["last_used"] = _utcnow()

    async def activate_tab(self, session_id: str, tab_id: str):
        session = await self.get_session(session_id)
        if not session:
            raise KeyError("Session not found")
        if tab_id not in session.get("tabs", {}):
            raise KeyError("Tab not found")
        session["active_tab_id"] = tab_id
        session["last_used"] = _utcnow()
        session["tabs"][tab_id]["last_used"] = _utcnow()

    async def _resolve_tab(self, session: dict, tab_id: Optional[str]) -> Tuple[str, dict]:
        tid = tab_id or session.get("active_tab_id")
        tabs = session.get("tabs", {})
        if not tid or tid not in tabs:
            raise KeyError("Tab not found")
        return tid, tabs[tid]

    async def get_page(self, session_id: str, tab_id: Optional[str] = None) -> Tuple[str, Page]:
        session = await self.get_session(session_id)
        if not session:
            raise KeyError("Session not found")
        tid, tab = await self._resolve_tab(session, tab_id)
        session["last_used"] = _utcnow()
        tab["last_used"] = _utcnow()
        return tid, tab["page"]

    async def close_session(self, session_id: str):
        if session_id in self.sessions:
            session = self.sessions[session_id]
            try:
                ctx: BrowserContext = session["context"]
                await ctx.close()
            except Exception:
                pass
            self.sessions.pop(session_id, None)
            logger.info(f"Closed browser session: {session_id}")

    async def cleanup(self):
        try:
            self._cleanup_stop.set()
            if self._cleanup_task and not self._cleanup_task.done():
                self._cleanup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._cleanup_task
        except Exception:
            pass

        for sid in list(self.sessions.keys()):
            with contextlib.suppress(Exception):
                await self.close_session(sid)

        if self.browser:
            with contextlib.suppress(Exception):
                await self.browser.close()
        if self.playwright:
            with contextlib.suppress(Exception):
                await self.playwright.stop()


session_manager = BrowserSessionManager()


# -----------------------------
# Models
# -----------------------------
class CreateSessionResponse(BaseModel):
    session_id: str
    created_at: datetime
    initial_tab_id: str


class CreateTabResponse(BaseModel):
    tab_id: str


class NavigateRequest(BaseModel):
    url: str
    tab_id: Optional[str] = None


class ClickRequest(BaseModel):
    x: float
    y: float
    button: str = "left"
    click_count: int = 1
    tab_id: Optional[str] = None


class TypeRequest(BaseModel):
    text: str
    tab_id: Optional[str] = None


class KeyPressRequest(BaseModel):
    key: str
    modifiers: Optional[Dict[str, bool]] = None
    tab_id: Optional[str] = None


class ScrollRequest(BaseModel):
    delta_x: float = 0
    delta_y: float = 0
    tab_id: Optional[str] = None


class SessionStatusResponse(BaseModel):
    session_id: str
    active_tab_id: str
    current_url: str
    title: str
    can_go_back: bool
    can_go_forward: bool


class ScreenshotResponse(BaseModel):
    screenshot: str
    url: str
    title: str
    tab_id: str


# -----------------------------
# Endpoints
# -----------------------------
@router.post("/session", response_model=CreateSessionResponse)
async def create_session():
    try:
        session_id, tab_id = await session_manager.create_session()
        session = await session_manager.get_session(session_id)
        if not session:
            raise RuntimeError("Session not created")
        return CreateSessionResponse(session_id=session_id, created_at=session["created_at"], initial_tab_id=tab_id)
    except Exception as e:
        logger.error(f"Failed to create session: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/session/{session_id}")
async def close_session(session_id: str):
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    await session_manager.close_session(session_id)
    return {"status": "closed"}


@router.post("/session/{session_id}/tabs", response_model=CreateTabResponse)
async def create_tab(session_id: str):
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        tab_id = await session_manager.create_tab(session_id)
        return CreateTabResponse(tab_id=tab_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/session/{session_id}/tabs/{tab_id}")
async def close_tab(session_id: str, tab_id: str):
    try:
        await session_manager.close_tab(session_id, tab_id)
        return {"status": "closed", "tab_id": tab_id}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/session/{session_id}/tabs/{tab_id}/activate")
async def activate_tab(session_id: str, tab_id: str):
    try:
        await session_manager.activate_tab(session_id, tab_id)
        return {"status": "active", "tab_id": tab_id}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/session/{session_id}/status", response_model=SessionStatusResponse)
async def get_session_status(session_id: str, tab_id: Optional[str] = None):
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        tid, page = await session_manager.get_page(session_id, tab_id)
        tab = session["tabs"][tid]
        history = tab["history"]
        history_index = tab["history_index"]

        return SessionStatusResponse(
            session_id=session_id,
            active_tab_id=session.get("active_tab_id"),
            current_url=page.url,
            title=await page.title(),
            can_go_back=history_index > 0,
            can_go_forward=history_index < len(history) - 1,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Tab not found")


@router.post("/{session_id}/navigate")
async def navigate(session_id: str, request: NavigateRequest):
    try:
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        tid, page = await session_manager.get_page(session_id, request.tab_id)
        tab = session["tabs"][tid]

        if _is_internal(request.url):
            q = _parse_internal_search(request.url)
            if q is not None:
                html = await _render_search_results_html(q)
                await page.set_content(html, wait_until="domcontentloaded")
                await page.wait_for_timeout(200)
                tab["history"] = tab["history"][: tab["history_index"] + 1]
                tab["history"].append(request.url)
                tab["history_index"] = len(tab["history"]) - 1
                return {"status": "navigated", "url": request.url, "title": f"Search: {q}", "tab_id": tid}

            await page.set_content(
                "<html><body style='background:#0b0b0f;color:#e4e4e7;font-family:system-ui;padding:24px'>"
                "<h2>Internal page</h2><p>This page is not implemented.</p></body></html>",
                wait_until="domcontentloaded",
            )
            return {"status": "navigated", "url": request.url, "title": "Internal page", "tab_id": tid}

        await page.goto(request.url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            await asyncio.sleep(0.5)

        tab["history"] = tab["history"][: tab["history_index"] + 1]
        tab["history"].append(page.url)
        tab["history_index"] = len(tab["history"]) - 1

        return {"status": "navigated", "url": page.url, "title": await page.title(), "tab_id": tid}
    except HTTPException:
        raise
    except KeyError:
        raise HTTPException(status_code=404, detail="Session or tab not found")
    except Exception as e:
        logger.error(f"Navigation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{session_id}/back")
async def go_back(session_id: str, tab_id: Optional[str] = None):
    try:
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        tid, page = await session_manager.get_page(session_id, tab_id)
        tab = session["tabs"][tid]

        if tab["history_index"] > 0:
            tab["history_index"] -= 1
            await page.go_back()
            return {"status": "success", "url": page.url, "tab_id": tid}
        return {"status": "no_history", "tab_id": tid}
    except HTTPException:
        raise
    except KeyError:
        raise HTTPException(status_code=404, detail="Session or tab not found")


@router.post("/{session_id}/forward")
async def go_forward(session_id: str, tab_id: Optional[str] = None):
    try:
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        tid, page = await session_manager.get_page(session_id, tab_id)
        tab = session["tabs"][tid]

        if tab["history_index"] < len(tab["history"]) - 1:
            tab["history_index"] += 1
            await page.go_forward()
            return {"status": "success", "url": page.url, "tab_id": tid}
        return {"status": "no_forward_history", "tab_id": tid}
    except HTTPException:
        raise
    except KeyError:
        raise HTTPException(status_code=404, detail="Session or tab not found")


@router.post("/{session_id}/refresh")
async def refresh(session_id: str, tab_id: Optional[str] = None):
    try:
        tid, page = await session_manager.get_page(session_id, tab_id)
        await page.reload()
        return {"status": "refreshed", "url": page.url, "tab_id": tid}
    except KeyError:
        raise HTTPException(status_code=404, detail="Session or tab not found")


@router.get("/{session_id}/screenshot", response_model=ScreenshotResponse)
async def get_screenshot(session_id: str, tab_id: Optional[str] = None):
    try:
        tid, page = await session_manager.get_page(session_id, tab_id)
        screenshot_bytes = await page.screenshot(type="jpeg", quality=60)
        screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        return ScreenshotResponse(
            screenshot=f"data:image/jpeg;base64,{screenshot_base64}",
            url=page.url,
            title=await page.title(),
            tab_id=tid,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Session or tab not found")
    except Exception as e:
        logger.error(f"Screenshot failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{session_id}/click")
async def click(session_id: str, request: ClickRequest):
    try:
        tid, page = await session_manager.get_page(session_id, request.tab_id)
        await page.mouse.click(request.x, request.y, button=request.button, click_count=request.click_count)
        return {"status": "clicked", "tab_id": tid}
    except KeyError:
        raise HTTPException(status_code=404, detail="Session or tab not found")


@router.post("/{session_id}/type")
async def type_text(session_id: str, request: TypeRequest):
    try:
        tid, page = await session_manager.get_page(session_id, request.tab_id)
        await page.keyboard.type(request.text)
        return {"status": "typed", "tab_id": tid}
    except KeyError:
        raise HTTPException(status_code=404, detail="Session or tab not found")


@router.post("/{session_id}/keypress")
async def keypress(session_id: str, request: KeyPressRequest):
    try:
        tid, page = await session_manager.get_page(session_id, request.tab_id)

        modifiers = request.modifiers or {}
        keys = []
        if modifiers.get("ctrl"):
            keys.append("Control")
        if modifiers.get("alt"):
            keys.append("Alt")
        if modifiers.get("shift"):
            keys.append("Shift")
        if modifiers.get("meta"):
            keys.append("Meta")
        keys.append(request.key)

        await page.keyboard.press("+".join(keys))
        return {"status": "pressed", "tab_id": tid}
    except KeyError:
        raise HTTPException(status_code=404, detail="Session or tab not found")


@router.post("/{session_id}/scroll")
async def scroll(session_id: str, request: ScrollRequest):
    try:
        tid, page = await session_manager.get_page(session_id, request.tab_id)
        await page.mouse.wheel(request.delta_x, request.delta_y)
        return {"status": "scrolled", "tab_id": tid}
    except KeyError:
        raise HTTPException(status_code=404, detail="Session or tab not found")


# -----------------------------
# WebSocket streaming (single socket per session; client can switch active tab)
# -----------------------------
@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()

    session = await session_manager.get_session(session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    streaming = True
    active_tab_id = session.get("active_tab_id")
    is_navigating = False
    last_sent_state: Optional[str] = None

    async def send_state(state: str, extra: Optional[dict] = None):
        nonlocal last_sent_state
        if state == last_sent_state:
            return
        last_sent_state = state
        payload = {"type": "state", "state": state, "ts": _utcnow().isoformat(), "tab_id": active_tab_id}
        if extra:
            payload.update(extra)
        with contextlib.suppress(Exception):
            await websocket.send_json(payload)

    async def stream_screenshots():
        nonlocal streaming, active_tab_id
        frame_delay = max(0.05, 1.0 / max(1.0, STREAM_FPS))

        while streaming:
            try:
                _, page = await session_manager.get_page(session_id, active_tab_id)

                if is_navigating:
                    await asyncio.sleep(0.25)
                    continue

                screenshot_bytes = await page.screenshot(type="jpeg", quality=STREAM_JPEG_QUALITY)
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")

                await websocket.send_json(
                    {
                        "type": "screenshot",
                        "data": f"data:image/jpeg;base64,{screenshot_base64}",
                        "url": page.url,
                        "title": await page.title(),
                        "tab_id": active_tab_id,
                        "ts": _utcnow().isoformat(),
                    }
                )

                await asyncio.sleep(frame_delay)
            except KeyError:
                await send_state("tab_missing")
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"Streaming error: {e}")
                await asyncio.sleep(0.5)

    await send_state("connected", {"session_id": session_id})
    stream_task = asyncio.create_task(stream_screenshots())

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")

            if event_type == "ping":
                await websocket.send_json({"type": "pong", "ts": _utcnow().isoformat()})
                continue

            if event_type == "activate_tab":
                tab_id = data.get("tabId")
                if tab_id:
                    try:
                        await session_manager.activate_tab(session_id, tab_id)
                        active_tab_id = tab_id
                        await send_state("tab_activated", {"tab_id": active_tab_id})
                    except KeyError:
                        await websocket.send_json({"type": "error", "message": "Tab not found", "tab_id": tab_id})
                continue

            tab_id = data.get("tabId") or active_tab_id

            if event_type == "new_tab":
                try:
                    new_id = await session_manager.create_tab(session_id)
                    active_tab_id = new_id
                    await websocket.send_json({"type": "tab_created", "tab_id": new_id})
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": str(e)})
                continue

            if event_type == "close_tab":
                close_id = data.get("tabId")
                if close_id:
                    try:
                        await session_manager.close_tab(session_id, close_id)
                        session = await session_manager.get_session(session_id)
                        if not session:
                            await websocket.send_json({"type": "session_closed"})
                            await websocket.close()
                            return
                        active_tab_id = session.get("active_tab_id")
                        await websocket.send_json({"type": "tab_closed", "tab_id": close_id, "active_tab_id": active_tab_id})
                    except KeyError:
                        await websocket.send_json({"type": "error", "message": "Tab not found"})
                continue

            try:
                _, page = await session_manager.get_page(session_id, tab_id)
            except KeyError:
                await websocket.send_json({"type": "error", "message": "Session or tab not found"})
                continue

            if event_type == "navigate":
                url = data.get("url")
                if url:
                    try:
                        is_navigating = True
                        await send_state("navigating")

                        if _is_internal(url):
                            q = _parse_internal_search(url)
                            if q is not None:
                                html = await _render_search_results_html(q)
                                await page.set_content(html, wait_until="domcontentloaded")
                                await page.wait_for_timeout(200)
                            else:
                                await page.set_content(
                                    "<html><body style='background:#0b0b0f;color:#e4e4e7;font-family:system-ui;padding:24px'>"
                                    "<h2>Internal page</h2><p>This page is not implemented.</p></body></html>",
                                    wait_until="domcontentloaded",
                                )
                        else:
                            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception:
                                await asyncio.sleep(0.5)

                        session = await session_manager.get_session(session_id)
                        if session and tab_id in session.get("tabs", {}):
                            tab = session["tabs"][tab_id]
                            tab["history"] = tab["history"][: tab["history_index"] + 1]
                            tab["history"].append(url if _is_internal(url) else page.url)
                            tab["history_index"] = len(tab["history"]) - 1

                    except Exception as e:
                        await websocket.send_json({"type": "error", "message": str(e), "tab_id": tab_id})
                    finally:
                        is_navigating = False
                        await send_state("idle")

            elif event_type == "click":
                x, y = data.get("x", 0), data.get("y", 0)
                button = data.get("button", "left")
                click_count = int(data.get("clickCount", 1) or 1)
                await page.mouse.click(x, y, button=button, click_count=click_count)

            elif event_type == "type":
                text = data.get("text", "")
                await page.keyboard.type(text)

            elif event_type == "keypress":
                key = data.get("key", "")
                modifiers = data.get("modifiers") or {}
                keys = []
                if modifiers.get("ctrl"):
                    keys.append("Control")
                if modifiers.get("alt"):
                    keys.append("Alt")
                if modifiers.get("shift"):
                    keys.append("Shift")
                if modifiers.get("meta"):
                    keys.append("Meta")
                keys.append(key)
                await page.keyboard.press("+".join(keys) if len(keys) > 1 else key)

            elif event_type == "scroll":
                delta_x = data.get("deltaX", 0)
                delta_y = data.get("deltaY", 0)
                await page.mouse.wheel(delta_x, delta_y)

            elif event_type == "back":
                session = await session_manager.get_session(session_id)
                if session and tab_id in session.get("tabs", {}):
                    tab = session["tabs"][tab_id]
                    if tab["history_index"] > 0:
                        tab["history_index"] -= 1
                        await page.go_back()

            elif event_type == "forward":
                session = await session_manager.get_session(session_id)
                if session and tab_id in session.get("tabs", {}):
                    tab = session["tabs"][tab_id]
                    if tab["history_index"] < len(tab["history"]) - 1:
                        tab["history_index"] += 1
                        await page.go_forward()

            elif event_type == "refresh":
                await page.reload()

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {session_id}")
    finally:
        streaming = False
        stream_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stream_task


async def cleanup_sessions():
    await session_manager.cleanup()
