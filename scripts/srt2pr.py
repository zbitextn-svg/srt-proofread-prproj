#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""srt2pr.py — 一步把 SRT 变成 Premiere 工程（沿用模版样式）。

    python3 srt2pr.py 字幕.srt --template 模版.prproj --out 工程.prproj

可选先做音频对齐再导出：

    python3 srt2pr.py 字幕.srt --template 模版.prproj --out 工程.prproj \
        --align 视频.mp4 --join 0.2

做完会回读工程自检，把条数/文本/时间偏差/引用完整性打成 JSON，
方便上游 agent 判断是否可以直接交付。

安全边界（见 safety.py）：输出只能落在工作目录白名单内，输入文件一律不许覆盖，
覆盖陌生文件要人工批准，每次运行都写变更日志。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from prproj import Prproj, TICKS_PER_SECOND
from safety import Guard, SafetyError, fail
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
    # --- 安全边界 ---
    ap.add_argument(
        "--workdir",
        default=None,
        help="写入白名单目录，默认取输入字幕所在目录；所有输出必须落在其中",
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

    # ---------- 先过安全门，任何写入之前 ----------
    try:
        guard = Guard.build(
            workdir=args.workdir,
            primary_input=args.srt,
            inputs=[args.template, args.align] if args.align else [args.template],
            approve=args.approve_overwrite,
            interactive=args.interactive,
        )
        out_path = guard.check_output(args.out, what="工程输出")
        srt_out = guard.check_output(args.save_srt, what="字幕输出") if args.save_srt else None
    except SafetyError as err:
        return fail(err, "srt2pr")

    cues = load_srt(args.srt)
    if not cues:
        print(json.dumps({"ok": False, "error": "SRT 里没有解析到条目"}, ensure_ascii=False))
        return 2

    report = {
        "ok": True,
        "input": {"cues": len(cues), "stats": stats(cues, args.fps)},
        "boundary": {"workdir": guard.workdir, "log": guard.log_path},
    }

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
        out_path,
        track_index=args.track,
        sequence_name=args.sequence_name,
        fps=args.fps,
        keep_template_items=args.keep_template_items,
    )
    report["export"] = info

    # 回读校验
    chk = Prproj(out_path)
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
    report["output"] = out_path

    written = [out_path]
    if srt_out:
        save_srt(cues, srt_out)
        report["saved_srt"] = srt_out
        written.append(srt_out)

    # ---------- 留痕 + 确认原始素材没被动 ----------
    try:
        log_path = guard.log("srt2pr", args=vars(args), outputs=written, note="export prproj")
        guard.verify_inputs_untouched()
    except SafetyError as err:
        return fail(err, "srt2pr")
    report["change_log"] = log_path

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
