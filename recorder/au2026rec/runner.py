"""排程執行：等時間 → 開課程頁 → OBS 開錄 → 到點停錄 → 下一場。"""
from __future__ import annotations

import csv
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from au2026rec.browser import BrowserError, Navigator
from au2026rec.obs import ObsController, ObsError
from au2026rec.plan import STATUS_SKIPPED, PlanItem, format_delta

log = logging.getLogger("au2026rec")

REPORT_FIELDS = [
    "index", "code", "title", "mode",
    "planned_start_local", "planned_end_local",
    "recorded_start", "recorded_stop",
    "result", "output_path", "played", "playing", "note", "url",
]


class Interrupted(RuntimeError):
    """使用者按了 Ctrl-C。"""


@dataclass
class RunOptions:
    gap_seconds: int = 15
    scene: str = ""
    local_tz: Any = None
    report_file: Path | None = None
    countdown_every: int = 30
    skip_past: bool = True
    # 錄完把檔案收進「每堂課一個資料夾」，順便抓附件。留 None = 維持舊行為。
    library_root: Path | None = None
    fetch_attachments: bool = True
    # 錄影期間每隔幾秒確認影片真的還在播。0 = 不監看（回到只睡到結束的舊行為）。
    watch_every: int = 30
    watch_max_reloads: int = 3


class _StopFlag:
    def __init__(self) -> None:
        self.raised = False

    def install(self) -> None:
        def handler(_signum: int, _frame: object) -> None:
            if self.raised:  # 第二次 Ctrl-C 直接砍
                raise KeyboardInterrupt
            self.raised = True
            log.warning("收到中斷訊號，會先把目前這場收尾（再按一次 Ctrl-C 立即中止）")

        try:
            signal.signal(signal.SIGINT, handler)
        except (ValueError, OSError):  # pragma: no cover - 非主執行緒
            log.debug("無法安裝 SIGINT handler")

    def check(self) -> None:
        if self.raised:
            raise Interrupted


