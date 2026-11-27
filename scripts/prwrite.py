#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在一个已有的 .prproj 里改字幕文字，样式、时间码、对象结构全部保持不动。

这是「路线 B：直接拿工程来校对」的第三步（prtext → 人工校对 → prwrite）。

只碰四个地方，其余字节原样保留：
  1. 文本组件「源文本」参数里的 base64 blob（重算长度前缀、4 字节对齐）
  2. `<InstanceName>`（图层名）
  3. `SubClip/Name`
  4. `MasterClip/Name`

有一个坑必须先处理：工程里同一段文字可能只存一份，其余条目用
`<StartKeyframeValue ... BinaryHash="X"/>` 引用它（自闭合，没有值）。
如果直接改那个带值的条目，所有引用同一 hash 的条目会一起变 —— 或者
引用失配变空白。所以脚本先把要改的条目「独立化」（复制一份该 hash 的
blob、换新 hash、写进本条），再改字。

默认 dry-run，只打印要改什么。加 --apply 才写文件。

用法：
    python3 prwrite.py 工程.prproj --changes 改动.json --out 交付/校对版.prproj --workdir 交付
    python3 prwrite.py 工程.prproj --edits 改动.tsv --out 交付/校对版.prproj --apply
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prproj as P  # noqa: E402
import prtext as T  # noqa: E402


# ----------------------------------------------------------------- 读改动

