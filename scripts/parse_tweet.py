"""Extract tweet text and outbound links from a saved X/Twitter status page.

The page embeds the tweet JSON in script tags; this parses it offline so the agent
can read a thread without a browser.
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

URL_FEED_RE = re.compile(r'https?://[^\s"\'<>\\]+')


def load(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def extract_full_texts(text: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r'"full_text"\s*:\s*"((?:[^"\\]|\\.)*)"', text):
        raw = match.group(1)
        try:
            value = json.loads(f'"{raw}"')
        except ValueError:
            value = raw
        value = html.unescape(value).strip()
        if value and value not in found:
            found.append(value)
    return found


def extract_links(text: str) -> list[str]:
    links: list[str] = []
    for match in URL_FEED_RE.finditer(text):
        url = html.unescape(match.group(0)).rstrip('.,)\\/')
        if any(skip in url for skip in (
                "twimg.com", "x.com/i/", "twitter.com/i/", "abs.twimg.com",
                "video.twimg.com", "ton.twitter.com", "schema.org")):
            continue
        if url.startswith("https://x.com/") or url.startswith("https://twitter.com/"):
            continue
        if url not in links:
            links.append(url)
    return links


def extract_tweet_texts(text: str) -> list[str]:
    """Pull the visible tweet bodies out of the rendered HTML."""
    blocks = re.findall(r'data-testid="tweetText"[^>]*>(.*?)</div>', text, re.S)
    seen: list[str] = []

    def to_text(fragment: str) -> str:
        fragment = re.sub(r"<br\s*/?>", "\n", fragment)
        fragment = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*>', r"\1", fragment)
        fragment = re.sub(r"<[^>]+>", "", fragment)
        return html.unescape(fragment).strip()

    for block in blocks:
        value = to_text(block)
        if value and value not in seen:
            seen.append(value)
    return seen


def plain_text(text: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</(p|div|li|h\d)>", "\n", text)
    text = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*>', r" \1 ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def main() -> int:
    path = Path(sys.argv[1])
    text = load(path)
    tweets = extract_tweet_texts(text)
    texts = extract_full_texts(text)
    links = extract_links(text)
    print(f"== tweets (rendered): {len(tweets)} ==")
    for item in tweets[:30]:
        print("-----")
        print(item[:1500])
    if texts:
        print(f"\n== tweets (json): {len(texts)} ==")
        for item in texts[:10]:
            print("-", item.replace("\n", " ")[:300])
    print(f"\n== links: {len(links)} ==")
    for item in links[:120]:
        print(item)

    if "--plain" in sys.argv:
        plain = plain_text(text)
        print("\n== plain text (4000 chars) ==")
        print(plain[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())