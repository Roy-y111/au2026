"""每堂課一個資料夾，裡面放影片、字幕、附件與課程資訊。

    AU2026/
      BES2251-D AI for Ideation/
        BES2251-D.mp4                  下載的影片，或搬過來的螢幕錄影
        BES2251-D.en.srt               官方英文字幕（只有下載模式拿得到）
        ClassHandout-....pdf           講義
        BES2251-....pdf                簡報
        session.json                   課程資訊與來源網址

附件的取得方式是實測出來的：課程頁上的「View Downloads」按鈕會開一個對話框，
裡面每個項目點下去會**開新分頁**指向 static.rainfocus.com 上的檔案。網址是
動態組出來的（檔名帶一段 id），頁面原始碼裡找不到，所以只能點開來攔。
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# AU 匯出的課表會在標題前面掛「(Favorited)」，那是收藏狀態不是課名，別進資料夾名。
_NOISE = re.compile(r"^\s*\((?:Favorited|已收藏)\)\s*", re.IGNORECASE)
DOWNLOAD_BUTTON = "button.session-downloads"
DOWNLOAD_LINKS = ".rf-downloadFiles-modal .download-file-link a"
# 附件都掛在這些路徑底下（實測：講義是 srchandout，簡報是 srcpresentationpdf）
ATTACHMENT_MARKERS = ("/srchandout/", "/srcpresentationpdf/", "/srcsupplement/")


class BundleError(RuntimeError):
    pass


def safe_name(text: str, max_length: int = 80) -> str:
    cleaned = _ILLEGAL.sub("", _NOISE.sub("", text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ")
    return cleaned or "session"


def session_folder(root: Path, code: str, title: str) -> Path:
    """課程資料夾：<root>/<代碼> <標題>/"""
    # 標題先各自清一遍，收藏標記才不會卡在代碼後面躲過開頭比對。
    name = safe_name(f"{safe_name(code, 30)} {safe_name(title)}".strip() if code else title)
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_session_info(folder: Path, session: Any, extra: dict[str, Any] | None = None) -> Path:
    """把課程資訊寫成 session.json，方便日後對照與重跑。"""
    info = {
        "code": getattr(session, "code", ""),
        "title": _NOISE.sub("", getattr(session, "title", "") or ""),
        "mode": getattr(session, "mode", ""),
        "track": getattr(session, "track", ""),
        "room": getattr(session, "room", ""),
        "url": getattr(session, "url", ""),
        "start": getattr(session, "start", None).isoformat() if getattr(session, "start", None) else "",
        "end": getattr(session, "end", None).isoformat() if getattr(session, "end", None) else "",
    }
    info.update(extra or {})
    target = folder / "session.json"
    target.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return target


# ── 附件 ────────────────────────────────────────────────────────────────

@dataclass
class Attachment:
    name: str
    url: str


def collect_attachment_urls(page: Any, *, wait_seconds: int = 20) -> list[Attachment]:
    """打開課程頁的下載清單，把每個附件的真實網址攔下來。

    清單裡的 <a> 沒有 href，點下去才會開新分頁指向檔案，所以要監聽 context
    的新分頁；攔到就把那個分頁關掉，不打擾使用者正在看的畫面。
    """
    context = page.context
    grabbed: list[str] = []

    def on_page(new_page: Any) -> None:
        try:
            url = new_page.url
            if any(marker in url for marker in ATTACHMENT_MARKERS):
                grabbed.append(url)
                new_page.close()
        except Exception:
            log.debug("處理附件分頁時出錯", exc_info=True)

    context.on("page", on_page)
    try:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if page.locator(DOWNLOAD_BUTTON).count():
                break
            time.sleep(1)
        if not page.locator(DOWNLOAD_BUTTON).count():
            return []  # 這堂課沒有附件

        page.evaluate(f"() => document.querySelector({DOWNLOAD_BUTTON!r}).click()")
        time.sleep(2.5)
        links = page.locator(DOWNLOAD_LINKS)
        count = links.count()
        names = [links.nth(i).inner_text().strip() for i in range(count)]
        log.info("這堂課有 %d 個附件", count)
        for index in range(count):
            try:
                links.nth(index).click(timeout=8_000)
                time.sleep(2.5)
            except Exception as exc:
                log.warning("點附件 %s 失敗：%s", names[index][:40], str(exc)[:80])
    finally:
        try:
            context.remove_listener("page", on_page)
        except Exception:
            log.debug("移除分頁監聽失敗", exc_info=True)

    seen: set[str] = set()
    result: list[Attachment] = []
    for url in grabbed:
        if url in seen:
            continue
        seen.add(url)
        raw = urllib.parse.unquote(url.rsplit("/", 1)[-1].split("?")[0])
        result.append(Attachment(name=safe_name(raw, 120), url=url))
    return result


def download_attachment(attachment: Attachment, folder: Path, *, force: bool = False) -> Path | None:
    """把附件存進資料夾。已經存在就跳過（可斷點續跑）。"""
    target = folder / attachment.name
    if target.exists() and target.stat().st_size > 0 and not force:
        log.info("附件已存在，跳過：%s", attachment.name[:60])
        return target
    try:
        request = urllib.request.Request(
            attachment.url, headers={"User-Agent": "Mozilla/5.0 au2026rec"}
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            target.write_bytes(response.read())
    except Exception as exc:
        log.warning("附件下載失敗 %s：%s", attachment.name[:50], str(exc)[:100])
        return None
    log.info("附件已存 %s（%.0f KB）", attachment.name[:60], target.stat().st_size / 1024)
    return target


def fetch_attachments(page: Any, folder: Path, *, force: bool = False) -> list[Path]:
    """抓齊這堂課的所有附件。沒有附件就回空清單，不視為錯誤。"""
    try:
        attachments = collect_attachment_urls(page)
    except Exception as exc:
        log.warning("讀取附件清單失敗：%s", str(exc)[:120])
        return []
    saved = [download_attachment(a, folder, force=force) for a in attachments]
    return [p for p in saved if p is not None]
