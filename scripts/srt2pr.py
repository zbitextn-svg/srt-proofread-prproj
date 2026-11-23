#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""srt2pr.py — 一步把 SRT 变成 Premiere 工程（沿用模版样式）。

    python3 srt2pr.py 字幕.srt --template 模版.prproj --out 工程.prproj

可选先做音频对齐再导出：

    python3 srt2pr.py 字幕.srt --template 模版.prproj --out 工程.prproj \
        --align 视频.mp4 --join 0.2

做完会回读工程自检，把条数/文本/时间偏差/引用完整性打成 JSON，
方便上游 agent 判断是否可以直接交付。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from prproj import Prproj, TICKS_PER_SECOND
from srtlib import Cue, load_srt, save_srt, join_small_gaps, stats


def _fmt(t: float) -> str:
    m = int(t // 60)
    return "%d:%05.2f" % (m, t - m * 60)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="SRT → Premiere 工程（母版克隆，样式继承）")
    ap.add_argument("srt")
    ap.add_argument("--template", required=True, help="用作样式模版的 .prproj")
    ap.add_argument("--out", required=True, help="输出 .prproj")
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--track", type=int, default=None, help="模版里第几条视频轨（0 起）")
    ap.add_argument("--sequence-name", default=None)
    ap.add_argument("--align", default=None, help="用于气口分析的视频/音频")
    ap.add_argument("--max-shift", type=float, default=0.25)
    ap.add_argument("--join", type=float, default=0.0, help="≤该秒数的间隔连上，默认不连")
    ap.add_argument("--keep-template-items", action="store_true")
    ap.add_argument("--save-srt", default=None, help="把处理后的字幕另存一份")
    args = ap.parse_args(argv)

    cues = load_srt(args.srt)
    if not cues:
        print(json.dumps({"ok": False, "error": "SRT 里没有解析到条目"}, ensure_ascii=False))
        return 2

    report = {"ok": True, "input": {"cues": len(cues), "stats": stats(cues, args.fps)}}

    if args.align:
        from audioalign import align, extract_db

        db = extract_db(args.align)
        results, notes = align(cues, db, args.fps, args.max_shift, args.join)
        cues = [Cue(r.new_start, r.new_end, c.text, i + 1) for i, (r, c) in enumerate(zip(results, cues))]
        report["align"] = {
            "adjusted": sum(
                1 for r in results if abs(r.new_start - r.old_start) > 1e-6 or abs(r.new_end - r.old_end) > 1e-6
            ),
            "over_2frames": notes,
        }

    joined = 0
    if args.join > 0:
        joined = join_small_gaps(cues, args.join, args.fps)
    report["joined"] = joined

    pr = Prproj(args.template)
    info = pr.export(
        [(c.text, c.start, c.end) for c in cues],
        args.out,
        track_index=args.track,
        sequence_name=args.sequence_name,
        fps=args.fps,
        keep_template_items=args.keep_template_items,
    )
    report["export"] = info

    # 回读校验
    chk = Prproj(args.out)
    integrity = chk.check_integrity()
    got = chk.read_back()
    expect = [(c.text, c.start, c.end) for c in cues]
    text_ok = [g[0] for g in got] == [e[0] for e in expect]
    dev = []
    for g, e in zip(got, expect):
        dev.append(abs(g[1] - e[1]) + abs(g[2] - e[2]))
    max_dev_frames = (max(dev) * args.fps) if dev else 0.0
    report["verify"] = {
        "items_in_project": len(got),
        "items_expected": len(expect),
        "text_all_match": text_ok,
        "max_time_dev_frames": round(max_dev_frames, 2),
        "dangling_refs": integrity["dangling"],
        "duplicate_ids": integrity["duplicate_ids"],
        "objects": integrity["objects"],
    }
    ok = (
        len(got) == len(expect)
        and text_ok
        and not integrity["dangling"]
        and not integrity["duplicate_ids"]
        and max_dev_frames <= 1.01
    )
    report["ok"] = bool(ok)
    report["output"] = os.path.abspath(args.out)

    if args.save_srt:
        save_srt(cues, args.save_srt)
        report["saved_srt"] = os.path.abspath(args.save_srt)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
