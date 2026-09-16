"""直接下載 On-demand 課程：影片 + 官方字幕 + 附件，收進課程資料夾。

跟錄影不同，這條路不用等實際時間、不吃螢幕、不動 OBS —— 播放器載入時會去要
HLS 的 master.m3u8，我們把那個網址攔下來，交給 ffmpeg 直接拉檔。一小時的課
大約幾分鐘就抓完（實測 19–25 倍速）。

**這比螢幕錄影更容易觸犯平台條款，風險也更高。** 使用前請讀 DISCLAIMER.md。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from au2026rec import bundle, hls

log = logging.getLogger(__name__)


class DownloadError(RuntimeError):
    pass


@dataclass
class DownloadSettings:
    root: Path
    height: int = 720
    ffmpeg: str = "ffmpeg"
    subtitles: bool = True
    attachments: bool = True
    manifest_wait_seconds: int = 45
    force: bool = False


def ensure_ffmpeg(command: str) -> str:
    """確認 ffmpeg 叫得動，回傳可執行的路徑。"""
    resolved = shutil.which(command) or (command if Path(command).exists() else None)
    if not resolved:
        raise DownloadError(
            f"找不到 ffmpeg（設定值：{command}）。請安裝 ffmpeg 並加進 PATH，"
            "或在 config.toml 的 [library] ffmpeg 填完整路徑。"
        )
    return resolved


# ── 攔 master.m3u8 ──────────────────────────────────────────────────────

def capture_master_url(navigator: Any, url: str, *, wait_seconds: int = 45) -> str:
    """開課程頁，把播放器要的 master.m3u8 網址攔下來。

    監聽要在導頁前就掛上，不然播放器早在我們反應過來前就要走了。
    """
    page = navigator.page
    found: list[str] = []

    def on_request(request: Any) -> None:
        try:
            if ".m3u8" in request.url:
                found.append(request.url)
        except Exception:
            log.debug("處理 request 時出錯", exc_info=True)

    page.on("request", on_request)
    try:
        opened = navigator.open_session(url)
        if opened.get("missing"):
            # 空頁面等 45 秒也不會冒出串流，直接認賠下一場。
            raise DownloadError(
                "課程頁是空的（No session to display）—— 這個網址已失效或該場被撤下。"
                "到 AU 網站複製新網址後用 au2026rec url <課程代碼> <網址> 補上，"
                "或重跑 au2026rec catalog 更新對照表。"
            )
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and not found:
            time.sleep(1)
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            log.debug("移除 request 監聽失敗", exc_info=True)

    if not found:
        raise DownloadError(
            "等不到影片串流網址。可能是影片沒開始播（On-demand 需要自動播放旗標，"
            "用「開瀏覽器登入」時加 --autoplay），或這一場還沒開放隨選觀看。"
        )
    # master 通常是第一個被要的，但保險起見優先挑檔名就叫 master 的那支。
    master = next((u for u in found if "master.m3u8" in u), found[0])
    log.info("攔到串流：%s", master.split("?")[0])
    return master


# ── ffmpeg ──────────────────────────────────────────────────────────────

def _run_ffmpeg(command: list[str], *, what: str) -> None:
    log.info("ffmpeg 開始%s…", what)
    process = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if process.returncode != 0:
        tail = "\n".join((process.stderr or "").strip().splitlines()[-8:])
        raise DownloadError(f"ffmpeg {what}失敗（代碼 {process.returncode}）：\n{tail}")


def has_audio(path: Path, *, ffmpeg: str) -> bool:
    """檔案裡到底有沒有聲音。ffprobe 跟 ffmpeg 放在一起，找不到就當作有。"""
    probe = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
    if not probe.exists():
        return True
    try:
        output = subprocess.run(
            [str(probe), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, errors="replace", timeout=60,
        )
    except Exception:
        log.debug("ffprobe 驗音軌失敗", exc_info=True)
        return True
    return "audio" in (output.stdout or "")


def download_video(
    variant: hls.Variant,
    target: Path,
    *,
    ffmpeg: str,
    audio: hls.AudioTrack | None = None,
    referer: str = "",
    force: bool = False,
) -> Path:
    if target.exists() and target.stat().st_size > 0 and not force:
        log.info("影片已存在，跳過：%s", target.name)
        return target
    partial = target.with_suffix(target.suffix + ".part")
    command = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y"]
    if referer:
        command += ["-headers", f"Referer: {referer}\r\n"]
    command += ["-i", variant.url]
    if audio is not None:
        # 畫質那支清單是純影片，聲音要當第二個輸入另外餵進來，再明講怎麼配對。
        command += ["-i", audio.url, "-map", "0:v:0", "-map", "1:a:0"]
    # -c copy：不重新編碼，拿到的就是平台原本的串流，快而且不掉畫質。
    # 寫到 .part 再改名，所以要明講輸出格式 —— ffmpeg 靠副檔名猜，猜不到 .part。
    command += ["-c", "copy", "-bsf:a", "aac_adtstoasc",
                "-f", target.suffix.lstrip(".") or "mp4", str(partial)]
    _run_ffmpeg(command, what=f"下載影片（{variant.label()}）")

    # 沒聲音是這條路最容易犯、又完全不會報錯的失敗，所以改名前先驗一次。
    if not has_audio(partial, ffmpeg=ffmpeg):
        partial.unlink(missing_ok=True)
        raise DownloadError(
            "抓下來的影片沒有聲音軌，已刪除 —— 這一場的聲音清單可能長得不一樣，"
            "請回報這個訊息。"
        )
    partial.replace(target)  # 寫完才改成正式檔名，中斷時不會留下半截的假成品
    log.info("影片已存 %s（%.0f MB）", target.name, target.stat().st_size / 1024 / 1024)
    return target


def download_subtitle(
    track: hls.SubtitleTrack, target: Path, *, ffmpeg: str, force: bool = False
) -> Path | None:
    if target.exists() and target.stat().st_size > 0 and not force:
        log.info("字幕已存在，跳過：%s", target.name)
        return target
    try:
        _run_ffmpeg(
            [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
             "-i", track.url, "-c:s", "srt", str(target)],
            what="轉字幕",
        )
    except DownloadError as exc:
        log.warning("字幕抓不到，略過：%s", str(exc)[:160])
        return None
    log.info("字幕已存 %s", target.name)
    return target


# ── 一堂課 ──────────────────────────────────────────────────────────────

def download_session(
    navigator: Any, session: Any, settings: DownloadSettings
) -> dict[str, Any]:
    """抓齊一堂課：影片 + 字幕 + 附件，全部放進它自己的資料夾。"""
    ffmpeg = ensure_ffmpeg(settings.ffmpeg)
    folder = bundle.session_folder(settings.root, session.code, session.title)
    stem = bundle.safe_name(session.code or session.title, 60)
    result: dict[str, Any] = {"folder": str(folder), "video": "", "subtitles": [], "attachments": []}

    if not session.url:
        raise DownloadError(f"{session.label()} 沒有課程網址，無法下載")

    master_url = capture_master_url(
        navigator, session.url, wait_seconds=settings.manifest_wait_seconds
    )
    master = hls.parse_master(hls.fetch(master_url, referer=session.url), master_url)

    variant = hls.pick_variant(master, settings.height)
    video = download_video(
        variant, folder / f"{stem}.mp4",
        ffmpeg=ffmpeg, audio=hls.pick_audio(master, variant),
        referer=session.url, force=settings.force,
    )
    result["video"] = str(video)
    result["quality"] = variant.label()

    if settings.subtitles:
        track = hls.pick_subtitle(master)
        if track is None:
            log.info("這一場沒有官方字幕")
        else:
            saved = download_subtitle(
                track, folder / f"{stem}.{track.language}.srt", ffmpeg=ffmpeg, force=settings.force
            )
            if saved:
                result["subtitles"].append(str(saved))

    if settings.attachments:
        # 附件要在課程頁上點才拿得到網址，所以趁分頁還停在這一場的時候抓。
        saved_files = bundle.fetch_attachments(navigator.page, folder, force=settings.force)
        result["attachments"] = [str(p) for p in saved_files]

    bundle.write_session_info(folder, session, {
        "source": "download",
        "quality": result.get("quality", ""),
        "video": Path(result["video"]).name if result["video"] else "",
        "subtitles": [Path(p).name for p in result["subtitles"]],
        "attachments": [Path(p).name for p in result["attachments"]],
    })
    navigator.leave_session()
    return result
