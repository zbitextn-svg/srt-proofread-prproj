#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 FCP7 XML 里每条字幕的「剪辑名」同步成画面真实文本。

背景
----
一条 Premiere 字幕在 FCP7 XML 里有三份文字：

  1. clipitem/name                      —— 剪辑名（时间线上显示的标签）
  2. effect(GraphicAndType)/name        —— 图形层的名字
  3. effect(GraphicAndType)/parameter「源文本」的 base64 value
                                        —— 真正被渲染的字（唯一真源）

在 Premiere 里双击改字，改的只有第 3 份；1 和 2 是图层/剪辑名，不会跟着变。
所以导出 XML 后常看到「name 还是旧字、画面却是新字」。

本脚本以第 3 份为唯一真源，反写 1 和 2，让三处对齐。

两种数据形态都能处理：
  · 直接带 value 的条目          → 解 base64 拿文本
  · 只带 <hash> 的条目（同文本复用图形样式）
                                → 用同 hash 的其它条目反查文本，无需手工补

默认只读原文件、另存新文件，不覆盖输入。

用法
----
    python3 syncnames.py 唱词.xml
    python3 syncnames.py 唱词.xml -o 唱词_同步.xml

退出码：0 全部对齐，2 有条目无法定位文本（原样保留，会列出）。
"""

from __future__ import annotations

import argparse
import base64
import html
import os
import re
import struct
import sys
from typing import Dict, List, Optional, Tuple

ITEM_RE = re.compile(r"<clipitem\b.*?</clipitem>", re.S)
EFFECT_RE = re.compile(r"<effect>(?:(?!</effect>).)*?</effect>", re.S)
SOURCE_TEXT_RE = re.compile(
    r"<parameter\b[^>]*>\s*<parameterid>1</parameterid>\s*<name>源文本</name>.*?<value>([A-Za-z0-9+/=\s]+)</value>",
    re.S,
)
SOURCE_HASH_RE = re.compile(r"<name>源文本</name>\s*<hash>([0-9a-fA-F\-]+)</hash>")


def decode_source_text(b64: str) -> Optional[str]:
    """base64 → 真实文本。文本在 blob 尾部，前面 4 字节是它的字节长度。

    注意：blob 末尾可能带 1~3 个 \\x00 对齐填充，所以不要求文本正好顶到结尾。
    """
    try:
        blob = base64.b64decode(re.sub(r"\s+", "", b64))
    except Exception:
        return None
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
        if text.strip() and all(ch.isprintable() or ch in "\n\t" for ch in text):
            return text
    return None


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def set_nth_name(block: str, n: int, text: str) -> Tuple[str, bool]:
    """把 block 里第 n 个（1 起）<name>…</name> 换成 text。返回 (新块, 是否改动)。"""
    cnt = 0
    for m in re.finditer(r"<name>(.*?)</name>", block, re.S):
        cnt += 1
        if cnt != n:
            continue
        if html.unescape(m.group(1)) == text:
            return block, False
        return block[: m.start(1)] + esc(text) + block[m.end(1):], True
    return block, False


def graphic_effect(item: str) -> Optional[str]:
    for m in EFFECT_RE.finditer(item):
        if "GraphicAndType" in m.group(0):
            return m.group(0)
    return None


def probe(item: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """返回 (文本, 源文本 hash, 旧剪辑名)。文本可能为 None（只有 hash 引用）。"""
    effect = graphic_effect(item)
    nm = re.search(r"<name>(.*?)</name>", item, re.S)
    old_name = html.unescape(nm.group(1)) if nm else None
    if effect is None:
        return None, None, old_name

    hm = SOURCE_HASH_RE.search(effect)
    h = hm.group(1) if hm else None

    sm = SOURCE_TEXT_RE.search(effect)
    text = decode_source_text(sm.group(1)) if sm else None
    return text, h, old_name


def apply_names(item: str, text: str) -> str:
    """把 clipitem 的剪辑名与图形层名都改成 text。"""
    item, _ = set_nth_name(item, 1, text)
    effect = graphic_effect(item)
    if effect is not None:
        new_effect, changed = set_nth_name(effect, 1, text)
        if changed:
            item = item.replace(effect, new_effect, 1)
    return item


def main() -> int:
    ap = argparse.ArgumentParser(description="同步 FCP7 XML 中字幕的剪辑名与画面文本")
    ap.add_argument("xml", help="输入的 xmeml / FCP7 XML")
    ap.add_argument("-o", "--out", default=None, help="输出路径，默认 <输入>_name同步.xml")
    ap.add_argument("--limit-preview", type=int, default=10, help="最多打印多少条差异样例")
    ap.add_argument("--report", default=None, help="另存一份 Markdown 差异清单")
    args = ap.parse_args()

    src = os.path.abspath(args.xml)
    if not os.path.isfile(src):
        print("找不到文件：%s" % src, file=sys.stderr)
        return 1
    out = args.out or os.path.splitext(src)[0] + "_name同步.xml"
    if os.path.abspath(out) == src:
        print("拒绝覆盖输入文件，请用 -o 指定别的名字", file=sys.stderr)
        return 1

    raw = open(src, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        print("不是 UTF-8 编码的 XML，暂不支持", file=sys.stderr)
        return 1

    spans = list(ITEM_RE.finditer(s))
    if not spans:
        print("没有找到 clipitem，确认这是唱词时间线 XML", file=sys.stderr)
        return 1

    # 第一遍：收集 hash → 文本（同一图形样式被多个条目复用时靠它补全）
    hash2text: Dict[str, str] = {}
    probes: List[Tuple[Optional[str], Optional[str], Optional[str]]] = []
    for m in spans:
        text, h, old = probe(m.group(0))
        probes.append((text, h, old))
        if text and h:
            hash2text.setdefault(h, text)

    # 第二遍：定文本 → 回写两个 name
    parts, cursor, diffs, unresolved = [], 0, [], []
    for no, (m, (text, h, old)) in enumerate(zip(spans, probes), 1):
        parts.append(s[cursor:m.start()])
        if text is None and h:
            text = hash2text.get(h)
        if text is None:
            unresolved.append((no, old or "(无 name)"))
            parts.append(m.group(0))
        else:
            if old is not None and old.strip() != text.strip():
                diffs.append((no, old, text))
            parts.append(apply_names(m.group(0), text))
        cursor = m.end()
    parts.append(s[cursor:])

    data = (b"\xef\xbb\xbf" if bom else b"") + "".join(parts).encode("utf-8")
    with open(out, "wb") as f:
        f.write(data)

    print("条目总数：%d" % len(spans))
    print("剪辑名需要修正：%d" % len(diffs))
    for no, old, new in diffs[: args.limit_preview]:
        print("  #%d %s  ->  %s" % (no, old, new))
    if len(diffs) > args.limit_preview:
        print("  …其余 %d 条见输出文件" % (len(diffs) - args.limit_preview))
    if unresolved:
        print("找不到画面文本：%d 条（原样保留）" % len(unresolved))
        for no, name in unresolved[: args.limit_preview]:
            print("  ! #%d %s" % (no, name))
    print("已写入：%s" % out)

    if args.report:
        lines = [
            "# 剪辑名同步清单",
            "",
            "- 源文件：`%s`" % os.path.basename(src),
            "- 条目总数：%d" % len(spans),
            "- 剪辑名修正：%d" % len(diffs),
            "- 无法定位文本：%d" % len(unresolved),
            "",
            "画面文本为真源，下表是把「时间线标签」改成与画面一致的记录。",
            "时间码与样式未做任何改动。",
            "",
            "| # | 原 name | 改为 |",
            "|---|---|---|",
        ]
        for no, old, new in diffs:
            lines.append("| %d | %s | %s |" % (no, old.replace("|", "\\|"), new.replace("|", "\\|")))
        if unresolved:
            lines += ["", "## 未能定位文本的条目", "", "| # | name |", "|---|---|"]
            for no, name in unresolved:
                lines.append("| %d | %s |" % (no, name.replace("|", "\\|")))
        with open(args.report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("清单已写入：%s" % args.report)

    return 2 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
