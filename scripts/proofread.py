#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""proofread.py — 字幕文本校对：只给建议，不擅自改。

核心原则（踩过的坑）：
1. 每一处改动必须能说清依据，输出「第几条 + 原文 → 建议 + 规则」，
   由人确认；默认不写回文件。
2. 语气词/笑声的判定依赖位置（句尾 vs 句中），句中一律不动，
   因为「哎呀」这类词在句中常常是实义感叹，删了会断气口。
3. 重复字不一律当语音识别重影：中间有停顿就是真说了两遍，
   保留字、把一条拆成两条即可。所以这里只提示，不删。
4. 作品名/组合名/专有名词靠词表，代码里不写死任何具体名字。

用法：
    python3 proofread.py in.srt --report report.md
    python3 proofread.py in.srt --out fixed.srt --apply
    python3 proofread.py in.srt --rules my_rules.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Sequence

from safety import Guard, SafetyError, fail
from srtlib import Cue, load_srt, save_srt, stats

# ------------------------------------------------------------------ 默认规则

DEFAULT_RULES = {
    # 句尾单独出现、可以直接删的语气词（不含 吧/吗/嘛/呢，这三个承载疑问或软化语气）
    "tail_particles": ["啊", "呀", "哇", "哦", "喔", "噢", "嗯", "呃", "唉", "诶", "哎", "哈", "呗", "嘿"],
    # 永远保留
    "keep_particles": ["吧", "吗", "嘛", "呢"],
    # 笑声：连续两个字以上，整段删（含前面空格）
    "laugh_pattern": r"(哈){2,}|(嘿){2,}|(呵){2,}|(嘻){2,}|(嘎){2,}",
    # 英文 → 中文。按自己的项目口径改这个表，代码不预设任何业务词
    "en_map": {},
    # 需要加书名号的作品名 / 节目名（填全称，不要缩写）
    "works": [],
    # 需要加双引号的组合名、外号
    "groups": [],
    # 单个语意行的字数上限，超出只提示不硬拆
    "max_line_chars": 14,
    # 提示阈值
    "min_dur": 0.30,
    "max_cps": 9.0,
    "join_gap": 0.20,
}

# 中文数字
_CN_DIGITS = "零一二三四五六七八九"


