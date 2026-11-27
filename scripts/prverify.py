#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交付前校验：输出工程和模版工程是不是真的一致。

一条字幕在 .prproj 里的样式并不写在 XML 属性上，而是编码在「源文本」
参数的 base64 blob 里 —— 字体名、字号、描边、填充、对齐、行距全在
那段二进制里。只看 XML 是查不出字体被换的。

所以这里做四件事：

  A 骨架      版本号 / 画幅 / 帧率 与模版严格相等
              （PremiereData、Project、Sequence 三个 Version 都要对）
  B 逐对象    抹掉「允许变的东西」后，每个对象块的 sha1 必须和对照工程一致。
              允许变：源文本值、InstanceName、SubClip/MasterClip 的 Name。
              其他任何一个字节变了都会报出来，并指出是哪个对象。
  C 字体      从 blob 里抽出字体名，输出的字体集合不许超出模版
  D 位置      「位置」参数 x/y 的分布要落在模版的范围内

B 需要 --against（同一工程改文字的场景，最严格）。
只给 --template（从模版生成新工程的场景）时，做 A/C/D + 完整性。

用法：
    python3 prverify.py 输出.prproj --template 模版.prproj --against 源工程.prproj
    python3 prverify.py 输出.prproj --template 模版.prproj --md 校验.md --json 校验.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prproj as P  # noqa: E402
import prtext as T  # noqa: E402

SV_RE = re.compile(r"<StartKeyframeValue\b[^>]*(?:/>|>.*?</StartKeyframeValue>)", re.S)
IN_RE = re.compile(r"<InstanceName>.*?</InstanceName>", re.S)
NAME_RE = re.compile(r"<Name>.*?</Name>", re.S)
FONT_RE = re.compile(r"[A-Za-z][A-Za-z0-9 _\-\.\+]{2,}")
POS_RE = re.compile(r"<PointComponentParam[^>]*>\s*<Name>位置</Name>.*?<StartKeyframe>([^<]*)</StartKeyframe>", re.S)


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def scrub(pr, obj) -> str:
    """抹掉允许变化的内容，留下「应该一模一样」的部分。"""
    block = pr.xml[obj.start:obj.end]
    block = SV_RE.sub("<SV/>", block)
    block = IN_RE.sub("<IN/>", block)
    if obj.tag in ("SubClip", "MasterClip", "Clip"):
        block = NAME_RE.sub("<N/>", block, count=1)
    return block


def skeleton(pr) -> dict:
    def one(pat, default=None):
        m = re.search(pat, pr.xml)
        return m.group(1) if m else default

    seq_vers = []
    for o in pr.objects_of("Sequence"):
        b = pr.xml[o.start:o.end]
        m = re.search(r'Version="(\d+)"', b[:200])
        if m:
            seq_vers.append(m.group(1))

    fps = None
    m = re.search(r"<VideoFrameRate>(\d+)</VideoFrameRate>", pr.xml)
    if m:
        try:
            fps = round(P.TICKS_PER_SECOND / int(m.group(1)), 3)
        except ZeroDivisionError:
            pass

    return {
        "premiere_data_version": one(r'<PremiereData Version="(\d+)"'),
        "project_version": one(r'<Project ObjectID="\d+"[^>]*Version="(\d+)"'),
        "sequence_version": seq_vers[0] if seq_vers else None,
        "sequence_count": len(seq_vers),
        "frame_rect": one(r"<FrameRect>([^<]+)</FrameRect>"),
        "fps": fps,
        "objects": len(pr.objects),
    }


def blob_fonts(blob: bytes):
    """blob 里形如「u32 长度 + ASCII 名字」的字体族名。"""
    out = set()
    n = len(blob)
    for i in range(0, n - 4):
        (ln,) = struct.unpack_from("<I", blob, i)
        if ln < 4 or ln > 80 or i + 4 + ln > n:
            continue
        try:
            s = blob[i + 4:i + 4 + ln].decode("ascii")
        except UnicodeDecodeError:
            continue
        if not FONT_RE.fullmatch(s):
            continue
        # 字体名一般够长，且带厂商/字重的痕迹；太短的当噪声
        if len(s) >= 6 and re.search(r"[-_ ]|Regular|Bold|Light|Medium|Black|Italic|Semi", s):
            out.add(s)
    return out