def load_changes(changes_path=None, edits_path=None):
    """返回 [(no, 新文本, 原文或 None, 依据)]。"""
    out = []
    if changes_path:
        with open(changes_path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        items = data.get("changes", data) if isinstance(data, dict) else data
        if isinstance(items, dict):
            for k, v in items.items():
                out.append((int(k), str(v), None, ""))
        else:
            for it in items:
                out.append(
                    (
                        int(it.get("no")),
                        str(it.get("text", "")),
                        it.get("from"),
                        it.get("reason", ""),
                    )
                )
    if edits_path:
        with open(edits_path, encoding="utf-8-sig") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    raise SystemExit("改动清单第 %d 行不是「序号<TAB>新文本」：%r" % (lineno, line))
                try:
                    no = int(parts[0].strip())
                except ValueError:
                    raise SystemExit("改动清单第 %d 行序号不是数字：%r" % (lineno, parts[0]))
                out.append((no, parts[1], None, parts[2] if len(parts) > 2 else ""))
    out.sort(key=lambda x: x[0])
    if not out:
        raise SystemExit("没有任何改动：请给 --changes 或 --edits")
    return out


# ----------------------------------------------------------------- blob 操作

def replace_text(blob: bytes, off: int, old_len: int, text: str) -> bytes:
    """换掉 blob 里的文本，重算长度前缀与 4 字节对齐。"""
    out = bytearray(P._replace_text_blob(blob, off, old_len, text))
    pad = (-len(out)) % 4
    if pad:
        out += b"\x00" * pad
    return bytes(out)


def set_value(param_block: str, blob: bytes, new_hash: str) -> str:
    """把参数块里的 StartKeyframeValue 换成一个带值的、指定 hash 的标签。"""
    return re.sub(
        r"<StartKeyframeValue\b[^>]*(?:/>|>.*?</StartKeyframeValue>)",
        lambda _m: value_tag(blob, new_hash),
        param_block,
        count=1,
        flags=re.S,
    )


def value_tag(blob: bytes, h: str) -> str:
    return '<StartKeyframeValue Encoding="base64" BinaryHash="%s">%s</StartKeyframeValue>' % (
        h,
        base64.b64encode(blob).decode("ascii"),
    )


def closed_ref_spans(pr, h: str, exclude=()):
    """所有引用 hash h 的自闭合 StartKeyframeValue 的位置（跳过 exclude 区间）。"""
    pat = re.compile(r'<StartKeyframeValue\b[^>]*BinaryHash="%s"[^>]*/>' % re.escape(h))
    out = []
    for m in pat.finditer(pr.xml):
        if any(a <= m.start() < b for a, b in exclude):
            continue
        out.append((m.start(), m.end()))
    return out


def ref_count(pr, tag: str, attr: str, uid: str) -> int:
    return len(re.findall(r'<%s %s="%s"\s*/>' % (tag, attr, re.escape(uid)), pr.xml))


def hash_refs(pr, h: str) -> int:
    """一个 BinaryHash 在全文出现几次（定义 + 自闭合引用都算）。>1 就是共用。"""
    return len(re.findall(r'BinaryHash="%s"' % re.escape(h), pr.xml))


# ----------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="在 .prproj 里改字幕文字，保留全部样式")
    ap.add_argument("prproj", help="源工程（不会被修改）")
    ap.add_argument("--changes", default=None, help="改动 JSON")
    ap.add_argument("--edits", default=None, help="改动 TSV（序号<TAB>新文本[<TAB>依据]）")
    ap.add_argument("--out", required=True, help="输出工程路径")
    ap.add_argument("--workdir", default=None, help="写入白名单根目录（默认输入所在目录）")
    ap.add_argument("--report", default=None, help="修改清单 Markdown 输出路径")
    ap.add_argument("--apply", action="store_true", help="真的写文件（默认 dry-run）")
    ap.add_argument("--approve-overwrite", action="store_true", help="允许覆盖已存在的非本工具输出")
    args = ap.parse_args()

    changes = load_changes(args.changes, args.edits)
    pr = P.Prproj(args.prproj)
    rows = T.collect(pr)
    by_no = {r["no"]: r for r in rows}

    # ---- 1. 校验改动清单能不能落地 ----
    problems = []
    for no, new_text, from_text, _reason in changes:
        r = by_no.get(no)
        if r is None:
            problems.append("#%03d 不存在（本工程共 %d 条）" % (no, len(rows)))
            continue
        if not r["text"]:
            problems.append("#%03d 当前读不到文字，无法安全替换" % no)
            continue
        if from_text is not None and from_text.strip() != r["text"].strip():
            problems.append(
                "#%03d 原文对不上：清单写《%s》，工程里是《%s》" % (no, from_text, r["text"])
            )
        if not new_text.strip():
            problems.append("#%03d 新文本为空（要删条目请手动在 PR 里删，不要给空文字）" % no)
    if problems:
        print("改动清单有问题，先修清单：", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 3

    # ---- 2. 收集要改的块（按位置，从后往前替换，避免位置漂移） ----
    h2blob = {}
    for h, closed, content in T.iter_values(pr.xml):
        if closed or not content:
            continue
        blob = T.decode_b64(content)
        if blob is not None:
            h2blob.setdefault(h, blob)

    edits = []      # (start, end, new_block, 说明)
    log = []
    # 所有本轮要改的条目所在参数块，拆分共用引用时要跳过它们（它们会自己写成独立值）
    change_spans = []
    for no in {c[0] for c in changes}:
        pid = (by_no.get(no) or {}).get("param")
        o = pr._by_id.get(pid) if pid else None
        if o is not None:
            change_spans.append((o.start, o.end))

    for no, new_text, _from_text, reason in changes:
        r = by_no[no]
        ti = r["ti"]
        ti_block = pr.block(ti)
        loc = T.text_param_of(pr, ti_block)
        if loc is None:
            print("#%03d 找不到文本参数" % no, file=sys.stderr)
            return 3
        fb, pb, pid = loc

        # 2a. 取当前 blob；自闭合就从同 hash 借一份
        blob = None
        vm = re.search(r">([^<]+)</StartKeyframeValue>", pb, re.S)
        if vm:
            blob = T.decode_b64(vm.group(1))
        if blob is None and r["hash"]:
            blob = h2blob.get(r["hash"])
        if blob is None:
            print("#%03d 拿不到源 blob，无法改" % no, file=sys.stderr)
            return 3

        # 2b. 定位旧文本字节
        old = r["text"]
        raw = old.encode("utf-8")
        off = P._find_text_bytes(blob, raw)
        if off is None:
            got = P._guess_text_tail(blob)
            if got and got[0] == old:
                off = got[1]
        if off is None:
            print("#%03d 在 blob 里定位不到《%s》" % (no, old), file=sys.stderr)
            return 3
        new_blob = replace_text(blob, off, len(raw), new_text)
        if T.tail_text(new_blob) != new_text:
            print("#%03d 写入后回读不符：%r" % (no, T.tail_text(new_blob)), file=sys.stderr)
            return 3

        # 2c. hash：只被这一条用的就沿用，共用的必须处理，否则会串改
        obj = pr._by_id.get(pid)
        if obj is None:
            print("#%03d 参数对象定位失败" % no, file=sys.stderr)
            return 3
        old_hash = r["hash"]
        shared = bool(old_hash) and hash_refs(pr, old_hash) > 1
        if shared and not r["closed"]:
            # 本条就是那个「带值的定义」，别处还有自闭合引用者。
            # 直接改它会让所有引用者一起变（或失去定义变空白），
            # 所以先把引用者各自剥离成独立的一份旧内容。
            for s, e in closed_ref_spans(pr, old_hash, exclude=change_spans):
                edits.append((s, e, value_tag(blob, str(uuid.uuid4())), "#%03d 拆分共用引用" % no))
        new_hash = str(uuid.uuid4()) if shared else (old_hash or str(uuid.uuid4()))

        edits.append((obj.start, obj.end, set_value(pb, new_blob, new_hash), "#%03d 源文本" % no))

        # 2d. 图层名
        fobj = None
        cref = re.search(r'<Components ObjectRef="(\d+)"/>', ti_block)
        if cref:
            for ref in re.finditer(r'<Component Index="\d+" ObjectRef="(\d+)"/>',
                                   pr.block(cref.group(1))):
                cand = pr._by_id.get(ref.group(1))
                if cand and P.TEXT_MATCHNAME in pr.block(cand.oid):
                    fobj = cand
                    break
        if fobj is not None:
            new_fb = P._set_instance_name(pr.block(fobj.oid), new_text)
            if new_fb != pr.block(fobj.oid):
                edits.append((fobj.start, fobj.end, new_fb, "#%03d 图层名" % no))

        # 2e. SubClip / MasterClip 名（共用时跳过，避免串改）
        sm = re.search(r'<SubClip ObjectRef="(\d+)"/>', ti_block)
        if sm:
            sid = sm.group(1)
            sobj = pr._by_id.get(sid)
            if sobj is not None and ref_count(pr, "SubClip", "ObjectRef", sid) <= 1:
                sub = pr.block(sid)
                sub2 = re.sub(
                    r"<Name>.*?</Name>",
                    lambda _m: "<Name>%s</Name>" % P._xml_escape(new_text),
                    sub,
                    count=1,
                    flags=re.S,
                )
                if sub2 != sub:
                    edits.append((sobj.start, sobj.end, sub2, "#%03d 片段名" % no))

                mu = re.search(r'<MasterClip ObjectURef="([^"]+)"/>', sub)
                if mu:
                    uid = mu.group(1)
                    if ref_count(pr, "MasterClip", "ObjectURef", uid) <= 1:
                        mobj = pr._by_obj.get(uid)
                        if mobj is not None:
                            mast = pr.xml[mobj.start:mobj.end]
                            mast2 = re.sub(
                                r"<Name>.*?</Name>",
                                lambda _m: "<Name>%s</Name>" % P._xml_escape(new_text),
                                mast,
                                count=1,
                                flags=re.S,
                            )
                            if mast2 != mast:
                                edits.append((mobj.start, mobj.end, mast2, "#%03d 素材名" % no))

        log.append((no, r["start"], r["end"], old, new_text, reason))

    # ---- 3. 从后往前落盘 ----
    xml = pr.xml
    for start, end, new_block, _desc in sorted(edits, key=lambda e: -e[0]):
        xml = xml[:start] + new_block + xml[end:]

    pr.xml = xml
    pr._index()

    # ---- 4. 回读校验 ----
    after = {r["no"]: r for r in T.collect(pr)}
    want = {no: text for no, text, _f, _r in changes}
    bad = []
    for no, text in want.items():
        got = (after.get(no) or {}).get("text")
        if got != text:
            bad.append((no, text, got))
    unresolved = [r["no"] for r in after.values() if not r["text"]]
    moved = [
        no
        for no in after
        if no in by_no and (after[no]["start"], after[no]["end"]) != (by_no[no]["start"], by_no[no]["end"])
    ]

    print("条目总数：%d" % len(rows))
    print("改动：%d 处（涉及 %d 个对象块）" % (len(log), len(edits)))
    for no, start, _end, old, new, reason in log:
        print("  #%03d %s  %s → %s%s"
              % (no, T.fmt_tc(start), old, new, ("   [" + reason + "]") if reason else ""))
    print("回读：文本不符 %d｜读不到文字 %d｜时间码变化 %d" % (len(bad), len(unresolved), len(moved)))
    for row in bad[:10]:
        print("  ! #%03d 期望《%s》，实际《%s》" % row)
    if moved:
        print("  ! 时间码有变化，这不该发生：%s" % moved[:10])

    ok = not bad and not unresolved and not moved
    if not ok:
        print("\n校验没过，不写文件。", file=sys.stderr)
        return 4

    if args.report:
        rp = os.path.abspath(args.report)
        os.makedirs(os.path.dirname(rp) or ".", exist_ok=True)
        with open(rp, "w", encoding="utf-8") as fh:
            fh.write("# 修改清单\n\n")
            fh.write("- 源工程：`%s`\n- 条目总数：%d\n- 改动：%d 处\n\n"
                     % (os.path.basename(args.prproj), len(rows), len(log)))
            fh.write("| # | 时间码 | 原文 | 改后 | 依据 |\n|---|---|---|---|---|\n")
            for no, start, _end, old, new, reason in log:
                fh.write("| %03d | %s | %s | %s | %s |\n"
                         % (no, T.fmt_tc(start), old, new, reason or ""))
        print("清单：%s" % rp)

    if not args.apply:
        print("\n[dry-run] 未写文件。确认无误后加 --apply。")
        return 0

    from safety import Guard, SafetyError, fail

    try:
        guard = Guard.build(
            workdir=args.workdir,
            primary_input=args.prproj,
            inputs=[args.prproj],
            approve=args.approve_overwrite,
        )
        out_path = guard.check_output(args.out, what="工程输出")
        pr._write(out_path)
        outputs = [out_path]
        if args.report:
            outputs.append(os.path.abspath(args.report))
        guard.log("prwrite", args={"source": args.prproj, "changes": len(log)},
                  outputs=outputs, note="rewrite subtitle text in prproj")
        guard.verify_inputs_untouched()
    except SafetyError as err:
        return fail(err, "prwrite")

    print("\n已写入：%s" % out_path)
    print("源工程未被修改（已校验 sha256）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