def setup_logging(log_file: Path | None, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("obsws_python").setLevel(logging.WARNING)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sleep_until(target: datetime, stop: _StopFlag, *, label: str, countdown_every: int = 30) -> None:
    """等到 target。每 countdown_every 秒印一次剩餘時間，可被 Ctrl-C 打斷。"""
    next_report = 0.0
    while True:
        stop.check()
        remaining = (target - now_utc()).total_seconds()
        if remaining <= 0:
            return
        if countdown_every and remaining >= 5 and time.monotonic() >= next_report:
            log.info("%s：還有 %s", label, format_delta(timedelta(seconds=int(remaining))))
            next_report = time.monotonic() + max(countdown_every, 5)
        time.sleep(min(1.0, max(remaining, 0.05)))


class Runner:
    def __init__(
        self,
        browser: Navigator,
        obs: ObsController,
        options: RunOptions,
    ) -> None:
        self.browser = browser
        self.obs = obs
        self.options = options
        self._stop = _StopFlag()
        self._rows: list[dict[str, object]] = []

    # ── 主流程 ──────────────────────────────────────────────────────────
    def run(self, items: Sequence[PlanItem]) -> list[dict[str, object]]:
        todo = [item for item in items if item.status != STATUS_SKIPPED]
        for item in items:
            if item.status == STATUS_SKIPPED:
                self._record_row(item, result="skipped", note=item.note)

        if self.options.skip_past:
            fresh = [item for item in todo if item.stop_at > now_utc()]
            for item in todo:
                if item not in fresh:
                    log.warning("略過已結束的場次：%s", item.session.label())
                    self._record_row(item, result="past", note="執行時該場次已結束")
            todo = fresh

        if not todo:
            log.warning("沒有待錄的場次（都結束了或都被跳過）")
            return self._rows

        now = now_utc()
        ongoing = [i for i in todo if i.open_at <= now < i.stop_at]
        if ongoing:
            cur = ongoing[0]
            log.warning(
                "現在正在進行中：%s（%s–%s）—— 會立刻跳轉並接手錄影",
                cur.session.label(), self._fmt(cur.start), self._fmt(cur.end),
            )
        log.info("待錄 %d 場，第一場 %s 開始", len(todo), self._fmt(todo[0].start))
        self._stop.install()
        try:
            for position, item in enumerate(todo, start=1):
                log.info("── [%d/%d] %s ──", position, len(todo), item.session.label())
                nxt = todo[position] if position < len(todo) else None
                self._run_one(item, nxt.open_at if nxt else None)
        except Interrupted:
            log.warning("已中斷排程，尚未執行的場次不會錄")
        finally:
            self._flush_report()
        return self._rows

    def _run_one(self, item: PlanItem, next_open_at: datetime | None = None) -> None:
        session = item.session
        row: dict[str, object] = {}

        sleep_until(
            item.open_at,
            self._stop,
            label=f"等 {session.code or session.title} 開場",
            countdown_every=self.options.countdown_every,
        )

        if not session.url:
            # 沒網址不代表這場要放棄：畫面照錄，使用者自己把頁面開起來就救得回來。
            log.error(
                "%s 查不到課程網址，不會自動導頁 —— 請自己在瀏覽器開好那一頁。"
                "補網址：au2026rec url %s <網址>",
                session.label(),
                session.code or "CODE",
            )
            row["played"] = False
            opened = {"played": False, "note": "缺少課程網址，未導頁"}
        else:
            try:
                opened = self.browser.open_session(session.url)
            except BrowserError as exc:
                # 導頁失敗也照錄：你手動把頁面開起來，這場還是救得回來。
                log.error("開課程頁失敗，仍會照時間錄影：%s", exc)
                opened = {"played": False, "note": f"導頁失敗：{exc}"}
            except Exception as exc:  # 網頁千奇百怪，不要因為一場毀掉整晚
                log.exception("開課程頁時發生未預期錯誤，仍會照時間錄影：%s", exc)
                opened = {"played": False, "note": f"導頁錯誤：{exc}"}

        row["played"] = opened.get("played")
        row["playing"] = opened.get("playing")
        if opened.get("note"):
            item.note = (item.note + "；" if item.note else "") + str(opened["note"])
        if not opened.get("played"):
            log.warning(
                "沒有自動播放，仍會照時間錄影（畫面可能停在課程頁）。"
                "現在手動按播放還來得及；事後可用 au2026rec probe 補 play_selectors"
            )

        sleep_until(item.start, self._stop, label="等課程開始", countdown_every=0)

        started_at: datetime | None = None
        output_path: str | None = None
        result = "recorded"
        note = item.note
        try:
            if self.options.scene or session.scene:
                self.obs.switch_scene(session.scene or self.options.scene)
            how = self.obs.start_recording(item.output_name)
            if how == "continued":
                result = "recorded-continued"
                note = (note + "；" if note else "") + "接續 OBS 既有的錄影（中途重啟）"
            started_at = now_utc()
        except ObsError as exc:
            log.error("OBS 開始錄影失敗：%s", exc)
            self._record_row(item, result="obs-error", note=str(exc), extra=row)
            self.browser.leave_session()
            return

        try:
            self._record_until(item)
        except Interrupted:
            result = "interrupted"
            note = (note + "；" if note else "") + "使用者中斷，已提前收尾"
            self._stop.raised = False  # 讓收尾流程跑完
            raise_after = True
        else:
            raise_after = False

        try:
            output_path = self.obs.stop_recording()
        except ObsError as exc:
            log.error("OBS 停止錄影失敗：%s", exc)
            result = "obs-stop-error"
            note = (note + "；" if note else "") + str(exc)

        filed = self._file_into_library(item, output_path, next_open_at)
        if filed:
            output_path = str(filed)
        self.browser.leave_session()
        self._record_row(
            item,
            result=result,
            note=note,
            output_path=output_path,
            started_at=started_at,
            stopped_at=now_utc(),
            extra=row,
        )
        log.info("完成 %s → %s", item.output_name, output_path or "（OBS 未回報路徑）")

        if raise_after:
            self._stop.raised = True
            raise Interrupted
        if self.options.gap_seconds:
            # 場間休息是給 OBS 喘口氣用的，但不能吃掉下一場的開頁時間 ——
            # 直播背靠背時，晚一秒開頁就少錄一秒。
            rest_until = now_utc() + timedelta(seconds=self.options.gap_seconds)
            if next_open_at is not None:
                rest_until = min(rest_until, next_open_at)
            sleep_until(rest_until, self._stop, label="場間休息", countdown_every=0)

    # ── 錄影期間的監看 ──────────────────────────────────────────────────
    def _record_until(self, item: PlanItem) -> None:
        """睡到這場結束，中間定期確認影片還在播，卡住就出手救。

        會需要這段，是因為畫質鎖死 1080p 之後 ABR 不能自己降階 —— 網路一抖
        不再是「畫質變差」，而是直接轉圈圈，而且錄下來才會發現。所以救援的
        第一步就是把鎖解掉，把降階的能力還給播放器。

        三段式，一段沒救起來才升到下一段：
          1. 第一次偵測到卡住 → 只記一筆，可能只是短暫緩衝
          2. 連續兩次 → 解鎖畫質回 auto
          3. 連續三次 → 重載頁面（重載後不再鎖畫質），最多幾次
        """
        label = "錄影中"
        every = self.options.watch_every
        page = getattr(self.browser, "_page", None)
        if not every or page is None:
            sleep_until(item.stop_at, self._stop, label=label,
                        countdown_every=self.options.countdown_every)
            return

        strikes = 0
        reloads = 0
        relaxed = False
        while True:
            self._stop.check()
            wake = min(now_utc() + timedelta(seconds=every), item.stop_at)
            sleep_until(wake, self._stop, label=label,
                        countdown_every=self.options.countdown_every)
            if now_utc() >= item.stop_at:
                return

            try:
                healthy, why = self.browser.verify_playing(seconds=5)
            except Exception as exc:  # 監看本身絕對不能把錄影搞掛
                log.debug("監看播放狀態時出錯：%s", exc, exc_info=True)
                continue

            if healthy:
                if strikes:
                    log.info("影片恢復正常播放")
                strikes = 0
                continue

            strikes += 1
            log.warning("影片看起來卡住了（第 %d 次）：%s", strikes, why)
            if strikes == 2 and not relaxed:
                relaxed = self.browser.relax_quality()
                if not relaxed:
                    strikes = 3  # 沒有畫質可解，直接跳到重載那一段
            if strikes >= 3:
                if not item.session.url:
                    # 沒網址就是使用者自己開的頁面，重載會把他開的東西弄掉。
                    log.error("影片持續卡住，但這場沒有網址可重載 —— 請手動處理瀏覽器")
                    strikes = 0
                    continue
                if reloads >= self.options.watch_max_reloads:
                    log.error(
                        "重載 %d 次仍然卡住，不再嘗試 —— 錄影繼續，但畫面可能是轉圈圈。"
                        "現在手動處理那個瀏覽器還來得及。", reloads,
                    )
                    strikes = 0  # 別再反覆重載，交給人
                    continue
                reloads += 1
                log.warning("重新載入課程頁（第 %d 次）", reloads)
                try:
                    # 重載後不鎖畫質：卡住的原因很可能就是它。
                    self.browser.open_session(item.session.url, lock_quality=False)
                except Exception as exc:
                    log.error("重載失敗，錄影繼續：%s", str(exc)[:120])
                strikes = 0

    # ── 課程資料夾 ──────────────────────────────────────────────────────
    def _file_into_library(
        self, item: PlanItem, output_path: str | None, next_open_at: datetime | None = None
    ) -> Path | None:
        """把這一場的錄影搬進它自己的資料夾，順便抓附件、寫 session.json。

        搬檔失敗不該影響下一場 —— 錄影本體已經在 OBS 的資料夾裡了，最壞的情況
        只是沒歸檔，人工搬也來得及。
        """
        root = self.options.library_root
        if root is None:
            return None
        from au2026rec import bundle  # 延後匯入：沒開這功能時不必要的相依

        session = item.session
        try:
            folder = bundle.session_folder(root, session.code, session.title)
        except OSError as exc:
            log.warning("建不出課程資料夾，錄影留在原處：%s", exc)
            return None

        moved: Path | None = None
        if output_path:
            moved = self._move_recording(Path(output_path), folder)

        attachments: list[Path] = []
        page = getattr(self.browser, "_page", None)
        if self.options.fetch_attachments and page is not None:
            # 抓附件要在課程頁上點按鈕，會花十幾秒。直播背靠背時這段時間比附件值錢 ——
            # 下一場快開了就先放掉，事後用 download 指令補抓。
            spare = (next_open_at - now_utc()).total_seconds() if next_open_at else None
            if spare is not None and spare < 120:
                log.info("下一場快開了（剩 %.0f 秒），這場的附件留到事後再抓", spare)
            else:
                attachments = bundle.fetch_attachments(page, folder)

        try:
            bundle.write_session_info(folder, session, {
                "source": "recording",
                "video": (moved or Path(output_path or "")).name if (moved or output_path) else "",
                "attachments": [p.name for p in attachments],
            })
        except OSError as exc:
            log.warning("寫不出 session.json：%s", exc)
        return moved

    @staticmethod
    def _move_recording(source: Path, folder: Path) -> Path | None:
        """等 OBS 把檔尾寫完再搬。太早搬會拿到還在寫入的檔案。"""
        import shutil

        for _ in range(30):
            if source.exists() and source.stat().st_size > 0:
                break
            time.sleep(1)
        if not source.exists():
            log.warning("找不到錄影檔 %s，略過歸檔", source)
            return None
        previous = -1
        for _ in range(30):  # 檔案大小連續兩次一樣才算寫完
            size = source.stat().st_size
            if size == previous:
                break
            previous = size
            time.sleep(1)

        target = folder / source.name
        if target.exists():
            target = folder / f"{source.stem}_{int(time.time())}{source.suffix}"
        try:
            shutil.move(str(source), str(target))
        except OSError as exc:
            log.warning("搬不動錄影檔（可能還被 OBS 佔用），留在 %s：%s", source, exc)
            return None
        log.info("錄影已歸檔：%s", target)
        return target

    # ── 報告 ────────────────────────────────────────────────────────────
    def _fmt(self, moment: datetime) -> str:
        tz = self.options.local_tz
        return moment.astimezone(tz).strftime("%m/%d %H:%M") if tz else moment.isoformat()

    def _record_row(
        self,
        item: PlanItem,
        *,
        result: str,
        note: str = "",
        output_path: str | None = None,
        started_at: datetime | None = None,
        stopped_at: datetime | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        row: dict[str, object] = {
            "index": item.index,
            "code": item.session.code,
            "title": item.session.title,
            "mode": item.session.mode,
            "planned_start_local": self._fmt(item.start),
            "planned_end_local": self._fmt(item.end),
            "recorded_start": self._fmt(started_at) if started_at else "",
            "recorded_stop": self._fmt(stopped_at) if stopped_at else "",
            "result": result,
            "output_path": output_path or "",
            "played": "",
            "playing": "",
            "note": note,
            "url": item.session.url or "",
        }
        row.update(extra or {})
        self._rows.append(row)
        self._flush_report()

    def _flush_report(self) -> None:
        target = self.options.report_file
        if target is None or not self._rows:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self._rows)