def _int_to_cn(n: int) -> str:
    if n < 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    if n < 100:
        return _CN_DIGITS[n // 10] + "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    if n < 1000:
        s = _CN_DIGITS[n // 100] + "百"
        r = n % 100
        if r == 0:
            return s
        if r < 10:
            return s + "零" + _CN_DIGITS[r]
        return s + (_CN_DIGITS[r // 10] + "十" + (_CN_DIGITS[r % 10] if r % 10 else ""))
    if n < 10000:
        s = _CN_DIGITS[n // 1000] + "千"
        r = n % 1000
        if r == 0:
            return s
        if r < 100:
            return s + "零" + _int_to_cn(r)
        return s + _int_to_cn(r)
    return str(n)


@dataclass
class Issue:
    cue: int
    start: float
    code: str
    level: str          # error / warn / info
    before: str
    after: str
    rule: str
    auto: bool = False  # 是否属于可安全自动应用


def _merge_rules(rules: Optional[dict]) -> dict:
    out = dict(DEFAULT_RULES)
    if rules:
        out.update(rules)
    return out


# ------------------------------------------------------------------ 文本规则


def check_text(cue: Cue, idx: int, rules: dict) -> List[Issue]:
    out: List[Issue] = []
    t = cue.text
    plain = t.replace("\n", "")

    # 1) 笑声
    laugh_spans = []
    for m in re.finditer(rules["laugh_pattern"], plain):
        laugh_spans.append((m.start(), m.end()))
        out.append(
            Issue(idx, cue.start, "laugh", "warn", m.group(0), "（删除）", "笑声不写字幕", auto=True)
        )

    # 2) 整条只有语气词
    stripped = re.sub(r"[\s，。！？、,.!?…~～]", "", plain)
    if stripped and all(ch in "".join(rules["tail_particles"]) + "".join(rules["keep_particles"]) for ch in stripped):
        out.append(
            Issue(idx, cue.start, "filler_only", "warn", plain, "（整条删除）", "整条只有语气词", auto=False)
        )

    # 3) 句尾语气词（只删一个，逐条给）
    if len(stripped) >= 2 and stripped[-1] in rules["tail_particles"]:
        out.append(
            Issue(
                idx,
                cue.start,
                "tail_particle",
                "info",
                plain,
                _drop_last_char(plain),
                "句尾语气词「%s」" % stripped[-1],
                auto=False,
            )
        )

    # 4) 英文（长词优先，大小写同形只报一次；中文替换顺带收掉两侧空格）
    en_map = rules.get("en_map", {})
    seen: set = set()
    for en in sorted(en_map, key=len, reverse=True):
        if not en:
            continue
        key = en.lower()
        if key in seen:
            continue
        seen.add(key)
        cn = en_map[en]
        pat = re.compile(r"\s*(?<![A-Za-z])%s(?![A-Za-z])\s*" % re.escape(en), re.I)
        if pat.search(plain):
            out.append(
                Issue(
                    idx,
                    cue.start,
                    "english",
                    "warn",
                    plain,
                    _tidy(pat.sub(cn, plain)),
                    "唱词不留英文：%s → %s" % (en, cn),
                    auto=True,
                )
            )

    # 5) 阿拉伯数字
    for m in re.finditer(r"\d+", plain):
        n = int(m.group(0))
        if 0 <= n <= 9999 and not re.match(r"\d{4}年?", plain[max(0, m.start() - 1): m.end() + 1]):
            out.append(
                Issue(
                    idx,
                    cue.start,
                    "arabic_number",
                    "info",
                    m.group(0),
                    _int_to_cn(n),
                    "数字写中文",
                    auto=False,
                )
            )

    # 6) 作品名缺书名号
    for w in rules.get("works", []):
        if not w:
            continue
        if w in plain and "《%s》" % w not in plain:
            out.append(
                Issue(
                    idx,
                    cue.start,
                    "work_title",
                    "warn",
                    plain,
                    plain.replace(w, "《%s》" % w),
                    "作品名必须加书名号",
                    auto=True,
                )
            )

    # 7) 组合名缺引号
    for g in rules.get("groups", []):
        if not g:
            continue
        if g in plain and ("“%s”" % g) not in plain and ("「%s」" % g) not in plain:
            out.append(
                Issue(
                    idx,
                    cue.start,
                    "group_quote",
                    "info",
                    plain,
                    plain.replace(g, "“%s”" % g),
                    "组合名/外号加双引号",
                    auto=False,
                )
            )

    # 8) 相邻重复字（只提示：可能是真说了两遍）
    for m in re.finditer(r"([\u4e00-\u9fa5])\1", plain):
        if m.group(1) in "的了个是":
            continue
        if any(a <= m.start() < b for a, b in laugh_spans):
            continue
        out.append(
            Issue(
                idx,
                cue.start,
                "dup_char",
                "info",
                m.group(0),
                m.group(1),
                "相邻重复字：先听中间有没有停顿，有停顿是两个独立音节，保留并拆条",
                auto=False,
            )
        )

    # 9) 的/地/得
    for m in re.finditer(r"([\u4e00-\u9fa5])(地|得)(?=[\u4e00-\u9fa5])", plain):
        out.append(
            Issue(
                idx,
                cue.start,
                "de",
                "info",
                m.group(0),
                "",
                "确认「的/地/得」用字",
                auto=False,
            )
        )

    # 10) 行长
    n = len(re.sub(r"\s", "", plain))
    if n > rules["max_line_chars"]:
        out.append(
            Issue(
                idx,
                cue.start,
                "long_line",
                "info",
                plain,
                "",
                "%d 字，超过 %d 字建议按语意断句（不是按字数均分）"
                % (n, rules["max_line_chars"]),
                auto=False,
            )
        )

    # 11) 句末标点
    if plain and plain[-1] in "，。、；：":
        out.append(
            Issue(idx, cue.start, "punct", "info", plain, plain[:-1], "字幕结尾不留标点", auto=True)
        )
    return out


def _tidy(s: str) -> str:
    """只压缩连续空格。英文词两侧的空格已在正则里吃掉，
    句子中间有意的分层空格（一个）必须保留。"""
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def _drop_last_char(s: str) -> str:
    for i in range(len(s) - 1, -1, -1):
        if s[i] in "，。！？、,.!?…~～ \n":
            continue
        return (s[:i] + s[i + 1:]).rstrip()
    return s


# ------------------------------------------------------------------ 时间规则


def check_timing(cues: Sequence[Cue], rules: dict, fps: float) -> List[Issue]:
    out: List[Issue] = []
    for i, c in enumerate(cues):
        if c.dur < rules["min_dur"]:
            out.append(
                Issue(
                    i,
                    c.start,
                    "too_short",
                    "info",
                    "%s（%.2fs）" % (c.text, c.dur),
                    "",
                    "条目过短：只要切点正确就不用凑时长",
                    auto=False,
                )
            )
        if c.cps > rules["max_cps"]:
            out.append(
                Issue(
                    i,
                    c.start,
                    "fast",
                    "info",
                    "%s（%.1f 字/秒）" % (c.text, c.cps),
                    "",
                    "语速偏快，检查是否该拆条",
                    auto=False,
                )
            )
    for i in range(1, len(cues)):
        gap = cues[i].start - cues[i - 1].end
        ms = int(round(gap * 1000))
        if ms < 0:
            out.append(
                Issue(i, cues[i].start, "overlap", "error", "", "", "与上一条重叠 %.0fms" % (-ms), auto=False)
            )
        elif 0 < ms <= int(round(rules["join_gap"] * 1000)):
            out.append(
                Issue(
                    i,
                    cues[i].start,
                    "small_gap",
                    "warn",
                    "与上一条间隔 %dms" % ms,
                    "0",
                    "间隔 ≤%.1f 秒，直接连上，避免字幕时上时下" % rules["join_gap"],
                    auto=True,
                )
            )
    return out


# ------------------------------------------------------------------ 输出


def apply_auto(cues: List[Cue], issues: Sequence[Issue]) -> int:
    """只应用可安全自动改的规则：英文、书名号、句末标点。"""
    by_idx: Dict[int, List[Issue]] = {}
    for it in issues:
        if it.auto and it.code in ("english", "work_title", "punct"):
            by_idx.setdefault(it.cue, []).append(it)
    n = 0
    for idx, items in by_idx.items():
        text = cues[idx].text
        for it in items:
            if it.after and it.after != text:
                text = it.after
        if text != cues[idx].text:
            cues[idx].text = text
            n += 1
    return n


_LEVEL_ORDER = {"error": 0, "warn": 1, "info": 2}


def to_markdown(issues: Sequence[Issue], st: dict, n_cues: int) -> str:
    issues = sorted(issues, key=lambda x: (x.cue, _LEVEL_ORDER.get(x.level, 3)))
    lines = ["# 字幕校对报告", ""]
    lines.append("条目 %d 条，共 %d 处待确认。" % (n_cues, len(issues)))
    if st:
        lines.append("")
        lines.append(
            "平均 %.1f 字/条，最长 %d 字，均长 %.2fs，间隔为 0 的 %d 处，≤200ms 的 %d 处。"
            % (
                st.get("chars_avg", 0),
                st.get("chars_max", 0),
                st.get("dur_avg", 0),
                st.get("gap_zero", 0),
                st.get("gap_0_200ms", 0),
            )
        )
    if not issues:
        lines += ["", "没有发现问题。"]
        return "\n".join(lines) + "\n"

    lines += ["", "| # | 时间码 | 原文 | 建议 | 规则 | 级别 |", "|---|---|---|---|---|---|"]
    for it in issues:
        lines.append(
            "| %d | %s | %s | %s | %s | %s |"
            % (
                it.cue + 1,
                _ts(it.start),
                _md(it.before),
                _md(it.after),
                it.rule,
                it.level,
            )
        )
    return "\n".join(lines) + "\n"


def _ts(t: float) -> str:
    m = int(t // 60)
    s = t - m * 60
    return "%d:%05.2f" % (m, s)


def _md(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="SRT 字幕校对（只建议，不擅自改）")
    ap.add_argument("srt")
    ap.add_argument("--rules", help="自定义规则 JSON")
    ap.add_argument("--report", default=None, help="输出 Markdown 报告")
    ap.add_argument("--out", default=None, help="输出修正后的 SRT")
    ap.add_argument("--apply", action="store_true", help="应用可安全自动改的规则")
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--join", action="store_true", help="顺手把 ≤join_gap 的间隔连上")
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

    # 写入类参数先过安全门，再动任何文件
    try:
        guard = Guard.build(
            workdir=args.workdir,
            primary_input=args.srt,
            inputs=[args.rules] if args.rules else [],
            approve=args.approve_overwrite,
            interactive=args.interactive,
        )
        report_path = guard.check_output(args.report, what="报告输出") if args.report else None
        srt_out = guard.check_output(args.out, what="字幕输出") if args.out else None
    except SafetyError as err:
        return fail(err, "proofread")

    rules = dict(DEFAULT_RULES)
    if args.rules:
        with open(args.rules, "r", encoding="utf-8") as fh:
            rules.update(json.load(fh))

    cues = load_srt(args.srt)
    if not cues:
        print("没有解析到任何字幕条目", file=sys.stderr)
        return 2

    issues = check_text_all(cues, rules) + check_timing(cues, rules, args.fps)

    if args.apply:
        n = apply_auto(cues, issues)
        print("已应用 %d 处安全改动" % n, file=sys.stderr)
    if args.join:
        from srtlib import join_small_gaps

        n = join_small_gaps(cues, rules["join_gap"], args.fps)
        print("已连上 %d 处小间隔" % n, file=sys.stderr)
        # 连上之后时间类问题要按最终结果重算，否则报告会显示已经修掉的旧问题
        issues = [i for i in issues if i.code not in ("small_gap", "overlap", "too_short")]
        issues += check_timing(cues, rules, args.fps)

    md = to_markdown(issues, stats(cues, args.fps), len(cues))
    written = []
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(md)
        print("报告写入 %s" % report_path, file=sys.stderr)
        written.append(report_path)
    else:
        print(md)
    if srt_out:
        save_srt(cues, srt_out)
        print("字幕写入 %s" % srt_out, file=sys.stderr)
        written.append(srt_out)

    try:
        if written:
            guard.log("proofread", args=vars(args), outputs=written, note="%d issues" % len(issues))
        guard.verify_inputs_untouched()
    except SafetyError as err:
        return fail(err, "proofread")
    if written:
        print("变更日志 %s" % guard.log_path, file=sys.stderr)
    return 0


def check_text_all(cues: Sequence[Cue], rules: dict) -> List[Issue]:
    out: List[Issue] = []
    for i, c in enumerate(cues):
        out.extend(check_text(c, i, rules))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
