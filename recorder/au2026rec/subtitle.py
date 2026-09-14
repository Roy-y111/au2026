"""錄好的課程影片 → 英文逐字稿（Groq Whisper）→ 繁體中文字幕（你自己的 agent）。

    影片 ──ffmpeg 切音軌──► Groq Whisper ──► <影片>.en.srt（原始證據，不再改動）
                                                  │
                        只把「文字」送去翻譯，時間軸留在本機
                                                  ▼
                        agy -p（模型由你指定）──► <影片>.zh-TW.srt

做法沿用 study「好學生筆記」的結構保留原則：

* 翻譯的 agent 永遠看不到時間軸，只拿到一串編號句子，回傳同樣數量的譯文；
  數量對不上就重試、再不行就拆小批重試，絕不「少一句也沒關係」。
* 組回字幕時用的是英文字幕原本的時間軸，最後逐條比對時間軸一個字元都沒變。
* 中途斷掉（網路、配額、關機）直接重跑同一個指令，已完成的切片與翻譯批次會跳過。

只用標準函式庫；需要本機裝好 ffmpeg / ffprobe，翻譯需要 Antigravity CLI（agy）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"
CHUNK_SECONDS = 600          # 10 分鐘一片；48 kbps 單聲道約 3.6 MB，遠低於 Groq 25 MB 上限
DEFAULT_MODEL = "gemini-3.8-flash-medium"
DEFAULT_TRANSLATOR = "agy"
BATCH_CUES = 40              # 一次送幾句去翻譯
BATCH_CHARS = 6000           # 單批英文字數上限（Windows 命令列長度有上限）
VIDEO_EXTS = {".mkv", ".mp4", ".mov", ".m4v", ".webm", ".flv", ".ts"}

# Whisper 在靜音、掌聲、換場時常「幻聽」出的固定句型（study 實戰累積）。
_HALLUCINATION = [re.compile(p, re.I) for p in (
    r"^subtitles? by\b", r"\bamara\.org\b", r"^thank you for watching\b",
    r"^thanks for watching\b", r"^please subscribe\b", r"^subscribe\b",
    r"^the end\.?$", r"^[\s.\-—…]*$", r"^\(.*(music|applause|laughter).*\)$",
    r"^\[.*(music|applause|laughter).*\]$", r"^key terms\b",
    r"^end of transcript\b", r"\btranscription by\b", r"\btranslation by\b",
    r"\bcastingwords\b", r"^captions? by\b",
)]

PROMPT = """你是專業的技術研討會字幕翻譯。以下是 Autodesk University 2026 課程「{title}」的英文字幕，已切成 {n} 句並編號。
{context}
請把每一句翻成台灣繁體中文字幕，規則：
1. 輸出「剛好 {n} 個字串」的 JSON 陣列，順序與輸入一一對應；不可合併、拆分、省略或新增句子。
2. 忠實翻譯，不摘要、不補充講者沒說的內容；語氣自然口語，適合當字幕閱讀。
3. 產品名、軟體介面名稱、指令、參數、檔案格式、縮寫與人名保留英文（例：Revit、Forma、MCP、Dynamo）。
4. 使用台灣業界慣用語與全形標點；不要加註解或括號說明。
5. 只輸出 JSON 陣列本身，不要任何其他文字。

