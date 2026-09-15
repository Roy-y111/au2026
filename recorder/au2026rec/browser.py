"""把瀏覽器導到課程網頁。

attach（預設）
    接管一個用一般方式開、你自己登入好的瀏覽器。開瀏覽器的事交給
    browserlaunch 做：它只多加 --remote-debugging-port 與一個專用 profile
    目錄，其餘一律不加，所以登入頁看到的就是普通瀏覽器，不會被自動化偵測擋下。
    程式事後才從除錯埠接上去導頁 —— 導的是同一個分頁，視窗位置、大小、
    在哪個螢幕都不會變，OBS 錄到的永遠是對的畫面。

open
    只呼叫系統「開這個網址」，零自動化。attach 接不上時的退路
    （fallback_to_open），也可以自己設成主要模式。

程式不碰視窗大小與位置：你把瀏覽器擺成什麼樣，錄到的就是什麼樣。
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Sequence

log = logging.getLogger(__name__)

MODE_ATTACH = "attach"
MODE_OPEN = "open"
MODES = (MODE_ATTACH, MODE_OPEN)


class BrowserError(RuntimeError):
    pass


@dataclass
class BrowserSettings:
    mode: str = MODE_ATTACH
    cdp_url: str = "http://localhost:9222"
    fallback_to_open: bool = True
    settle_seconds: int = 8
    play_selectors: Sequence[str] = field(default_factory=list)
    center_player: bool = True
    unmute: bool = True
    dismiss_selectors: Sequence[str] = field(default_factory=list)
    close_page_after: bool = True


class Navigator:
    """導頁介面。runner 只認這四個方法。"""

    label = "navigator"

    def start(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError




    def unmute(self) -> tuple[bool, str]:
        """播放器預設靜音時把它打開 —— 不然整場錄下來沒聲音。

        優先點播放器自己的靜音鍵，而不是直接改 video.muted：
        影片若是以「靜音自動播放」啟動的，沒有使用者手勢就把它取消靜音，
        Chrome 有機會直接把影片暫停。點按鈕才是它認得的互動。
        """
        state = self.video_state()
        if state is None:
            return False, "找不到影片"
        if not state.get("muted") and (state.get("volume") or 0) > 0:
            return True, "本來就有聲音"

        for selector in (".vjs-mute-control", "[aria-label*='Unmute' i]", "[title*='Unmute' i]"):
            try:
                locator = self.page.locator(selector).first
                locator.wait_for(state="visible", timeout=1_500)
                locator.click(timeout=1_500)
            except Exception:
                continue
            after = self.video_state()
            if after and not after.get("muted"):
                log.info("已解除靜音（點 %s）", selector)
                return True, ""
            break

        # 按鈕點不到就直接改屬性，並確認影片沒有因此被暫停
        try:
            self.page.evaluate(
                "() => { const v = document.querySelector('video'); "
                "if (v) { v.muted = false; if (v.volume === 0) v.volume = 1; } }"
            )
        except Exception as exc:
            return False, f"解除靜音失敗：{exc}"
        after = self.video_state()
        if after and not after.get("muted"):
            if after.get("paused"):
                log.warning("解除靜音後影片被暫停了，重新播放")
                try:
                    self.page.evaluate("() => document.querySelector('video').play()")
                except Exception:
                    pass
            log.info("已解除靜音（直接設定 muted=false）")
            return True, ""
        return False, "試過按鈕與屬性都沒能解除靜音"


    def ensure_unmuted(self, attempts: int = 5, interval: float = 2.5) -> tuple[bool, str]:
        """反覆確認沒有被靜音。

        播放器會在「開始播放的那一瞬間」把自己設回靜音，所以解除一次不夠 ——
        要在開播後的頭幾秒持續盯著，被設回去就再解一次。
        """
        last = ""
        for attempt in range(1, attempts + 1):
            ok, why = self.unmute()
            last = why
            time.sleep(interval)
            state = self.video_state()
            if state and not state.get("muted") and (state.get("volume") or 0) > 0:
                if attempt > 1:
                    log.info("第 %d 次才解除靜音成功（播放器會在開播瞬間自己靜音）", attempt)
                return True, ""
            log.info("第 %d 次解除靜音後又被設回去了，再試", attempt)
        return False, last or "重試多次仍然是靜音"

    def center_player(self) -> bool:
        """把播放器捲到畫面正中央。

        課程頁的播放器預設偏上，整個螢幕錄下來會上下留一堆空白。
        捲動不會影響播放，也不會碰到播放器本身（點它會暫停）。
        """
        try:
            ok = self.page.evaluate(
                "() => { const v = document.querySelector('.video-js') "
                "|| document.querySelector('video'); "
                "if (!v) return false; "
                "v.scrollIntoView({block: 'center', inline: 'center'}); return true; }"
            )
        except Exception:
            log.debug("置中播放器失敗", exc_info=True)
            return False
        if ok:
            log.info("已把播放器捲到畫面中央")
        return bool(ok)

    # ── 播放狀態 ────────────────────────────────────────────────────────
    _VIDEO_STATE_JS = (
        "() => { const v = document.querySelector('video'); return v ? "
        "{t: Math.round(v.currentTime * 10) / 10, paused: v.paused, "
        "muted: v.muted, volume: v.volume, "
        "ready: v.readyState, buffered: v.buffered.length} : null; }"
    )

    def video_state(self) -> dict[str, Any] | None:
        """讀目前分頁的影片狀態；沒有 video 元素回 None。"""
        try:
            return self.page.evaluate(self._VIDEO_STATE_JS)
        except Exception:
            log.debug("讀影片狀態失敗", exc_info=True)
            return None

    @staticmethod
    def _looks_playing(state: dict[str, Any] | None) -> bool:
        """真的在播 = 沒暫停、有資料、有緩衝。只看 paused 會被轉圈圈騙過去。"""
        return bool(
            state
            and not state.get("paused")
            and (state.get("ready") or 0) >= 3
            and (state.get("buffered") or 0) > 0
        )

    def verify_playing(self, seconds: int = 6) -> tuple[bool, str]:
        """確認影片真的在前進。

        播放器顯示「播放中」不代表有畫面 —— 直播被打斷時 currentTime 會照跑，
        但 readyState 是 0、buffered 是空的，錄下來就是一整片轉圈圈。
        """
        before = self.video_state()
        if before is None:
            return False, "頁面上沒有 video 元素"
        time.sleep(seconds)
        after = self.video_state()
        if after is None:
            return False, "video 元素消失了"
        advanced = after["t"] > before["t"]
        has_data = (after.get("ready") or 0) >= 3 and (after.get("buffered") or 0) > 0
        if advanced and has_data:
            return True, ""
        if advanced:
            return False, (
                f"時間在跑但沒有畫面資料（readyState={after['ready']}、"
                f"buffered={after['buffered']}）—— 轉圈圈，錄下來會是空的"
            )
        return False, f"影片沒有前進（paused={after['paused']}、readyState={after['ready']}）"

    def open_session(self, url: str) -> dict[str, Any]:
        raise NotImplementedError

    def leave_session(self) -> None:
        return None

    def __enter__(self) -> "Navigator":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ── 系統預設瀏覽器 ──────────────────────────────────────────────────────

class OsOpenNavigator(Navigator):
    """把網址交給系統預設瀏覽器。沒有任何自動化能力，但幾乎不會壞。"""

    label = "系統預設瀏覽器（開新分頁）"

    def start(self) -> None:
        log.info("導頁方式：%s", self.label)

    def close(self) -> None:
        return None

    def open_session(self, url: str) -> dict[str, Any]:
        log.info("交給系統開啟 %s", url)
        opened = False
        if platform.system() == "Windows":
            try:
                os.startfile(url)  # type: ignore[attr-defined]
                opened = True
            except OSError as exc:
                log.warning("os.startfile 失敗，改用 webbrowser：%s", exc)
        elif platform.system() == "Darwin":
            opened = subprocess.run(["open", url], check=False).returncode == 0
        if not opened:
            opened = webbrowser.open(url, new=2)
        if not opened:
            raise BrowserError(f"系統開不起這個網址：{url}")
        return {
            "url": url,
            "played": False,
            "navigator": self.label,
            "note": "由系統瀏覽器開啟，未嘗試按播放鍵",
        }


# ── attach：接管已開好的瀏覽器 ──────────────────────────────────────────

class AttachNavigator(Navigator):
    label = "接管已開啟的瀏覽器（CDP）"

    def __init__(self, settings: BrowserSettings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    # ── 生命週期 ────────────────────────────────────────────────────────
    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 安裝問題
            raise BrowserError("缺少 playwright，請執行 pip install playwright") from exc

        self._playwright = sync_playwright().start()
        url = self.settings.cdp_url
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(url, timeout=10_000)
        except Exception as exc:
            self.close()
            raise BrowserError(
                f"接不上瀏覽器的除錯埠（{url}）：{exc}\n"
                "請先用選單的「開瀏覽器登入 AU2026」（或指令 au2026rec browser --set-mode）"
                "把瀏覽器開起來並登入，而且那個視窗要一直開著。"
            ) from exc

        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        pages = [p for p in self._context.pages if not p.is_closed()]
        self._page = pages[0] if pages else self._context.new_page()
        log.info(
            "已接管瀏覽器（%s），使用分頁：%s",
            url,
            (self._page.title() or self._page.url or "?")[:60],
        )

    def close(self) -> None:
        # 這個瀏覽器是使用者自己開的，只斷開連線，絕對不要關掉它。
        self._browser = self._context = self._page = None
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:  # pragma: no cover - 關閉失敗無所謂
            log.debug("停止 Playwright 時出錯", exc_info=True)
        self._playwright = None

    @property
    def page(self) -> Any:
        if self._page is None:
            raise BrowserError("瀏覽器尚未就緒")
        return self._page

    # ── 頁面操作 ────────────────────────────────────────────────────────
    def goto(self, url: str, *, timeout_ms: int = 60_000) -> Any:
        page = self.page
        log.info("導到 %s", url)
        try:
            page.bring_to_front()
        except Exception:
            log.debug("bring_to_front 失敗", exc_info=True)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            log.debug("networkidle 等不到，繼續往下走")
        return page

    def _click_first(
        self, selectors: Sequence[str], *, what: str, timeout_ms: int = 2_500
    ) -> str | None:
        page = self.page
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=timeout_ms)
                locator.click(timeout=timeout_ms)
            except Exception:
                continue
            log.info("%s：點到 %s", what, selector)
            return selector
        log.info("%s：候選選擇器都沒命中（不影響錄影，只是不會自動播）", what)
        return None

    def dismiss_popups(self) -> None:
        page = self.page
        for selector in self.settings.dismiss_selectors:
            try:
                locator = page.locator(selector).first
                if locator.is_visible(timeout=1_000):
                    locator.click(timeout=1_500)
                    log.info("關掉彈窗：%s", selector)
            except Exception:
                continue

    def open_session(self, url: str) -> dict[str, Any]:
        result: dict[str, Any] = {"url": url, "played": False, "navigator": self.label}
        self.goto(url)
        self.dismiss_popups()
        if self.settings.settle_seconds:
            time.sleep(self.settings.settle_seconds)
        self.dismiss_popups()

        # 直播通常進頁面就自動播。這時候絕對不能去點播放器 ——
        # video.js 點畫面會 toggle 暫停，等於把正在播的直播按停。
        if self._looks_playing(self.video_state()):
            log.info("影片已經在播，不去點播放鍵（避免把直播按成暫停）")
            result["play_selector"] = "（頁面自動播放）"
            result["played"] = True
        else:
            hit = self._click_first(self.settings.play_selectors, what="播放")
            result["play_selector"] = hit
            result["played"] = hit is not None

        playing, why = self.verify_playing()
        result["playing"] = playing
        if self.settings.unmute:
            ok, why = self.ensure_unmuted()
            result["unmuted"] = ok
            if not ok:
                log.warning("沒能解除靜音，這場可能會沒聲音：%s", why)
                result["note"] = (str(result.get("note") or "") + "；" if result.get("note") else "") + f"靜音未解除（{why}）"
        if self.settings.center_player:
            result["centered"] = self.center_player()
        if playing:
            log.info("已確認影片真的在播")
        else:
            result["note"] = why
            log.warning("影片沒有正常播放：%s", why)
        return result

    def leave_session(self) -> None:
        if not self.settings.close_page_after:
            return
        try:  # 導回空白頁，確保影片與聲音停下來
            self.page.goto("about:blank", wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            log.debug("導回空白頁失敗", exc_info=True)

    # ── 除錯輔助 ────────────────────────────────────────────────────────
    def probe(self, url: str, *, limit: int = 40) -> list[dict[str, str]]:
        """列出頁面上可點的元素，用來補 play_selectors 清單。"""
        self.goto(url)
        self.dismiss_popups()
        if self.settings.settle_seconds:
            time.sleep(self.settings.settle_seconds)
        script = """
        (limit) => {
          const out = [];
          const nodes = document.querySelectorAll(
            "button, a, [role=button], video, [class*=play i], [aria-label]"
          );
          for (const el of nodes) {
            const box = el.getBoundingClientRect();
            if (box.width < 4 || box.height < 4) continue;
            const style = getComputedStyle(el);
            if (style.visibility === "hidden" || style.display === "none") continue;
            out.push({
              tag: el.tagName.toLowerCase(),
              text: (el.innerText || el.value || "").trim().slice(0, 60),
              aria: el.getAttribute("aria-label") || "",
              id: el.id || "",
              cls: (el.className && el.className.toString ? el.className.toString() : "").slice(0, 80),
            });
            if (out.length >= limit) break;
          }
          return out;
        }
        """
        return list(self.page.evaluate(script, limit))


# ── 工廠 ────────────────────────────────────────────────────────────────

def make_navigator(settings: BrowserSettings) -> Navigator:
    """依設定建立導頁器；attach 接不上時可退回 open。"""
    mode = (settings.mode or MODE_ATTACH).lower()
    if mode not in MODES:
        raise BrowserError(f"[browser] mode 只能是 {' / '.join(MODES)}，讀到 {settings.mode!r}")

    if mode == MODE_OPEN:
        navigator: Navigator = OsOpenNavigator()
        navigator.start()
        return navigator

    navigator = AttachNavigator(settings)
    try:
        navigator.start()
    except BrowserError as exc:
        if settings.fallback_to_open:
            log.warning("%s", exc)
            log.warning("改用系統預設瀏覽器開新分頁（錄影照舊進行，只是不會自動按播放）")
            fallback = OsOpenNavigator()
            fallback.start()
            return fallback
        raise
    return navigator
