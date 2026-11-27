#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从一个已有的 .prproj 里，把每条字幕的文字和时间码取出来。

这是「路线 B：直接拿工程来校对」的第一步。取出来的清单是纯文本，
校对改的是这份清单，改完再交给 prwrite.py 写回工程 —— 这样中间过程
永远不会碰样式、时间码和对象结构。

难点只有一个：同一段文字在工程里可能只存了一份，其余条目用
`<StartKeyframeValue Encoding="base64" BinaryHash="..."/>` 引用它
（自闭合，没有值）。脚本会用同一个 hash 里带值的那条反查出来，
并在报告里标出哪些是「回填」的。

用法：
    python3 prtext.py 工程.prproj --workdir 交付
    python3 prtext.py 工程.prproj --track 0 --txt 交付/清单.txt --json 交付/清单.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prproj as P  # noqa: E402

# <StartKeyframeValue ...> 或 <StartKeyframeValue ... />
TAG_RE = re.compile(r"<StartKeyframeValue\b([^>]*)>", re.S)
PARAM0_RE = re.compile(r'<Param Index="0" ObjectRef="(\d+)"\s*/>')


def iter_values(xml: str):
    """遍历所有 StartKeyframeValue，产出 (hash, 是否自闭合, 内容或 None)。

    必须先把「自闭合」和「带值」分开判断，不能让一条正则用 `.*?` 去补
    `</StartKeyframeValue>`：自闭合的标签会一路跨过去，把紧随其后的带值
    条目整个吞掉，导致那一条的文本永远查不到。
    """
    for m in TAG_RE.finditer(xml):
        attrs = m.group(1).rstrip()
        closed = attrs.endswith("/")
        hm = re.search(r'BinaryHash="([^"]+)"', attrs)
        if not hm:
            continue
        if closed:
            yield hm.group(1), True, None
            continue
        end = xml.find("</StartKeyframeValue>", m.end())
        if end == -1:
            continue
        yield hm.group(1), False, xml[m.end():end]


def tail_text(blob: bytes):
    """blob 尾部的 UTF-8 文本。

    结构是 [其它数据][u32 字节长度][UTF-8 文本][补 0 到 4 的倍数]。
    尾部补零个数随文本长度变化，所以不能要求文本正好顶到结尾。
    """
    n = len(blob)
    for idx in range(n - 1, 3, -1):
        (ln,) = struct.unpack_from("<I", blob, idx - 4)
        if ln <= 0 or idx + ln > n:
            continue
        if any(blob[idx + ln:]):
            continue
        try:
            text = blob[idx:idx + ln].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text and all(c.isprintable() or c in "\n\r\t" for c in text):
            return text
    return None


def decode_b64(raw: str):
    try:
        return base64.b64decode(re.sub(r"\s+", "", raw))
    except Exception:
        return None


def hash_text_map(pr) -> dict:
    """所有「带值」的 StartKeyframeValue：BinaryHash → 文本。"""
    out = {}
    for h, closed, content in iter_values(pr.xml):
        if closed or not content:
            continue
        blob = decode_b64(content)
        if blob is None:
            continue
        text = tail_text(blob)
        if text:
            out.setdefault(h, text)
    return out


def track_map(pr) -> dict:
    """VideoClipTrackItem 的 ObjectID → 视频轨序号（文档顺序，0 起）。"""
    out = {}
    for i, tr in enumerate(pr.objects_of("VideoClipTrack")):
        span = pr.xml[tr.start:tr.end]
        for m in re.finditer(r'<TrackItem Index="\d+" ObjectRef="(\d+)"/>', span):
            out[m.group(1)] = i
    return out


def text_param_of(pr, ti_block: str):
    """返回 (文本滤镜 block, 参数 block, 参数 ObjectID)，找不到返回 None。"""
    m = re.search(r'<Components ObjectRef="(\d+)"/>', ti_block)
    if not m:
        return None
    try:
        chain = pr.block(m.group(1))
    except KeyError:
        return None
    for ref in re.finditer(r'<Component Index="\d+" ObjectRef="(\d+)"/>', chain):
        try:
            fb = pr.block(ref.group(1))
        except KeyError:
            continue
        if P.TEXT_MATCHNAME not in fb:
            continue
        p0 = PARAM0_RE.search(fb)
        if not p0:
            return None
        try:
            pb = pr.block(p0.group(1))
        except KeyError:
            return None
        return fb, pb, p0.group(1)
    return None


def param_text(pb: str):
    """从一个参数块里取 (文本, 来源, 是否自闭合)。"""
    for h, closed, content in iter_values(pb):
        if closed or not content:
            return None, h, True
        blob = decode_b64(content)
        if blob is not None:
            got = tail_text(blob)
            if got:
                return got, h, False
        return None, h, False
    return None, None, False


def collect(pr, only_track=None):
    """按时间顺序返回全部字幕条目。"""
    h2t = hash_text_map(pr)
    tracks = track_map(pr)
    rows = []
    for ti in pr.objects_of("VideoClipTrackItem"):
        try:
            block = pr.block(ti.oid)
        except KeyError:
            continue
        ms = re.search(r"<Start>(-?\d+)</Start>", block)
        me = re.search(r"<End>(-?\d+)</End>", block)
        if not ms or not me:
            continue
        start = int(ms.group(1)) / P.TICKS_PER_SECOND
        end = int(me.group(1)) / P.TICKS_PER_SECOND
        track = tracks.get(ti.oid)
        if only_track is not None and track != only_track:
            continue

        loc = text_param_of(pr, block)
        if loc is None:
            continue  # 不是字幕条目（纯画面/素材片段），跳过

        _fb, pb, pid = loc
        text, h, closed = param_text(pb)
        source = "value" if text else "missing"
        if text is None and h and h in h2t:
            text, source = h2t[h], "hash"

        rows.append(
            {
                "ti": ti.oid,
                "param": pid,
                "track": track,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "source": source,
                "closed": closed,
                "hash": h,
            }
        )

    rows.sort(key=lambda r: (r["start"], r["end"]))
    for i, r in enumerate(rows, 1):
        r["no"] = i
    return rows


