"""HLS master playlist 解析：挑畫質、找字幕。

AU 的播放器是 Brightcove，影片走 HLS。master.m3u8 裡列了六種畫質與一軌
官方 WebVTT 字幕，網址帶 Fastly 的簽章 token（有時效，所以抓到就要馬上用）。

這裡刻意自己解析而不是用 ffmpeg 的 `-map p:N` —— 節目編號的順序沒有保證，
寫死編號哪天就抓到錯的畫質了；照 RESOLUTION 挑才穩。
"""
from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


class HlsError(RuntimeError):
    pass


@dataclass
class Variant:
    url: str
    width: int = 0
    height: int = 0
    bandwidth: int = 0
    # 影片與聲音是分開的兩支播放清單，這裡記的是它要配哪一組聲音。
    audio_group: str = ""

    def label(self) -> str:
        return f"{self.width}x{self.height} {self.bandwidth / 1000:.0f} kbps"


@dataclass
class AudioTrack:
    url: str
    group: str = ""
    language: str = "en"
    name: str = ""


@dataclass
class SubtitleTrack:
    url: str
    language: str = "en"
    name: str = ""


@dataclass
class Master:
    url: str
    variants: list[Variant]
    subtitles: list[SubtitleTrack]
    audio: list[AudioTrack] = field(default_factory=list)


def _attributes(line: str) -> dict[str, str]:
    body = line.split(":", 1)[1] if ":" in line else ""
    return {k: v.strip('"') for k, v in _ATTR.findall(body)}


def _absolute(base: str, target: str) -> str:
    """把相對 URI 接成完整網址，並且把 master 的簽章 query 帶過去。

    Fastly 的 token 掛在 query string 上，相對 URI 不會自己帶 —— 漏掉就 403。
    """
    joined = urllib.parse.urljoin(base, target)
    base_query = urllib.parse.urlsplit(base).query
    if base_query and not urllib.parse.urlsplit(joined).query:
        joined = f"{joined}?{base_query}"
    return joined


def fetch(url: str, *, referer: str = "", timeout: int = 30) -> str:
    headers = {"User-Agent": "Mozilla/5.0 au2026rec"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise HlsError(f"讀不到播放清單：{str(exc)[:120]}") from exc


def parse_master(text: str, url: str) -> Master:
    variants: list[Variant] = []
    subtitles: list[SubtitleTrack] = []
    audio: list[AudioTrack] = []
    lines = [line.strip() for line in text.splitlines()]

    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _attributes(line)
            target = next((l for l in lines[index + 1:] if l and not l.startswith("#")), "")
            if not target:
                continue
            width = height = 0
            if "x" in attrs.get("RESOLUTION", ""):
                raw_w, _, raw_h = attrs["RESOLUTION"].partition("x")
                width, height = int(raw_w or 0), int(raw_h or 0)
            variants.append(Variant(
                url=_absolute(url, target),
                width=width,
                height=height,
                bandwidth=int(attrs.get("BANDWIDTH") or 0),
                audio_group=attrs.get("AUDIO", ""),
            ))
        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = _attributes(line)
            if not attrs.get("URI"):
                continue
            if attrs.get("TYPE") == "SUBTITLES":
                subtitles.append(SubtitleTrack(
                    url=_absolute(url, attrs["URI"]),
                    language=attrs.get("LANGUAGE") or "en",
                    name=attrs.get("NAME") or "",
                ))
            elif attrs.get("TYPE") == "AUDIO":
                audio.append(AudioTrack(
                    url=_absolute(url, attrs["URI"]),
                    group=attrs.get("GROUP-ID", ""),
                    language=attrs.get("LANGUAGE") or "en",
                    name=attrs.get("NAME") or "",
                ))

    if not variants:
        raise HlsError("播放清單裡沒有任何畫質 —— 可能抓到的不是 master.m3u8")
    return Master(url=url, variants=variants, subtitles=subtitles, audio=audio)


def pick_variant(master: Master, height: int) -> Variant:
    """挑最接近指定高度的畫質：優先不超過它的最高一檔。"""
    ranked = sorted(master.variants, key=lambda v: (v.height, v.bandwidth))
    at_or_below = [v for v in ranked if v.height and v.height <= height]
    chosen = at_or_below[-1] if at_or_below else ranked[0]
    log.info(
        "畫質：選 %s（目標 %dp，可選 %s）",
        chosen.label(), height,
        "、".join(f"{v.height}p" for v in ranked if v.height) or "?",
    )
    return chosen


def pick_subtitle(master: Master, language: str = "en") -> SubtitleTrack | None:
    if not master.subtitles:
        return None
    exact = [t for t in master.subtitles if t.language.lower().startswith(language.lower())]
    return (exact or master.subtitles)[0]


def pick_audio(master: Master, variant: Variant) -> AudioTrack | None:
    """找這個畫質要配的聲音軌。

    AU 的 master 把影片與聲音拆成兩支清單，畫質那一支是**純影片**。只餵畫質
    網址給 ffmpeg 會抓到一個沒有聲音的檔，而且不會有任何錯誤訊息。
    """
    if not master.audio:
        return None  # 音訊混在影片裡（有些來源是這樣），不用另外配
    same = [a for a in master.audio if a.group and a.group == variant.audio_group]
    chosen = (same or master.audio)[0]
    log.info("聲音：另外配 %s（%s）", chosen.name or chosen.language, chosen.group or "無群組")
    return chosen
