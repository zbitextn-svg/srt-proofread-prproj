#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""srtlib.py — SRT 读写与时间工具。无第三方依赖。

设计上容忍常见的「脏 SRT」：BOM、CRLF、序号缺失或错乱、
毫秒分隔符是逗号或点、时间码不足两位、多余空行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

_TS_RE = re.compile(
    r"(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)


@dataclass
class Cue:
    start: float
    end: float
    text: str
    index: int = 0

    @property
    def dur(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def chars(self) -> int:
        return len(re.sub(r"\s", "", self.text))

    @property
    def cps(self) -> float:
        return self.chars / self.dur if self.dur > 0 else 0.0


def parse_time(s: str) -> float:
    m = _TS_RE.search(s)
    if not m:
        raise ValueError("非法时间码: %r" % s)
    h, mi, se, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000.0


def fmt_time(t: float, sep: str = ",") -> str:
    if t < 0:
        t = 0.0
    total_ms = int(round(t * 1000))
    h, rem = divmod(total_ms, 3600000)
    mi, rem = divmod(rem, 60000)
    se, ms = divmod(rem, 1000)
    return "%02d:%02d:%02d%s%03d" % (h, mi, se, sep, ms)


def parse_srt(content: str) -> List[Cue]:
    if content.startswith("\ufeff"):
        content = content[1:]
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", content.strip())
    cues: List[Cue] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        ts_i = None
        for i, ln in enumerate(lines[:2]):
            if "-->" in ln:
                ts_i = i
                break
        if ts_i is None:
            continue
        left, _, right = lines[ts_i].partition("-->")
        try:
            start = parse_time(left)
            end = parse_time(right)
        except ValueError:
            continue
        text = "\n".join(lines[ts_i + 1:]).strip()
        if not text:
            continue
        cues.append(Cue(start, end, text, len(cues) + 1))
    return cues


def load_srt(path: str) -> List[Cue]:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return parse_srt(fh.read())


def dump_srt(cues: Sequence[Cue], sep: str = ",") -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(
            "%d\n%s --> %s\n%s\n" % (i, fmt_time(c.start, sep), fmt_time(c.end, sep), c.text)
        )
    return "\n".join(out)


def save_srt(cues: Sequence[Cue], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dump_srt(cues))


# ------------------------------------------------------------------ 时间轴


def snap_to_frame(t: float, fps: float) -> float:
    return round(t * fps) / fps


def join_small_gaps(
    cues: List[Cue], max_gap: float = 0.2, fps: Optional[float] = None
) -> int:
    """把相邻字幕之间过小的空隙直接连上，避免字幕「时上时下」跳。

    用毫秒整数比较：0.2 秒的间隔在浮点下可能算成 0.2000000000001 而漏判。
    返回连上的处数。
    """
    n = 0
    limit = int(round(max_gap * 1000))
    for i in range(1, len(cues)):
        gap = int(round((cues[i].start - cues[i - 1].end) * 1000))
        if 0 < gap <= limit:
            cues[i - 1].end = cues[i].start
            n += 1
    if fps:
        for c in cues:
            c.start = snap_to_frame(c.start, fps)
            c.end = snap_to_frame(c.end, fps)
    return n


def fix_overlaps(cues: List[Cue], fps: Optional[float] = None) -> int:
    """把重叠按中点切开（后一条起点不能早于前一条终点）。"""
    n = 0
    min_step = 1.0 / fps if fps else 0.04
    for i in range(1, len(cues)):
        if cues[i].start < cues[i - 1].end - 1e-9:
            mid = (cues[i].start + cues[i - 1].end) / 2
            cues[i - 1].end = max(cues[i - 1].start + min_step, mid)
            cues[i].start = min(cues[i].end - min_step, mid)
            n += 1
    return n


def stats(cues: Sequence[Cue], fps: float = 25.0) -> dict:
    if not cues:
        return {"count": 0}
    durs = [c.dur for c in cues]
    chars = [c.chars for c in cues]
    gaps = []
    for a, b in zip(cues, cues[1:]):
        gaps.append(int(round((b.start - a.end) * 1000)))
    return {
        "count": len(cues),
        "chars_avg": round(sum(chars) / len(chars), 1),
        "chars_max": max(chars),
        "dur_min": round(min(durs), 2),
        "dur_max": round(max(durs), 2),
        "dur_avg": round(sum(durs) / len(durs), 2),
        "gap_zero": sum(1 for g in gaps if g == 0),
        "gap_0_200ms": sum(1 for g in gaps if 0 < g <= 200),
        "gap_overlap": sum(1 for g in gaps if g < 0),
        "total_span": round(cues[-1].end - cues[0].start, 2),
        "max_cps": round(max(c.cps for c in cues), 2),
    }