def font_inventory(pr):
    inv = {}
    for r in T.collect(pr):
        loc = T.text_param_of(pr, pr.block(r["ti"]))
        if loc is None:
            continue
        _fb, pb, _pid = loc
        blob = None
        vm = re.search(r">([^<]+)</StartKeyframeValue>", pb, re.S)
        if vm:
            blob = T.decode_b64(vm.group(1))
        if blob is None:
            continue
        for f in blob_fonts(blob):
            inv[f] = inv.get(f, 0) + 1
    return inv


def positions(pr):
    out = []
    for m in POS_RE.finditer(pr.xml):
        for v in m.group(1).split(",")[1:2]:
            for part in v.split(":"):
                try:
                    out.append(float(part))
                except ValueError:
                    pass
    return out


def item_blob(pr, row):
    """取一条字幕的 (blob, 文本起始偏移, 文本字节长度)。"""
    loc = T.text_param_of(pr, pr.block(row["ti"]))
    if loc is None:
        return None
    _fb, pb, _pid = loc
    text = row["text"]
    if not text:
        return None
    raw = text.encode("utf-8")
    vm = re.search(r">([^<]+)</StartKeyframeValue>", pb, re.S)
    if vm:
        blob = T.decode_b64(vm.group(1))
    else:
        blob = None
        h = row.get("hash")
        if h:
            m = re.search(
                r'BinaryHash="%s"[^>]*>([^<]+)</StartKeyframeValue>' % re.escape(h), pr.xml
            )
            if m:
                blob = T.decode_b64(m.group(1))
    if blob is None:
        return None
    off = P._find_text_bytes(blob, raw)
    if off is None:
        got = P._guess_text_tail(blob)
        if got and got[0] == text:
            off = got[1]
    if off is None:
        return None
    return blob, off, len(raw)


def style_sigs(pr) -> dict:
    """每条字幕的「样式指纹」：blob 里抹掉长度头、文本和尾部补零后剩下的字节。

    剩下的部分是字体名、字号、描边、填充、对齐、行距这些真正决定长相的
    参数。只改文字的话，这一段的字节一个都不该变 —— 所以它既能查出字体
    被换，也能查出描边、字号被改（这两样在 XML 里根本看不见）。
    """
    out = {}
    for row in T.collect(pr):
        got = item_blob(pr, row)
        if got is None:
            continue
        blob, off, ln = got
        stripped = blob[4:off - 4] + blob[off + ln:].rstrip(b"\x00")
        out[row["ti"]] = hashlib.sha1(stripped).hexdigest()[:12]
    return out


def compare_objects(pr_out, pr_ref):
    """逐对象比 scrub 后的 sha1，返回差异列表。"""
    a = {o.oid: o for o in pr_ref.objects}
    b = {o.oid: o for o in pr_out.objects}
    only_ref = sorted(set(a) - set(b))
    only_out = sorted(set(b) - set(a))
    diffs = []
    for oid in sorted(set(a) & set(b)):
        if a[oid].tag != b[oid].tag:
            diffs.append({"oid": oid, "tag": a[oid].tag, "why": "对象类型变了"})
            continue
        if sha1(scrub(pr_ref, a[oid])) != sha1(scrub(pr_out, b[oid])):
            diffs.append({"oid": oid, "tag": a[oid].tag, "why": "内容有非预期改动"})
    return {"only_in_ref": only_ref, "only_in_out": only_out, "changed": diffs}