輸入：
{payload}"""


class SubtitleError(RuntimeError):
    pass


@dataclass
class Cue:
    start: float
    end: float
    text: str


# ─── SRT 工具（純函式，方便測試） ───

def format_ts(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path: Path, entries: Iterable[tuple[str, str]]) -> None:
    """entries = [(timecode_line, text), ...]，timecode_line 形如 '00:00:01,000 --> 00:00:03,500'。"""
    blocks = [f"{i}\n{tc}\n{text}" for i, (tc, text) in enumerate(entries, 1)]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def parse_srt(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        if len(lines) >= 3 and "-->" in lines[1]:
            out.append((lines[1].strip(), " ".join(l.strip() for l in lines[2:]).strip()))
        elif len(lines) >= 2 and "-->" in lines[0]:
            out.append((lines[0].strip(), " ".join(l.strip() for l in lines[1:]).strip()))
    return out


def is_hallucination(text: str) -> bool:
    t = text.strip()
    return not t or any(p.search(t) for p in _HALLUCINATION)


def cues_from_segments(segments: list[dict], offset: float) -> list[Cue]:
    cues = []
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if is_hallucination(text):
            continue
        # 幾乎確定是靜音段的 Whisper 輸出（no_speech 高又信心低）也丟掉
        if seg.get("no_speech_prob", 0) > 0.9 and seg.get("avg_logprob", 0) < -1.0:
            continue
        start, end = float(seg["start"]) + offset, float(seg["end"]) + offset
        if end <= start:
            end = start + 0.5
        cues.append(Cue(start, end, text))
    return cues


def make_batches(texts: list[str], max_cues: int = BATCH_CUES,
                 max_chars: int = BATCH_CHARS) -> list[list[int]]:
    """把句子索引切成批次，每批不超過句數與字數上限。"""
    batches: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for i, t in enumerate(texts):
        if cur and (len(cur) >= max_cues or size + len(t) > max_chars):
            batches.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += len(t)
    if cur:
        batches.append(cur)
    return batches


def extract_json_array(raw: str) -> list:
    t = raw.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    i, j = t.find("["), t.rfind("]")
    if i == -1 or j <= i:
        raise ValueError("回覆裡找不到 JSON 陣列")
    return json.loads(t[i:j + 1])


def assemble_and_verify(en_entries: list[tuple[str, str]], zh: list[str]) -> list[tuple[str, str]]:
    """用英文字幕的原時間軸組出中文字幕，並驗證句數與時間軸完全一致。"""
    if len(zh) != len(en_entries):
        raise SubtitleError(f"譯文 {len(zh)} 句 ≠ 英文 {len(en_entries)} 句，拒絕輸出")
    if any(not s or not s.strip() for s in zh):
        raise SubtitleError("有空白譯文，拒絕輸出")
    out = [(tc, s.strip()) for (tc, _), s in zip(en_entries, zh)]
    for (tc_en, _), (tc_zh, _) in zip(en_entries, out):
        if tc_en != tc_zh:
            raise SubtitleError(f"時間軸不一致：{tc_en} ≠ {tc_zh}")
    return out


# ─── ffmpeg / Groq ───

def _need(tool: str, hint: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise SubtitleError(f"找不到 {tool}。{hint}")
    return path


def media_duration(video: Path) -> float:
    ffprobe = _need("ffprobe", "請先安裝 ffmpeg（Windows：winget install Gyan.FFmpeg，裝完重開終端機）")
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(video)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError as exc:
        raise SubtitleError(f"讀不出影片長度：{video}（{r.stderr.strip()[:200]}）") from exc


def extract_chunk(video: Path, start: float, out: Path) -> None:
    ffmpeg = _need("ffmpeg", "請先安裝 ffmpeg（Windows：winget install Gyan.FFmpeg，裝完重開終端機）")
    tmp = out.with_suffix(".part.mp3")
    r = subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{start}", "-t", f"{CHUNK_SECONDS}",
                        "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists():
        raise SubtitleError(f"ffmpeg 切音軌失敗：{r.stderr.strip()[:300]}")
    tmp.replace(out)


def _ssl_context():
    import ssl
    try:
        import certifi  # 有裝就用它的憑證包（某些 macOS Python 沒有系統憑證）
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _groq_via_curl(audio: Path, api_key: str, fields: dict) -> dict:
    """Python 的 SSL 憑證有問題時，改用系統內建的 curl（Windows 10+/macOS 都有）。"""
    curl = shutil.which("curl")
    if not curl:
        raise SubtitleError("SSL 憑證驗證失敗，且系統沒有 curl 可以替代")
    cmd = [curl, "-sS", "--fail-with-body", "-m", "300", GROQ_URL,
           "-H", f"Authorization: Bearer {api_key}", "-F", f"file=@{audio}"]
    for k, v in fields.items():
        cmd += ["-F", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SubtitleError(f"Groq（curl）失敗：{(r.stdout or r.stderr)[:300]}")
    return json.loads(r.stdout)


def groq_transcribe(audio: Path, api_key: str, language: str = "en",
                    log: Callable[[str], None] = print) -> dict:
    import ssl
    boundary = uuid.uuid4().hex
    fields = {"model": GROQ_MODEL, "response_format": "verbose_json",
              "language": language, "temperature": "0"}
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{audio.name}\"\r\nContent-Type: audio/mpeg\r\n\r\n").encode()
    body += audio.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": f"multipart/form-data; boundary={boundary}",
               "User-Agent": "au2026rec-srt"}
    for attempt in range(1, 7):
        req = urllib.request.Request(GROQ_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300, context=_ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 401:
                raise SubtitleError("Groq 金鑰無效（401），請確認 GROQ_API_KEY") from exc
            if exc.code in (429, 500, 502, 503, 504):
                wait = int(exc.headers.get("retry-after") or 0) or 20 * attempt
                log(f"    Groq {exc.code}，{wait} 秒後重試（第 {attempt} 次）")
                time.sleep(wait)
                continue
            raise SubtitleError(f"Groq 錯誤 {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if isinstance(getattr(exc, "reason", None), ssl.SSLError):
                log("    Python 的 SSL 憑證不齊，改用系統 curl 傳送")
                return _groq_via_curl(audio, api_key, fields)
            log(f"    連線問題（{exc}），20 秒後重試（第 {attempt} 次）")
            time.sleep(20)
    raise SubtitleError("Groq 連續失敗 6 次，稍後重跑同一個指令即可從這裡接續")


# ─── 翻譯（agent 只看文字） ───

def find_translator(translator: str) -> str:
    exe = shutil.which(translator) or (shutil.which("antigravity") if translator == "agy" else None)
    if not exe:
        raise SubtitleError(
            f"找不到翻譯用的 agent 指令「{translator}」。請先安裝並登入 Antigravity CLI（agy），"
            "或用 --translator 指定你自己的指令。")
    return exe


def call_agent(prompt: str, translator: str, model: str) -> str:
    exe = find_translator(translator)
    r = subprocess.run([exe, "-p", prompt, "--model", model, "--print-timeout", "10m"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=660)
    if r.returncode != 0:
        raise SubtitleError(f"{translator} 回傳錯誤碼 {r.returncode}：{(r.stderr or r.stdout)[:300]}")
    return r.stdout


def translate_batch(texts: list[str], title: str, context: str, translator: str,
                    model: str, log: Callable[[str], None],
                    agent: Callable[[str, str, str], str] = call_agent) -> list[str]:
    ctx = f"本場背景與專有名詞（照原字串保留）：{context}\n" if context else ""
    payload = json.dumps([{"n": i + 1, "en": t} for i, t in enumerate(texts)], ensure_ascii=False)
    prompt = PROMPT.format(title=title, n=len(texts), context=ctx, payload=payload)
    for attempt in range(1, 4):
        try:
            out = extract_json_array(agent(prompt, translator, model))
        except (ValueError, SubtitleError, subprocess.TimeoutExpired) as exc:
            log(f"    翻譯回覆無法解析（{exc}），重試 {attempt}/3")
            time.sleep(5)
            continue
        out = [o.get("zh", "") if isinstance(o, dict) else o for o in out]
        if len(out) == len(texts) and all(isinstance(x, str) and x.strip() for x in out):
            return [x.strip() for x in out]
        log(f"    句數對不上（要 {len(texts)} 句、回 {len(out)} 句），重試 {attempt}/3")
    if len(texts) > 1:
        mid = len(texts) // 2
        log(f"    改拆成 {mid} + {len(texts) - mid} 句兩小批重試")
        return (translate_batch(texts[:mid], title, context, translator, model, log, agent)
                + translate_batch(texts[mid:], title, context, translator, model, log, agent))
    raise SubtitleError("單句翻譯連續失敗，稍後重跑同一個指令即可從這裡接續")


# ─── 主流程 ───

@dataclass
class SrtOptions:
    groq_key: str
    model: str = DEFAULT_MODEL
    translator: str = DEFAULT_TRANSLATOR
    context: str = ""
    language: str = "en"
    translate: bool = True
    force: bool = False


def find_videos(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            found += sorted(f for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTS)
        elif p.is_file():
            found.append(p)
        else:
            raise SubtitleError(f"找不到檔案或資料夾：{p}")
    return found


def title_from_filename(video: Path) -> str:
    # 錄影工具的檔名像 20260916_0000_KEY1001-D_Day 1 Keynote → 取代碼與課名
    parts = video.stem.split("_", 2)
    return parts[2].replace("_", " ") if len(parts) == 3 and parts[0].isdigit() else video.stem


def transcribe_video(video: Path, opts: SrtOptions, log: Callable[[str], None] = print) -> Path:
    en_srt = video.with_name(video.stem + ".en.srt")
    if en_srt.exists() and not opts.force:
        log(f"  英文字幕已存在，沿用：{en_srt.name}")
        return en_srt
    work = video.with_name(video.stem + ".srt-work")
    work.mkdir(exist_ok=True)
    duration = media_duration(video)
    n = max(1, int((duration + CHUNK_SECONDS - 1) // CHUNK_SECONDS))
    log(f"  影片長 {duration / 60:.1f} 分鐘，切成 {n} 段送 Groq")
    cues: list[Cue] = []
    for i in range(n):
        cache = work / f"groq_{i:03d}.json"
        if not cache.exists():
            audio = work / f"chunk_{i:03d}.mp3"
            if not audio.exists():
                extract_chunk(video, i * CHUNK_SECONDS, audio)
            log(f"  轉錄第 {i + 1}/{n} 段…")
            result = groq_transcribe(audio, opts.groq_key, opts.language, log)
            cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            audio.unlink(missing_ok=True)
        result = json.loads(cache.read_text(encoding="utf-8"))
        cues += cues_from_segments(result.get("segments") or [], i * CHUNK_SECONDS)
    if not cues:
        raise SubtitleError("整部影片沒有轉出任何語音，請確認影片有聲音")
    write_srt(en_srt, [(f"{format_ts(c.start)} --> {format_ts(c.end)}", c.text) for c in cues])
    log(f"  ✓ 英文字幕 {len(cues)} 句 → {en_srt.name}")
    return en_srt


def translate_srt(video: Path, en_srt: Path, opts: SrtOptions,
                  log: Callable[[str], None] = print) -> Path:
    zh_srt = video.with_name(video.stem + ".zh-TW.srt")
    if zh_srt.exists() and not opts.force:
        log(f"  中文字幕已存在，略過：{zh_srt.name}")
        return zh_srt
    find_translator(opts.translator)  # 沒裝 agent 就立刻停，不要一路重試
    entries = parse_srt(en_srt.read_text(encoding="utf-8"))
    texts = [t for _, t in entries]
    work = video.with_name(video.stem + ".srt-work") / f"zh_{opts.model}"
    work.mkdir(parents=True, exist_ok=True)
    batches = make_batches(texts)
    title = title_from_filename(video)
    log(f"  翻譯 {len(texts)} 句，分 {len(batches)} 批（{opts.translator} / {opts.model}）")
    zh: list[str] = []
    for b, idx in enumerate(batches):
        part = work / f"batch_{b:03d}.json"
        if part.exists():
            got = json.loads(part.read_text(encoding="utf-8"))
            if len(got) == len(idx):
                zh += got
                continue
        log(f"  第 {b + 1}/{len(batches)} 批…")
        got = translate_batch([texts[i] for i in idx], title, opts.context,
                              opts.translator, opts.model, log)
        part.write_text(json.dumps(got, ensure_ascii=False), encoding="utf-8")
        zh += got
    write_srt(zh_srt, assemble_and_verify(entries, zh))
    # 寫完再讀回來比對一次，確保檔案本身句數與時間軸都正確
    back = parse_srt(zh_srt.read_text(encoding="utf-8"))
    if [tc for tc, _ in back] != [tc for tc, _ in entries]:
        raise SubtitleError("寫出的中文字幕讀回後時間軸不一致，已保留英文字幕，請回報")
    log(f"  ✓ 中文字幕 {len(back)} 句（時間軸與英文逐條一致）→ {zh_srt.name}")
    return zh_srt


def run(paths: list[str], opts: SrtOptions, log: Callable[[str], None] = print) -> int:
    videos = find_videos(paths)
    if not videos:
        raise SubtitleError("資料夾裡沒有影片檔（支援 " + "、".join(sorted(VIDEO_EXTS)) + "）")
    failed = 0
    for n, video in enumerate(videos, 1):
        log(f"\n[{n}/{len(videos)}] {video.name}")
        try:
            en_srt = transcribe_video(video, opts, log)
            if opts.translate:
                translate_srt(video, en_srt, opts, log)
        except SubtitleError as exc:
            failed += 1
            log(f"  ✗ {exc}")
    log(f"\n完成 {len(videos) - failed}/{len(videos)} 部" + ("" if not failed else "；失敗的直接重跑同一個指令會從中斷處接續"))
    return 1 if failed else 0


def resolve_groq_key(explicit: str | None) -> str:
    key = explicit or os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise SubtitleError(
            "沒有 Groq 金鑰。到 https://console.groq.com/keys 免費申請，然後設成環境變數：\n"
            "    Windows：setx GROQ_API_KEY \"gsk_你的金鑰\"（設完重開終端機）\n"
            "    macOS / Linux：export GROQ_API_KEY=gsk_你的金鑰\n"
            "  或執行時加 --groq-key gsk_你的金鑰")
    return key


if __name__ == "__main__":  # pragma: no cover
    sys.exit("請用 au2026rec srt <影片或資料夾>")
