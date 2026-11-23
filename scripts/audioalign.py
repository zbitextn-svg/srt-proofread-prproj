#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audioalign.py — 用音频能量把字幕切点吸附到气口上。

只看包络，不做语音识别。依据是：人在换气/断句的地方，能量会掉到
局部语音强度以下十几 dB。把切点挪到这些「谷」里，字幕就不会切在
字中间。

流程：
1. ffmpeg 抽 16k 单声道，带通 250–4000Hz（去掉低频隆隆声和高频齿音）
2. 10ms 一帧算 RMS 转 dB
3. 3 点中值平滑（去掉单帧毛刺）
4. 自适应阈值 th = max(p90 - 14, -46) 判语音/静音
5. 每条字幕在起点/终点附近找最近的上升沿/下降沿
6. 输出对齐后的 SRT + 逐条调整报告

不依赖 numpy。
"""

from __future__ import annotations

import argparse
import array
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from safety import Guard, SafetyError, fail
from srtlib import Cue, load_srt, save_srt, fmt_time, stats, join_small_gaps

FRAME_MS = 10
SR = 16000
SPF = SR * FRAME_MS // 1000  # 每帧样本数


# ------------------------------------------------------------------ 音频分析


def extract_db(path: str, start: float = 0.0, duration: Optional[float] = None) -> List[float]:
    """返回每 10ms 一帧的 dB 列表。"""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("找不到 ffmpeg，请先安装（brew install ffmpeg）")
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-ss", "%.3f" % start,
    ]
    if duration:
        cmd += ["-t", "%.3f" % duration]
    cmd += [
        "-i", path, "-vn", "-ac", "1", "-ar", str(SR),
        "-af", "highpass=f=250,lowpass=f=4000",
        "-f", "f32le", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 失败：%s" % proc.stderr.decode("utf-8", "ignore")[-500:])
    samples = array.array("f")
    samples.frombytes(proc.stdout)
    n = len(samples) // SPF
    out: List[float] = []
    for i in range(n):
        chunk = samples[i * SPF:(i + 1) * SPF]
        acc = 0.0
        for v in chunk:
            acc += v * v
        rms = math.sqrt(acc / SPF)
        db = 20.0 * math.log10(rms + 1e-9)
        out.append(max(db, -80.0))
    return out


def smooth(db: Sequence[float], k: int = 3) -> List[float]:
    if len(db) < k:
        return list(db)
    half = k // 2
    out = []
    for i in range(len(db)):
        lo = max(0, i - half)
        hi = min(len(db), i + half + 1)
        win = sorted(db[lo:hi])
        out.append(win[len(win) // 2])
    return out


def percentile(vals: Sequence[float], q: float) -> float:
    if not vals:
        return -80.0
    s = sorted(vals)
    k = int(round((len(s) - 1) * q))
    return s[max(0, min(len(s) - 1, k))]


def speech_threshold(db: Sequence[float]) -> float:
    return max(percentile(db, 0.90) - 14.0, -46.0)


def mask_of(db: Sequence[float], th: float) -> List[bool]:
    return [v > th for v in db]


def edges(mask: Sequence[bool]) -> Tuple[List[int], List[int]]:
    up, down = [], []
    for i in range(1, len(mask)):
        if not mask[i - 1] and mask[i]:
            up.append(i)
        elif mask[i - 1] and not mask[i]:
            down.append(i)
    return up, down


def nearest(seq: Sequence[int], target: int) -> Optional[int]:
    if not seq:
        return None
    lo, hi = 0, len(seq) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    best = seq[lo]
    if lo > 0 and abs(seq[lo - 1] - target) <= abs(best - target):
        best = seq[lo - 1]
    return best


@dataclass
class AlignResult:
    idx: int
    old_start: float
    old_end: float
    new_start: float
    new_end: float
    start_db: float
    end_db: float
    local_median: float

    @property
    def d_start(self) -> float:
        return self.new_start - self.old_start

    @property
    def d_end(self) -> float:
        return self.new_end - self.old_end


def align(
    cues: List[Cue],
    db: Sequence[float],
    fps: float = 25.0,
    max_shift: float = 0.25,
    join_gap: float = 0.20,
) -> Tuple[List[AlignResult], List[str]]:
    th = speech_threshold(db)
    mask = mask_of(db, th)
    up, down = edges(mask)
    results: List[AlignResult] = []
    notes: List[str] = []

    def t2f(t: float) -> int:
        return int(round(t * 1000 / FRAME_MS))

    def f2t(f: int) -> float:
        return f * FRAME_MS / 1000.0

    for i, c in enumerate(cues):
        # 相邻条目给出不越界的活动范围
        lo = cues[i - 1].end + 0.02 if i > 0 else 0.0
        hi = cues[i + 1].start - 0.02 if i + 1 < len(cues) else 1e9

        s_f, e_f = t2f(c.start), t2f(c.end)
        win = int(round(max_shift * 1000 / FRAME_MS))

        cand_up = [x for x in up if abs(x - s_f) <= win]
        cand_dn = [x for x in down if abs(x - e_f) <= win]
        new_s = f2t(nearest(cand_up, s_f)) if cand_up else c.start
        # 下降沿是「第一个静音帧」，出点取它前一帧，才贴着语音末尾
        new_e = f2t(max(0, nearest(cand_dn, e_f) - 1)) if cand_dn else c.end

        # 不越界、不产生倒挂
        new_s = max(lo, min(new_s, c.end - 0.04))
        new_e = min(hi, max(new_e, new_s + 0.04))
        new_s = max(0.0, round(new_s * fps) / fps)
        new_e = round(new_e * fps) / fps

        # 局部语音强度中位数（±2 秒），用于判断切点是否落在气口
        a = max(0, s_f - 200)
        b = min(len(db), e_f + 200)
        local_vals = [v for v, m in zip(db[a:b], mask[a:b]) if m]
        med = percentile(local_vals, 0.5) if local_vals else -80.0
        sdb = db[min(len(db) - 1, max(0, t2f(new_s)))]
        edb = db[min(len(db) - 1, max(0, t2f(new_e)))]

        results.append(AlignResult(i, c.start, c.end, new_s, new_e, sdb, edb, med))
        if abs(new_s - c.start) > 2.0 / fps + 1e-9 or abs(new_e - c.end) > 2.0 / fps + 1e-9:
            notes.append(
                "第 %d 条位移超过 2 帧：入 %.2f→%.2f，出 %.2f→%.2f"
                % (i + 1, c.start, new_s, c.end, new_e)
            )

    return results, notes


def suggest_splits(
    cues: Sequence[Cue],
    db: Sequence[float],
    min_silence: float = 0.18,
    min_piece: float = 0.30,
) -> List[Tuple[int, float]]:
    """条内长静音位置，作为拆条参考点。"""
    th = speech_threshold(db)
    mask = mask_of(db, th)
    out: List[Tuple[int, float]] = []
    for i, c in enumerate(cues):
        a = int(round(c.start * 100 / 10))
        b = int(round(c.end * 100 / 10))
        run = 0
        for k in range(a, min(b, len(mask))):
            if not mask[k]:
                run += 1
                continue
            if run * FRAME_MS / 1000.0 >= min_silence:
                t = k * FRAME_MS / 1000.0
                if t - c.start >= min_piece and c.end - t >= min_piece:
                    out.append((i, t))
            run = 0
    return out


# ------------------------------------------------------------------ 报告


def report_md(cues: Sequence[Cue], results: Sequence[AlignResult], notes: Sequence[str], splits, fps: float) -> str:
    lines = ["# 时间轴对齐报告", ""]
    moved = [
        r for r in results
        if abs(r.new_start - r.old_start) > 1e-6 or abs(r.new_end - r.old_end) > 1e-6
    ]
    lines.append("共 %d 条，调整 %d 条。" % (len(cues), len(moved)))
    if notes:
        lines += ["", "## 超差（>2 帧）", ""] + ["- " + n for n in notes]
    lines += ["", "| # | 原文 | 入点 | 出点 | 位移 | 切点能量 |", "|---|---|---|---|---|---|"]
    for r, c in zip(results, cues):
        ds = r.d_start * fps
        de = r.d_end * fps
        lines.append(
            "| %d | %s | %.2f→%.2f | %.2f→%.2f | %+.0f / %+.0f 帧 | 入 %+.1fdB / 出 %+.1fdB |"
            % (
                r.idx + 1,
                (c.text or "")[:18].replace("|", "\\|"),
                r.old_start,
                r.new_start,
                r.old_end,
                r.new_end,
                ds,
                de,
                r.start_db - r.local_median,
                r.end_db - r.local_median,
            )
        )
    if splits:
        lines += ["", "## 条内长静音（拆条参考）", ""]
        for i, t in splits:
            lines.append("- 第 %d 条 %.2fs 处有气口：%s" % (i + 1, t, cues[i].text))
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="用音频能量把字幕切点吸附到气口")
    ap.add_argument("media", help="视频或音频文件")
    ap.add_argument("srt", help="待对齐的字幕")
    ap.add_argument("--out", help="输出对齐后的 SRT")
    ap.add_argument("--report", help="输出 Markdown 报告")
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--max-shift", type=float, default=0.25, help="最大吸附位移（秒）")
    ap.add_argument("--join", type=float, default=0.20, help="≤该间隔直接连上，0 表示不连")
    ap.add_argument("--split-suggest", action="store_true", help="顺带提示条内气口")
    # --- 安全边界 ---
    ap.add_argument(
        "--workdir", default=None, help="写入白名单目录，默认取输入字幕所在目录"
    )
    ap.add_argument(
        "--approve-overwrite",
        action="store_true",
        help="批准覆盖已存在且非本工具生成的文件（无人值守时才用）",
    )
    ap.add_argument(
        "--interactive",
        action="store_true",
        help="覆盖陌生文件时在终端当场询问（只在人操作时用，agent 别加）",
    )
    args = ap.parse_args(argv)

    try:
        guard = Guard.build(
            workdir=args.workdir,
            primary_input=args.srt,
            inputs=[args.media],
            approve=args.approve_overwrite,
            interactive=args.interactive,
        )
        srt_out = guard.check_output(args.out, what="字幕输出") if args.out else None
        report_path = guard.check_output(args.report, what="报告输出") if args.report else None
    except SafetyError as err:
        return fail(err, "audioalign")

    cues = load_srt(args.srt)
    if not cues:
        print("没有字幕条目", file=sys.stderr)
        return 2
    db = extract_db(args.media)
    if not db:
        print("没有读到音频", file=sys.stderr)
        return 2

    results, notes = align(cues, db, args.fps, args.max_shift, args.join)
    new_cues = [
        Cue(r.new_start, r.new_end, c.text, i + 1) for i, (r, c) in enumerate(zip(results, cues))
    ]
    joined = 0
    if args.join > 0:
        joined = join_small_gaps(new_cues, args.join, args.fps)
        # 连上会改出点，报告按最终值对齐
        for r, c in zip(results, new_cues):
            r.new_end = c.end

    splits = suggest_splits(new_cues, db) if args.split_suggest else []
    md = report_md(new_cues, results, notes, splits, args.fps)
    written = []
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(md)
        print("报告 → %s" % report_path, file=sys.stderr)
        written.append(report_path)
    else:
        print(md)
    if srt_out:
        save_srt(new_cues, srt_out)
        print("字幕 → %s（连上 %d 处）" % (srt_out, joined), file=sys.stderr)
        written.append(srt_out)

    try:
        if written:
            guard.log("audioalign", args=vars(args), outputs=written, note="joined %d" % joined)
        guard.verify_inputs_untouched()
    except SafetyError as err:
        return fail(err, "audioalign")
    if written:
        print("变更日志 → %s" % guard.log_path, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
