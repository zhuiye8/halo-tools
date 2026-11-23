"""Lyrics models, LRC parsing and the NetEase (anonymous) provider.

The NetEase endpoints used here need no login cookie:

* search : ``GET https://music.163.com/api/search/get/web?s=<q>&type=1``
* lyric  : ``GET https://music.163.com/api/song/lyric?id=<id>&lv=-1&tv=-1&kv=-1``

Lyrics are cached on disk so a replay does not hit the network.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_TAG = re.compile(r"^\[(ti|ar|al|by|offset):(.*)\]$", re.IGNORECASE)

# Lyric files usually start with credit lines such as "作词 : 唐恬".  They carry a
# timestamp but are not meant to be displayed, so they are filtered out.
_CREDIT = re.compile(
    r"^(作词|作曲|编曲|制作人|监制|出品|发行|统筹|企划|营销|混音|母带|录音|配唱|和声|"
    r"吉他|贝斯|鼓|键盘|弦乐|词|曲|OP|SP|制作|编配|演唱|原唱|翻唱|词曲|作词作曲)"
    r"\s*[:：]", re.IGNORECASE)


_CREDIT_WORDS = (
    "作词", "作曲", "编曲", "制作", "监制", "出品", "发行", "统筹", "企划", "营销",
    "混音", "母带", "录音", "配唱", "和声", "吉他", "贝斯", "鼓", "键盘", "弦乐",
    "词", "曲", "版权", "OP", "SP", "编配", "演唱", "原唱", "翻唱", "词曲",
)

# A short name followed by a colon is the usual duet/role marker in Chinese lyric
# files ("祝青：", "女：").  A latin handle containing digits ("G2er") is a credit,
# not a lyric line.
_NAME_TAG = re.compile(r"^.{1,12}[：:]\s*$")
_HANDLE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,14}$")


def _is_credit(text: str) -> bool:
    """True for the credit/metadata lines that lyric files put at 0:00."""
    head, sep, _ = text.partition("：") if "：" in text else text.partition(":")
    if sep and len(head) <= 10 and any(word in head for word in _CREDIT_WORDS):
        return True
    if "版权" in text and len(text) <= 40:
        return True
    if _NAME_TAG.match(text):
        return True
    if _HANDLE.match(text) and any(char.isdigit() for char in text):
        return True
    # "歌名 (电影《…》主题曲) - 歌手" style title lines.
    return bool(re.search(r"\(.*\)\s*-\s*\S+$", text))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@dataclass(frozen=True)
class LyricLine:
    """One synced lyric line."""

    start: float
    text: str
    end: float = 0.0
    translation: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Lyrics:
    """A parsed lyric document."""

    lines: list[LyricLine] = field(default_factory=list)
    title: str = ""
    artist: str = ""
    source: str = ""

    def __bool__(self) -> bool:
        return bool(self.lines)

    def with_ends(self, fallback_gap: float = 6.0) -> "Lyrics":
        """Fill each line's end with the next line's start when the source omits it."""
        ordered = sorted(self.lines, key=lambda line: line.start)
        filled: list[LyricLine] = []
        for index, line in enumerate(ordered):
            end = line.end
            if end <= line.start:
                if index + 1 < len(ordered):
                    end = ordered[index + 1].start
                else:
                    end = line.start + fallback_gap
            filled.append(LyricLine(line.start, line.text, end, line.translation))
        return Lyrics(filled, self.title, self.artist, self.source)

    def line_at(self, position: float) -> LyricLine | None:
        """Return the line covering ``position`` (or the last line that started)."""
        current: LyricLine | None = None
        for line in self.lines:
            if line.start <= position:
                current = line
            else:
                break
        return current


def _clean(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def parse_lrc(content: str, translation: str = "",
              drop_names: "tuple[str, ...] | list[str]" = ()) -> list[LyricLine]:
    """Parse LRC text; supports multiple time tags per line and ``[offset:]``.

    ``drop_names`` removes lines that are just a performer name (the API reports
    "祝青/灼夭", and the lyric body sometimes repeats those as standalone lines).
    """
    names = {name.strip() for name in drop_names if name and name.strip()}
    offset = 0.0
    entries: list[tuple[float, str]] = []
    for raw in content.splitlines():
        raw = raw.strip("\ufeff").strip()
        if not raw:
            continue
        meta = _META_TAG.match(raw)
        if meta:
            if meta.group(1).lower() == "offset":
                try:
                    offset = float(meta.group(2)) / 1000.0
                except ValueError:
                    log.debug("ignoring bad offset tag %r", raw)
            continue
        stamps = list(_TIME_TAG.finditer(raw))
        if not stamps:
            continue
        text = _clean(_TIME_TAG.sub("", raw))
        if not text or _is_credit(text) or text in names:
            continue
        for match in stamps:
            minutes, seconds = int(match.group(1)), int(match.group(2))
            fraction = match.group(3) or "0"
            millis = int(fraction.ljust(3, "0")[:3])
            entries.append((minutes * 60 + seconds + millis / 1000.0 + offset, text))
    entries.sort(key=lambda item: item[0])

    translated = {line.start: line.text for line in parse_lrc(translation)} if translation else {}
    lines = [LyricLine(start, text, 0.0, translated.get(start, "")) for start, text in entries]
    return lines


def _http_get(url: str, timeout: float = 12.0) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Referer": "https://music.163.com/"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


@dataclass
class SongMatch:
    song_id: int
    title: str
    artist: str
    duration_ms: int
    score: float = 0.0


def _similarity(a: str, b: str) -> float:
    a, b = _clean(a).lower(), _clean(b).lower()
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    common = set(a) & set(b)
    return len(common) / max(len(set(a)), len(set(b)))


def score_candidate(candidate: SongMatch, title: str, artist: str, duration: float) -> float:
    """Weighted match; mirrors the heuristics used by Lyricify for robustness."""
    score = _similarity(candidate.title, title) * 1.0
    score += _similarity(candidate.artist, artist) * 0.6
    if duration > 0 and candidate.duration_ms > 0:
        delta = abs(candidate.duration_ms / 1000.0 - duration)
        score += max(0.0, 1.0 - delta / 8.0) * 0.8
    return score


class NeteaseClient:
    """Anonymous NetEase lyric client with an on-disk cache."""

    def __init__(self, cache_dir: Path, timeout: float = 12.0) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    # ------------------------------------------------------------------ search
    def search(self, title: str, artist: str = "", limit: int = 8) -> list[SongMatch]:
        query = " ".join(part for part in (title, artist) if part)
        url = ("https://music.163.com/api/search/get/web?s="
               f"{urllib.parse.quote(query)}&type=1&limit={limit}&offset=0")
        data = _http_get(url, self.timeout)
        songs = ((data.get("result") or {}).get("songs") or [])
        matches = []
        for song in songs:
            artists = "/".join(item.get("name", "") for item in song.get("artists", []))
            matches.append(SongMatch(
                song_id=int(song["id"]), title=song.get("name", ""), artist=artists,
                duration_ms=int(song.get("duration", 0))))
        log.debug("search %r -> %d candidates", query, len(matches))
        return matches

    def best_match(self, title: str, artist: str, duration: float = 0.0) -> SongMatch | None:
        candidates = self.search(title, artist)
        if not candidates:
            return None
        for candidate in candidates:
            candidate.score = score_candidate(candidate, title, artist, duration)
        candidates.sort(key=lambda item: item.score, reverse=True)
        best = candidates[0]
        log.info("lyric match: %r/%r score=%.2f (id=%d)", best.title, best.artist,
                 best.score, best.song_id)
        return best if best.score >= 0.6 else None

    # ------------------------------------------------------------------- lyric
    def fetch(self, match: SongMatch) -> Lyrics:
        cached = self._cache_path(match.song_id)
        if cached.exists():
            try:
                payload = json.loads(cached.read_text(encoding="utf-8"))
                return self._to_lyrics(payload, match, from_cache=True)
            except (ValueError, KeyError):
                log.warning("discarding corrupt lyric cache %s", cached)
        url = ("https://music.163.com/api/song/lyric?"
               f"id={match.song_id}&lv=-1&tv=-1&kv=-1")
        payload = _http_get(url, self.timeout)
        cached.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return self._to_lyrics(payload, match, from_cache=False)

    def _to_lyrics(self, payload: dict, match: SongMatch, *, from_cache: bool) -> Lyrics:
        lrc = ((payload.get("lrc") or {}).get("lyric") or "")
        translation = ((payload.get("tlyric") or {}).get("lyric") or "")
        names = tuple(part for part in re.split(r"[/、,&\s]+", match.artist) if part)
        lines = parse_lrc(lrc, translation, names)
        lyrics = Lyrics(lines, match.title, match.artist,
                        source="cache" if from_cache else "netease").with_ends()
        log.info("lyrics %r: %d lines (%s)", match.title, len(lyrics.lines), lyrics.source)
        return lyrics

    def lyrics_for(self, title: str, artist: str, duration: float = 0.0,
                   song_id: str = "") -> Lyrics:
        """Resolve lyrics, preferring an exact song id when the player provides one."""
        if song_id and song_id.isdigit():
            try:
                lyrics = self.fetch(SongMatch(int(song_id), title, artist, int(duration * 1000)))
                if lyrics:
                    return lyrics
            except (OSError, ValueError) as exc:
                log.warning("lyric fetch by id %s failed: %s", song_id, exc)
        match = self.best_match(title, artist, duration)
        if match is None:
            return Lyrics(title=title, artist=artist, source="netease-miss")
        try:
            return self.fetch(match)
        except (OSError, ValueError) as exc:
            log.warning("lyric fetch failed: %s", exc)
            return Lyrics(title=title, artist=artist, source="netease-error")

    def _cache_path(self, song_id: int) -> Path:
        return self.cache_dir / f"{song_id}.json"