def main() -> int:
    ap = argparse.ArgumentParser(description="交付前校验：工程与模版是否一致")
    ap.add_argument("prproj", help="要校验的输出工程")
    ap.add_argument("--template", default=None, help="模版工程（校验版本号/画幅/字体/位置）")
    ap.add_argument("--against", default=None, help="对照工程（同一工程改文本前的版本，做字节级比对）")
    ap.add_argument("--json", default=None, help="校验结果 JSON")
    ap.add_argument("--md", default=None, help="校验报告 Markdown")
    ap.add_argument("--max-pos-drift", type=float, default=0.02,
                    help="位置允许的最大偏移（占画面比例，默认 0.02）")
    args = ap.parse_args()

    out = P.Prproj(args.prproj)
    sk_out = skeleton(out)
    result = {"file": args.prproj, "skeleton": sk_out, "checks": {}, "ok": True}
    lines = []
    fails = []

    def add(key, ok, detail, note=""):
        result["checks"][key] = {"ok": ok, "detail": detail, "note": note}
        if not ok:
            fails.append(key)

    # ---------- 自身完整性 ----------
    integ = out.check_integrity()
    add("integrity", not integ.get("dangling_refs") and not integ.get("duplicate_ids"),
        integ, "悬空引用 / 重复对象 ID")

    rows = T.collect(out)
    unreadable = [r["no"] for r in rows if not r["text"]]
    add("readable", not unreadable, {"items": len(rows), "unreadable": unreadable},
        "每条字幕都要能读出文字")

    # ---------- A 骨架 ----------
    if args.template:
        tpl = P.Prproj(args.template)
        sk_tpl = skeleton(tpl)
        result["template_skeleton"] = sk_tpl
        keys = ["premiere_data_version", "project_version", "sequence_version", "frame_rect", "fps"]
        mismatch = {k: {"template": sk_tpl.get(k), "output": sk_out.get(k)}
                    for k in keys if sk_tpl.get(k) != sk_out.get(k)}
        add("skeleton", not mismatch, {"mismatch": mismatch},
            "版本号 / 画幅 / 帧率必须和模版一致")
    else:
        tpl = None
        add("skeleton", False, {"reason": "没有给 --template"}, "无法校验模版版本")

    # ---------- B 逐对象 ----------
    if args.against:
        ref = P.Prproj(args.against)
        cmp = compare_objects(out, ref)
        ok = not cmp["only_in_ref"] and not cmp["only_in_out"] and not cmp["changed"]
        summary = {
            "changed": len(cmp["changed"]),
            "only_in_ref": len(cmp["only_in_ref"]),
            "only_in_out": len(cmp["only_in_out"]),
            "sample": cmp["changed"][:15],
        }
        add("objects", ok, summary,
            "抹掉源文本/图层名/片段名后，每个对象必须和对照工程逐字节相同")
        result["object_diff"] = cmp

        # 样式指纹：同一时间线条目改文字前后，blob 里的样式字节必须一模一样
        sig_out = style_sigs(out)
        sig_ref = style_sigs(ref)
        common = set(sig_out) & set(sig_ref)
        style_bad = sorted(ti for ti in common if sig_out[ti] != sig_ref[ti])
        missing = len(set(sig_ref) - common)
        add("style", not style_bad,
            {"compared": len(common), "diff": len(style_bad),
             "sample": style_bad[:10], "unmatched_items": missing},
            "字体/字号/描边等样式字节必须和对照工程一致")
    else:
        add("objects", True, {"skipped": True}, "未提供 --against，跳过字节级比对")
        if tpl is not None:
            # 没有对照工程时退一步：输出的每一套样式都必须来自模版
            sig_out = style_sigs(out)
            sig_tpl = style_sigs(tpl)
            extra = sorted(set(sig_out.values()) - set(sig_tpl.values()))
            add("style", not extra,
                {"mode": "集合比对（较弱）", "output_kinds": len(set(sig_out.values())),
                 "template_kinds": len(set(sig_tpl.values())), "unexpected": extra[:10]},
                "输出的样式指纹必须都来自模版")
        else:
            add("style", True, {"skipped": True}, "无模版也无对照工程，跳过样式比对")

    # ---------- C 字体 ----------
    inv_out = font_inventory(out)
    result["fonts"] = inv_out
    if tpl is not None:
        inv_tpl = font_inventory(tpl)
        result["template_fonts"] = inv_tpl
        extra = sorted(set(inv_out) - set(inv_tpl))
        add("fonts", not extra, {"output": sorted(inv_out), "template": sorted(inv_tpl),
                                 "unexpected": extra},
            "输出的字体不许超出模版用过的字体")
    else:
        add("fonts", True, {"skipped": True, "output": sorted(inv_out)}, "无模版，仅列出字体")

    # ---------- D 位置 ----------
    pos_out = positions(out)
    if tpl is not None and pos_out:
        pos_tpl = positions(tpl)
        if pos_tpl:
            lo_t, hi_t = min(pos_tpl), max(pos_tpl)
            drift = max(0.0, lo_t - min(pos_out), max(pos_out) - hi_t)
            add("position", drift <= args.max_pos_drift,
                {"template_range": [round(lo_t, 4), round(hi_t, 4)],
                 "output_range": [round(min(pos_out), 4), round(max(pos_out), 4)],
                 "max_drift": round(drift, 4), "tolerance": args.max_pos_drift},
                "字幕位置不许整体被挪走")
        else:
            add("position", True, {"skipped": True}, "模版里没找到位置参数")
    else:
        add("position", True, {"skipped": True}, "无模版或读不到位置")

    result["ok"] = not fails

    # ---------- 报告 ----------
    lines.append("# 工程校验报告\n")
    lines.append("- 输出工程：`%s`" % os.path.basename(args.prproj))
    if args.template:
        lines.append("- 模版工程：`%s`" % os.path.basename(args.template))
    if args.against:
        lines.append("- 对照工程：`%s`" % os.path.basename(args.against))
    lines.append("- 结论：**%s**\n" % ("通过" if result["ok"] else "未通过"))
    lines.append("| 检查 | 结果 | 说明 |\n|---|---|---|")
    label = {"integrity": "引用完整性", "readable": "文本可读", "skeleton": "版本/画幅/帧率",
             "objects": "逐对象字节比对", "style": "样式字节（字体/描边）",
             "fonts": "字体集合", "position": "位置分布"}
    for k, v in result["checks"].items():
        lines.append("| %s | %s | %s |" % (label.get(k, k), "通过" if v["ok"] else "**不通过**", v["note"]))

    lines.append("\n## 骨架\n")
    lines.append("| 项 | 模版 | 输出 |\n|---|---|---|")
    for k in ["premiere_data_version", "project_version", "sequence_version", "frame_rect", "fps"]:
        lines.append("| %s | %s | %s |" % (k, (result.get("template_skeleton") or {}).get(k, "-"),
                                           sk_out.get(k)))
    if result["fonts"]:
        lines.append("\n## 字体\n")
        for f, n in sorted(result["fonts"].items(), key=lambda x: -x[1]):
            lines.append("- `%s`（%d 条）" % (f, n))
    if result.get("object_diff", {}).get("changed"):
        lines.append("\n## 非预期改动的对象（前 15 个）\n")
        lines.append("| 对象 ID | 类型 | 问题 |\n|---|---|---|")
        for d in result["object_diff"]["changed"][:15]:
            lines.append("| %s | %s | %s |" % (d["oid"], d["tag"], d["why"]))

    md = "\n".join(lines)
    print(md)

    if args.md:
        with open(os.path.abspath(args.md), "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
    if args.json:
        with open(os.path.abspath(args.json), "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)

    if not args.template:
        print("\n没有给 --template，模版版本与字体一致性未校验。", file=sys.stderr)
        return 2
    return 0 if result["ok"] else 5


if __name__ == "__main__":
    sys.exit(main())
