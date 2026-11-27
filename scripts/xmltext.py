#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FCP7 XML（xmeml）里字幕文本的读取与改写。

Premiere 导出的 XML 里，一条字幕的「画面上的字」只有一处是真源：
`GraphicAndType` 效果里 `源文本` 参数的 base64 value。其余的
`clipitem/name`、`effect/name` 只是标签（用 syncnames.py 同步）。

本脚本做两件事：

    --extract        按顺序列出 序号 / 时间码 / 文本（文本以 base64 为准）
    --changes        把校对后的文本写回 base64，并顺手同步两处 name

带 `<hash>` 没有 value 的条目（同文本复用图形样式），提取时用同 hash 的
其它条目反查；改写时把 `<hash>` 换成 `<value>`，让它不再依赖别人。

默认只读原文件、另存新文件。

用法：
    python3 xmltext.py 唱词.xml --extract --txt 清单.txt
    python3 xmltext.py 唱词.xml --changes 改动.json -o 交付/唱词_校对.xml
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

import syncnames as S  # noqa: E402

START_RE = re.compile(r"<start>(-?\d+)</start>")
END_RE = re.compile(r"<end>(-?\d+)</end>")
TIMEBASE_RE = re.compile(r"<timebase>(\d+)</timebase>")


def read_xml(path: str):
    with open(path, "rb") as fh:
        raw = fh.read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw[3:].decode("utf-8") if bom else raw.decode("utf-8"), bom


def fmt_tc(frames: float, fps: float) -> str:
    sec = frames / fps if fps else 0
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return "%02d:%02d:%02d.%03d" % (h, m, s, ms)


def collect(xml: str, fps: float):
    """按文档顺序取每条字幕：文本（含 hash 回填）、起止帧、旧名。"""
    rows = []
    h2t = {}
    for m in S.ITEM_RE.finditer(xml):
        text, h, _old = S.probe(m.group(0))
        if text and h:
            h2t.setdefault(h, text)
    for m in S.ITEM_RE.finditer(xml):
        block = m.group(0)
        text, h, old = S.probe(block)
        if text is None and h:
            text = h2t.get(h)
        ms = START_RE.search(block)
        me = END_RE.search(block)
        rows.append(
            {
                "no": len(rows) + 1,
                "start": int(ms.group(1)) if ms else None,
                "end": int(me.group(1)) if me else None,
                "text": text,
                "hash": h,
                "name": old,
                "span": (m.start(), m.end()),
            }
        )
    return rows


def replace_text_in_effect(item: str, new_text: str):
    """把条目里「源文本」的 base64 换成新文本；只有 hash 的写成 value。返回 (新条目, 是否成功)。"""
    effect = S.graphic_effect(item)
    if effect is None:
        return item, False

    sm = S.SOURCE_TEXT_RE.search(effect)
    if sm:
        try:
            blob = base64.b64decode(re.sub(r"\s+", "", sm.group(1)))
        except Exception:
            return item, False
        old = S.decode_source_text(sm.group(1))
        if old is None:
            return item, False
        raw = old.encode("utf-8")
        off = blob.find(raw)
        if off < 4 or struct.unpack_from("<I", blob, off - 4)[0] != len(raw):
            return item, False
        new = bytearray(blob[:off] + new_text.encode("utf-8") + blob[off + len(raw):])
        pad = (-len(new)) % 4
        if pad:
            new += b"\x00" * pad
        struct.pack_into("<I", new, off - 4, len(new_text.encode("utf-8")))
        (head,) = struct.unpack_from("<I", new, 0)
        if head == len(blob) - 12:
            struct.pack_into("<I", new, 0, len(new) - 12)
        b64 = base64.b64encode(bytes(new)).decode("ascii")
        new_effect = effect[: sm.start(1)] + b64 + effect[sm.end(1):]
        return item.replace(effect, new_effect, 1), True

    # 只有 <hash>：换成 <value>，从此独立，不依赖别人
    hm = S.SOURCE_HASH_RE.search(effect)
    if hm:
        b64 = base64.b64encode(_make_blob(new_text)).decode("ascii")
        new_param = "<name>源文本</name><value>%s</value>" % b64
        start = effect.rfind("<parameter", 0, hm.start())
        end = effect.find("</parameter>", hm.end())
        if start == -1 or end == -1:
            return item, False
        tail = effect[end:]
        head = effect[start:hm.start()]
        # 保留 <parameter> 开标签与 <parameterid>
        return item.replace(effect, head + new_param + tail, 1), True

    return item, False