def fmt_tc(sec: float) -> str:
    sign = "-" if sec < 0 else ""
    sec = abs(sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return "%s%02d:%02d:%02d.%03d" % (sign, h, m, s, ms)


def skeleton(pr) -> dict:
    """工程骨架，纯展示用（严格判定交给 prverify.py）。"""
    def one(pat):
        m = re.search(pat, pr.xml)
        return m.group(1) if m else None

    seq_ver = None
    for o in pr.objects_of("Sequence"):
        b = pr.xml[o.start:o.end]
        mv = re.search(r'Version="(\d+)"', b[:200])
        if mv:
            seq_ver = mv.group(1)
            break

    fps = None
    mv = re.search(r"<VideoFrameRate>(\d+)</VideoFrameRate>", pr.xml)
    if mv:
        try:
            fps = round(P.TICKS_PER_SECOND / int(mv.group(1)), 3)
        except ZeroDivisionError:
            fps = None

    return {
        "premiere_data_version": one(r'<PremiereData Version="(\d+)"'),
        "project_version": one(r'<Project ObjectID="\d+"[^>]*Version="(\d+)"'),
        "sequence_version": seq_ver,
        "frame_rect": one(r"<FrameRect>([^<]+)</FrameRect>"),
        "fps": fps,
        "objects": len(pr.objects),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="从 .prproj 提取字幕文本与时间码")
    ap.add_argument("prproj", help="要读取的工程文件")
    ap.add_argument("--track", type=int, default=None, help="只取第 N 条视频轨（0 起）")
    ap.add_argument("--workdir", default=None, help="输出目录（默认输入所在目录）")
    ap.add_argument("--txt", default=None, help="文本清单输出路径")
    ap.add_argument("--json", default=None, help="详细 JSON 输出路径")
    ap.add_argument("--quiet", action="store_true", help="只打印摘要，不打印全部条目")
    args = ap.parse_args()

    pr = P.Prproj(args.prproj)
    rows = collect(pr, args.track)

    workdir = os.path.abspath(args.workdir or os.path.dirname(os.path.abspath(args.prproj)))
    txt_path = os.path.abspath(args.txt) if args.txt else os.path.join(workdir, "字幕清单.txt")
    json_path = os.path.abspath(args.json) if args.json else os.path.join(workdir, "字幕清单.json")

    os.makedirs(os.path.dirname(txt_path) or ".", exist_ok=True)
    with open(txt_path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(
                "%03d\t%s-%s\t%s\n"
                % (r["no"], fmt_tc(r["start"]), fmt_tc(r["end"]), r["text"] or "(解析失败)")
            )
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"source": args.prproj, "skeleton": skeleton(pr), "items": rows},
            fh,
            ensure_ascii=False,
            indent=1,
        )

    sk = skeleton(pr)
    by_src = {}
    for r in rows:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    tracks = sorted({r["track"] for r in rows if r["track"] is not None})
    unresolved = [r for r in rows if not r["text"]]

    print("工程：%s" % args.prproj)
    print("骨架：版本 %s/%s/%s｜画幅 %s｜帧率 %s｜对象 %s"
          % (sk["premiere_data_version"], sk["project_version"], sk["sequence_version"],
             sk["frame_rect"], sk["fps"], sk["objects"]))
    print("字幕条目：%d 条（视频轨 %s）" % (len(rows), tracks or "?"))
    if rows:
        print("时间范围：%s → %s" % (fmt_tc(rows[0]["start"]), fmt_tc(rows[-1]["end"])))
    print("取值来源：直接带值 %d｜同 hash 回填 %d｜解析失败 %d"
          % (by_src.get("value", 0), by_src.get("hash", 0), by_src.get("missing", 0)))
    closed = sum(1 for r in rows if r["closed"])
    if closed:
        print("注意：%d 条的文字参数是自闭合（靠 BinaryHash 引用），改字前必须先把它们独立化。"
              % closed)

    dup = {}
    for r in rows:
        if r["text"]:
            dup[r["text"]] = dup.get(r["text"], 0) + 1
    repeated = {k: v for k, v in dup.items() if v > 1}
    if repeated:
        print("重复文本：%d 种（最多 %d 次）—— 是真的说了两遍还是切条重复，要听音频判断"
              % (len(repeated), max(repeated.values())))

    if unresolved:
        print("\n解析失败（工程里可能没存文本，或用了未见的编码）：")
        for r in unresolved[:20]:
            print("  #%03d %s-%s" % (r["no"], fmt_tc(r["start"]), fmt_tc(r["end"])))
        if len(unresolved) > 20:
            print("  …共 %d 条" % len(unresolved))

    if not args.quiet:
        print("\n---- 字幕清单 ----")
        for r in rows:
            print("%03d\t%s-%s\t%s"
                  % (r["no"], fmt_tc(r["start"]), fmt_tc(r["end"]), r["text"] or "(解析失败)"))

    print("\n已写出：\n  %s\n  %s" % (txt_path, json_path))
    return 2 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
