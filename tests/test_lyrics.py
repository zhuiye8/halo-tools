"""LRC parsing and lyric matching tests."""

from __future__ import annotations

from halobar.lyrics import (
    Lyrics,
    LyricLine,
    SongMatch,
    parse_lrc,
    score_candidate,
)


def test_parse_lrc_basic_and_multiple_tags():
    content = "[ti:测试]\n[00:01.50]第一句\n[00:03.00][00:05.00]重复句\n[00:07.123]第三句\n"
    lines = parse_lrc(content)
    assert [line.start for line in lines] == [1.5, 3.0, 5.0, 7.123]
    assert [line.text for line in lines] == ["第一句", "重复句", "重复句", "第三句"]


def test_parse_lrc_offset_and_metadata():
    content = "[offset:-500]\n[00:02.00]延迟半秒\n"
    lines = parse_lrc(content)
    assert lines[0].start == 1.5


def test_parse_lrc_translation_alignment():
    lrc = "[00:01.00]hello\n[00:03.00]world\n"
    translation = "[00:01.00]你好\n[00:03.00]世界\n"
    lines = parse_lrc(lrc, translation)
    assert lines[0].translation == "你好"
    assert lines[1].translation == "世界"


def test_credit_and_name_tag_lines_are_dropped():
    content = (
        "[00:01.00]作词 : 某人\n"
        "[00:02.00]祝青：\n"
        "[00:03.00]G2er\n"
        "[00:04.00]Oh\n"
        "[00:05.00]真正的歌词\n"
    )
    texts = [line.text for line in parse_lrc(content)]
    assert texts == ["Oh", "真正的歌词"]


def test_drop_names_removes_performer_lines():
    content = "[00:01.00]灼夭\n[00:02.00]风催雨\n"
    texts = [line.text for line in parse_lrc(content, drop_names=("祝青", "灼夭"))]
    assert texts == ["风催雨"]


def test_with_ends_fills_gaps():
    lyrics = Lyrics([LyricLine(1.0, "a"), LyricLine(4.0, "b")]).with_ends()
    assert lyrics.lines[0].end == 4.0
    assert lyrics.lines[1].end > 4.0


def test_line_at():
    lyrics = Lyrics([LyricLine(0.0, "a", 2.0), LyricLine(2.0, "b", 4.0)])
    assert lyrics.line_at(0.5).text == "a"
    assert lyrics.line_at(2.0).text == "b"
    assert lyrics.line_at(1.0).text == "a"
    assert lyrics.line_at(-1.0) is None


def test_score_candidate_prefers_exact_title_and_artist():
    exact = SongMatch(1, "如愿", "王菲", 265383)
    other = SongMatch(2, "如愿", "王菲菲", 267885)
    duration = 265.4
    assert score_candidate(exact, "如愿", "王菲", duration) > score_candidate(
        other, "如愿", "王菲", duration)