def _make_blob(text: str) -> bytes:
    """造一个最小可用的源文本 blob：magic + 版本头 + u32 长度 + 文本 + 补零。

    只用于「原本只有 hash」的条目 —— 这类条目自己在 XML 里没有完整 blob 可抄，
    造出来的只保证能解开文本，样式仍由 GraphicAndType 的名称与同组条目决定。
    """
    raw = text.encode("utf-8")
    body = bytearray()
    body += struct.pack("<II", 0x11223344, 0x10)   # magic 44332211 + 头
    body += b"\x00" * 8
    body += struct.pack("<I", len(raw))
    body += raw
    pad = (-len(body)) % 4
    body += b"\x00" * pad
    return bytes(body)


def main() -> int:
    ap = argparse.ArgumentParser(description="FCP7 XML 字幕文本的读取与改写")
    ap.add_argument("xml", help="xmeml 文件")
    ap.add_argument("--extract", action="store_true", help="列出每条字幕的文本与时间码")
    ap.add_argument("--changes", default=None, help="改动 JSON（序号 → 新文本）")
    ap.add_argument("--edits", default=None, help="改动 TSV（序号<TAB>新文本）")
    ap.add_argument("-o", "--out", default=None, help="输出 XML（默认不写）")
    ap.add_argument("--sync-names", action="store_true", default=True,
                    help="改文本时同步两处 name（默认开）")
    ap.add_argument("--no-sync-names", dest="sync_names", action="store_false")
    ap.add_argument("--txt", default=None, help="--extract 的输出文件")
    args = ap.parse_args()

    xml, bom = read_xml(args.xml)
    mt = TIMEBASE_RE.search(xml)
    fps = float(mt.group(1)) if mt else 25.0
    rows = collect(xml, fps)

    if args.extract or not args.changes and not args.edits:
        print("条目：%d｜timebase %s" % (len(rows), fps))
        lines = []
        for r in rows:
            tc = "%s-%s" % (fmt_tc(r["start"], fps), fmt_tc(r["end"], fps)) if r["start"] is not None else "-"
            line = "%03d\t%s\t%s" % (r["no"], tc, r["text"] or "(解析失败)")
            lines.append(line)
            print(line)
        if args.txt:
            with open(os.path.abspath(args.txt), "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            print("\n已写出：%s" % os.path.abspath(args.txt))
        bad = [r["no"] for r in rows if not r["text"]]
        if bad:
            print("解析失败：%s" % bad[:20], file=sys.stderr)
            return 2

    changes = {}
    if args.changes:
        with open(args.changes, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        items = data.get("changes", data) if isinstance(data, dict) else data
        if isinstance(items, dict):
            changes = {int(k): str(v) for k, v in items.items()}
        else:
            changes = {int(it["no"]): str(it["text"]) for it in items}
    if args.edits:
        with open(args.edits, encoding="utf-8-sig") as fh:
            for line in fh:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                changes[int(parts[0])] = parts[1]

    if args.changes or args.edits:
        if not args.out:
            print("改文本要给 -o 输出路径（不覆盖输入）", file=sys.stderr)
            return 3
        by_no = {r["no"]: r for r in rows}
        miss = [n for n in changes if n not in by_no]
        if miss:
            print("序号不存在：%s" % miss[:20], file=sys.stderr)
            return 3

        parts, cursor, log = [], 0, []
        for r in rows:
            new_text = changes.get(r["no"])
            start, end = r["span"]
            parts.append(xml[cursor:start])
            block = xml[start:end]
            if new_text is None or new_text == r["text"]:
                parts.append(block)
            else:
                new_block, ok = replace_text_in_effect(block, new_text)
                if not ok:
                    print("#%03d 写不进去（找不到源文本参数）" % r["no"], file=sys.stderr)
                    return 3
                if args.sync_names:
                    new_block = S.apply_names(new_block, new_text)
                parts.append(new_block)
                log.append((r["no"], r["text"], new_text))
            cursor = end
        parts.append(xml[cursor:])

        out_xml = "".join(parts)
        # 回读自检
        after = {x["no"]: x for x in collect(out_xml, fps)}
        bad = [(n, t, (after.get(n) or {}).get("text")) for n, t in changes.items()
               if (after.get(n) or {}).get("text") != t]
        print("改动：%d 处" % len(log))
        for no, old, new in log:
            print("  #%03d %s → %s" % (no, old, new))
        print("回读：文本不符 %d" % len(bad))
        for b in bad[:10]:
            print("  ! %s" % (b,))
        if bad:
            return 4

        data = (b"\xef\xbb\xbf" if bom else b"") + out_xml.encode("utf-8")
        with open(os.path.abspath(args.out), "wb") as fh:
            fh.write(data)
        print("已写出：%s" % os.path.abspath(args.out))

    return 0


if __name__ == "__main__":
    sys.exit(main())
