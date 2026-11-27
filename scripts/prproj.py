#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prproj.py — Premiere Pro 工程（.prproj）字幕轨注入。

核心思路：不重建样式，而是「母版克隆」。
从用户给的模版工程里找一条已经排好样式的字幕条目，读出它的
对象链（TrackItem → 组件链 → 文本滤镜 → 参数 → SubClip），
然后复制 N 份，只改文本和出入点。字体/颜色/描边/阴影/位置/
动画/安全框全部原样继承。

因此本模块不含任何硬编码的对象 ID、blob 偏移或轨道号：
所有定位都靠结构特征 + 文本内容反查，可以吃任何人的模版。

.prproj 是 gzip 压缩的 XML，直接读写字节流。
"""

from __future__ import annotations

import base64
import gzip
import io
import re
import shutil
import struct
import uuid
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Premiere 的时间基准：每秒 254016000000 ticks
TICKS_PER_SECOND = 254016000000

TEXT_MATCHNAME = "AE.ADBE Text"

# 顶层对象：文件里一级缩进的 <Tag ObjectID="n"> / <Tag ObjectUID="guid">
_TOP_OBJ_RE = re.compile(
    r'(?m)^\t<(\w+) (ObjectID|ObjectUID)="([^"]+)"[^>]*?(\/?)>'
)


@dataclass
class Obj:
    """一个顶层对象块。"""

    oid: str
    tag: str
    start: int          # 块起点（含行首 \t）
    end: int            # 块终点（含结束标签）
    kind: str           # "ObjectID" | "ObjectUID"
    self_closing: bool = False

    @property
    def is_id(self) -> bool:
        return self.kind == "ObjectID"


@dataclass
class TextChain:
    """模版里一条字幕条目的完整对象链。"""

    ti: str
    chain: str
    filter: str
    params: List[str]
    subclip: str
    clip: Optional[str]
    track_uid: Optional[str]
    text_param: str
    text: str
    blob: bytes
    text_offset: int
    text_len: int


class Prproj:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as fh:
            raw = fh.read()
        self.compressed = raw[:2] == b"\x1f\x8b"
        if self.compressed:
            raw = gzip.decompress(raw)
        self.xml = raw.decode("utf-8")
        self._objs: Optional[List[Obj]] = None
        self._by_id: Dict[str, Obj] = {}
        self._by_tag: Dict[str, List[Obj]] = {}
        self._index()

    # ---------------------------------------------------------------- 索引

    def _index(self) -> None:
        objs: List[Obj] = []
        for m in _TOP_OBJ_RE.finditer(self.xml):
            tag, kind, oid, selfclose = m.group(1), m.group(2), m.group(3), m.group(4) == "/"
            if selfclose:
                end = m.end()
            else:
                close = "\n\t</%s>" % tag
                e = self.xml.find(close, m.end())
                end = e + len(close) if e != -1 else m.end()
            objs.append(Obj(oid, tag, m.start(), end, kind, selfclose))
        self._objs = objs
        self._by_id = {o.oid: o for o in objs if o.is_id}
        self._by_obj = {o.oid: o for o in objs}
        self._by_tag = {}
        for o in objs:
            self._by_tag.setdefault(o.tag, []).append(o)

    @property
    def objects(self) -> List[Obj]:
        return self._objs or []

    def objects_of(self, tag: str) -> List[Obj]:
        return self._by_tag.get(tag, [])

    def block(self, oid: str) -> str:
        o = self._by_id.get(oid)
        if o is None:
            raise KeyError("no object %s" % oid)
        return self.xml[o.start:o.end]

    def block_any(self, oid: str) -> str:
        """按 ObjectID 或 ObjectUID 取块。Sequence/Track 这类对象只有 UID。"""
        o = self._by_obj.get(oid)
        if o is None:
            raise KeyError("no object %s" % oid)
        return self.xml[o.start:o.end]

    def max_id(self) -> int:
        # 用全文而非仅顶层：Project 内部还有嵌套对象也占 ID
        ids = re.findall(r'ObjectID="(\d+)"', self.xml)
        return max((int(x) for x in ids), default=0)

    # ------------------------------------------------------------ 模版探测

    def find_text_chains(self) -> List[TextChain]:
        """找出工程里所有「带文本滤镜」的条目，返回可用的模版链。"""
        out: List[TextChain] = []
        for ti in self.objects_of("VideoClipTrackItem"):
            chain = self._chain_of(ti)
            if not chain:
                continue
            comp = self._find_text_filter(chain)
            if not comp:
                continue
            fid, params = comp
            filt_block = self.block(fid)
            subclip = self._subclip_of(ti)
            if not subclip:
                continue
            probe = self._probe_text(params, filt_block)
            if probe is None:
                continue
            tp, text, blob, off, ln = probe
            out.append(
                TextChain(
                    ti=ti.oid,
                    chain=chain,
                    filter=fid,
                    params=params,
                    subclip=subclip,
                    clip=self._clip_of_subclip(subclip),
                    track_uid=self._track_uid_of(ti.oid),
                    text_param=tp,
                    text=text,
                    blob=blob,
                    text_offset=off,
                    text_len=ln,
                )
            )
        return out

    def _chain_of(self, ti: Obj) -> Optional[str]:
        b = self.block(ti.oid)
        m = re.search(r'<Components ObjectRef="(\d+)"/>', b)
        return m.group(1) if m else None

    def _find_text_filter(self, chain_id: str) -> Optional[Tuple[str, List[str]]]:
        b = self.block(chain_id)
        for m in re.finditer(r'<Component Index="\d+" ObjectRef="(\d+)"/>', b):
            fid = m.group(1)
            fb = self.block(fid)
            if TEXT_MATCHNAME in fb:
                params = re.findall(r'<Param Index="\d+" ObjectRef="(\d+)"/>', fb)
                return fid, params
        return None

    def _subclip_of(self, ti: Obj) -> Optional[str]:
        m = re.search(r'<SubClip ObjectRef="(\d+)"/>', self.block(ti.oid))
        return m.group(1) if m else None

    def _clip_of_subclip(self, sid: str) -> Optional[str]:
        m = re.search(r'<Clip ObjectRef="(\d+)"/>', self.block(sid))
        return m.group(1) if m else None

    def _track_uid_of(self, ti_id: str) -> Optional[str]:
        pat = re.compile(r'<TrackItem Index="\d+" ObjectRef="%s"/>' % re.escape(ti_id))
        for tr in self.objects_of("VideoClipTrack"):
            if pat.search(self.xml[tr.start:tr.end]):
                m = re.search(r'ObjectUID="([^"]+)"', self.xml[tr.start:tr.start + 200])
                return m.group(1) if m else None
        return None

    def _probe_text(self, params: Sequence[str], filt_block: str):
        """在参数里找到承载文本的那个 blob，并定位文本字节。"""
        # 文本的候选来源：滤镜的 InstanceName（PR 重存后的文本副本）
        m = re.search(r"<InstanceName>(.*?)</InstanceName>", filt_block, re.S)
        candidates = []
        if m:
            candidates.append(m.group(1))
        # 也可以从 SubClip/Clip 名字猜，但不可靠，略过
        for pid in params:
            pb = self.block(pid)
            bm = re.search(
                r'<StartKeyframeValue[^>]*Encoding="base64"[^>]*>(.*?)</StartKeyframeValue>',
                pb,
                re.S,
            )
            if not bm:
                continue
            try:
                blob = base64.b64decode(bm.group(1).strip())
            except Exception:
                continue
            for cand in candidates:
                if not cand:
                    continue
                raw = cand.encode("utf-8")
                idx = _find_text_bytes(blob, raw)
                if idx is not None:
                    return pid, cand, blob, idx, len(raw)
        # 没有 InstanceName 时，按「源文本」参数名兜底
        for pid in params:
            pb = self.block(pid)
            if "<Name>源文本</Name>" not in pb and "<Name>Source Text</Name>" not in pb:
                continue
            bm = re.search(
                r'<StartKeyframeValue[^>]*Encoding="base64"[^>]*>(.*?)</StartKeyframeValue>',
                pb,
                re.S,
            )
            if not bm:
                continue
            blob = base64.b64decode(bm.group(1).strip())
            got = _guess_text_tail(blob)
            if got:
                text, off = got
                return pid, text, blob, off, len(text.encode("utf-8"))
        return None

    # ---------------------------------------------------------------- 导出

    def export(
        self,
        entries: Iterable[Tuple[str, float, float]],
        out_path: str,
        track_index: Optional[int] = None,
        sequence_name: Optional[str] = None,
        fps: float = 25.0,
        keep_template_items: bool = False,
    ) -> dict:
        """把 [(文本, 起秒, 止秒)] 写进模版的字幕轨。

        返回统计信息，便于上层校验。
        """
        chains = self.find_text_chains()
        if not chains:
            raise RuntimeError(
                "模版里找不到带文本滤镜的字幕条目；请确认模版中至少有一条排好样式的字幕"
            )
        tpl = self._pick_template(chains, track_index)
        track = self._find_track_obj(tpl.track_uid)
        if track is None:
            raise RuntimeError("找不到字幕所在的视频轨道")

        entries = [(t.strip(), float(s), float(e)) for t, s, e in entries]
        entries = [x for x in entries if x[0] and x[2] > x[1]]
        if not entries:
            raise ValueError("没有有效字幕条目")

        next_id = self.max_id() + 1

        def alloc() -> str:
            nonlocal next_id
            n = next_id
            next_id += 1
            return str(n)

        # 母版各块的原文
        ti_b, chain_b, sub_b = self.block(tpl.ti), self.block(tpl.chain), self.block(tpl.subclip)

        # 链上**所有**组件都要克隆。除了文本滤镜，还可能挂着 Vector Motion /
        # 不透明度之类的组件；只克隆文本滤镜的话，旧对象删掉后新条目会指向空号
        # （Premiere 打开报"缺少组件"，回读校验报 dangling ref）。
        comp_ids = [
            m.group(1)
            for m in re.finditer(r'<Component Index="\d+" ObjectRef="(\d+)"/>', chain_b)
        ]
        if tpl.filter not in comp_ids:
            comp_ids.append(tpl.filter)
        comp_b = {cid: self.block(cid) for cid in comp_ids}
        comp_params = {
            cid: re.findall(r'<Param Index="\d+" ObjectRef="(\d+)"/>', comp_b[cid])
            for cid in comp_ids
        }
        param_blocks: Dict[str, str] = {}
        for cid in comp_ids:
            for pid in comp_params[cid]:
                param_blocks[pid] = self.block(pid)

        # 旧条目：默认清空整条轨（只留目标轨，其他轨不动）
        old_item_count = len(
            re.findall(
                r'<TrackItem Index="\d+" ObjectRef="\d+"/>',
                self.xml[track.start:track.end],
            )
        )
        old_ids = self._remove_old_items(track, keep=keep_template_items)
        if keep_template_items:
            old_item_count = 0

        new_blocks: List[str] = []
        item_refs: List[str] = []
        ticks_per_frame = TICKS_PER_SECOND / float(fps)

        for text, start, end in entries:
            f0 = int(round(start * fps))
            f1 = int(round(end * fps))
            if f1 <= f0:
                f1 = f0 + 1
            t0 = int(round(f0 * ticks_per_frame))
            t1 = int(round(f1 * ticks_per_frame))

            id_map: Dict[str, str] = {}
            ti_new = alloc()
            id_map[tpl.ti] = ti_new
            id_map[tpl.chain] = alloc()
            id_map[tpl.subclip] = alloc()
            for cid in comp_ids:
                id_map[cid] = alloc()
            for cid in comp_ids:
                for pid in comp_params[cid]:
                    id_map[pid] = alloc()

            # 1) TrackItem：替换时间与引用
            b = _remap(ti_b, id_map)
            b = re.sub(r"<Start>-?\d+</Start>", "<Start>%d</Start>" % t0, b, count=1)
            b = re.sub(r"<End>-?\d+</End>", "<End>%d</End>" % t1, b, count=1)
            new_blocks.append(b)

            # 2) 组件链：引用替换即可
            new_blocks.append(_remap(chain_b, id_map))

            # 3) 逐个组件：文本滤镜顺手改 InstanceName，其余原样
            for cid in comp_ids:
                cb = _remap(comp_b[cid], id_map)
                if cid == tpl.filter:
                    cb = _set_instance_name(cb, text)
                new_blocks.append(cb)

                # 4) 该组件的参数：只有承载文本的那个改 blob
                for pid in comp_params[cid]:
                    pb = param_blocks[pid]
                    if pid == tpl.text_param:
                        new_blob = _replace_text_blob(
                            tpl.blob, tpl.text_offset, tpl.text_len, text
                        )
                        pb = _set_blob(pb, new_blob)
                    new_blocks.append(_remap(pb, id_map))

            # 5) SubClip：引用替换
            new_blocks.append(_remap(sub_b, id_map))

            item_refs.append(ti_new)

        # 写轨道条目列表
        new_track_xml = self._rewrite_track_items(track, item_refs)
        self.xml = new_track_xml

        # 模版的工作区常常只覆盖它自己那几秒，条目一多就出现"序列只有 60 秒"的错觉。
        # 把工作区终点拉到最后一个条目的出点（只放大，不缩小）。
        last_ticks = int(round(max(e for _, _, e in entries) * fps * ticks_per_frame)) \
            if entries else 0
        self.xml = _bump_workout(self.xml, last_ticks)

        # 删旧对象（重新索引后按 ID 删除）
        self._index()
        self._drop_objects(old_ids)
        self._index()

        # 插到 </PremiereData> 之前；每个块自带行首缩进，补一个换行保证行首对齐
        insert_at = self.xml.rfind("\n</PremiereData>")
        if insert_at == -1:
            insert_at = len(self.xml)
        payload = "".join("\n" + b.lstrip("\n") for b in new_blocks)
        self.xml = self.xml[:insert_at] + payload + self.xml[insert_at:]

        # 序列名 + 抹掉模版里残留的外机路径
        if sequence_name:
            self.xml = _rename_sequence(self.xml, sequence_name)
        self.xml = _sanitize_local_paths(self.xml)

        self._index()
        self._write(out_path)

        return {
            "entries": len(entries),
            # 只回对象信息和文本长度，不回原文——避免把模版里的字幕内容打到日志里
            "template": {
                "item": tpl.ti,
                "text_chars": len(tpl.text),
                "track_uid": tpl.track_uid,
                "clip": tpl.clip,
            },
            "removed_items": old_item_count,
            "removed_objects": len(old_ids),
            "fps": fps,
            "sequence_name": sequence_name,
        }

    def _pick_template(self, chains: List[TextChain], track_index: Optional[int]) -> TextChain:
        if track_index is None:
            return chains[0]
        for c in chains:
            tr = self._find_track_obj(c.track_uid)
            if tr is None:
                continue
            m = re.search(r"<Index>(\d+)</Index>", self.xml[tr.start:tr.end])
            if m and int(m.group(1)) == track_index:
                return c
        return chains[0]

    def _find_track_obj(self, uid: Optional[str]) -> Optional[Obj]:
        if not uid:
            return None
        for tr in self.objects_of("VideoClipTrack"):
            if 'ObjectUID="%s"' % uid in self.xml[tr.start:tr.start + 300]:
                return tr
        return None

    def _remove_old_items(self, track: Obj, keep: bool = False) -> List[str]:
        """清空轨道上的条目，返回需要删除的对象 id 集合。"""
        tb = self.xml[track.start:track.end]
        ids = re.findall(r'<TrackItem Index="\d+" ObjectRef="(\d+)"/>', tb)
        if keep or not ids:
            return []
        doomed = set()
        for tid in ids:
            for x in self._descendants(tid):
                doomed.add(x)
        return sorted(doomed, key=lambda s: int(s) if s.isdigit() else 0)

    def _descendants(self, oid: str) -> List[str]:
        """TI 及其 chain/filter/params/subclip（到 Clip 为止，Clip 共享不删）。"""
        out = [oid]
        try:
            b = self.block(oid)
        except KeyError:
            return out
        m = re.search(r'<Components ObjectRef="(\d+)"/>', b)
        if m:
            cid = m.group(1)
            out.append(cid)
            try:
                cb = self.block(cid)
            except KeyError:
                cb = ""
            for fm in re.finditer(r'<Component Index="\d+" ObjectRef="(\d+)"/>', cb):
                fid = fm.group(1)
                out.append(fid)
                try:
                    fb = self.block(fid)
                except KeyError:
                    continue
                out.extend(re.findall(r'<Param Index="\d+" ObjectRef="(\d+)"/>', fb))
        sm = re.search(r'<SubClip ObjectRef="(\d+)"/>', b)
        if sm:
            out.append(sm.group(1))
        # 去重保序
        seen, uniq = set(), []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    def _rewrite_track_items(self, track: Obj, refs: Sequence[str]) -> str:
        tb = self.xml[track.start:track.end]
        lines = "".join(
            '\n\t\t\t\t\t<TrackItem Index="%d" ObjectRef="%s"/>' % (i, r)
            for i, r in enumerate(refs)
        )
        new_tb = re.sub(
            r'(<TrackItems Version="1">)(.*?)(</TrackItems>)',
            lambda m: m.group(1) + lines + m.group(3),
            tb,
            count=1,
            flags=re.S,
        )
        if new_tb == tb:
            raise RuntimeError("轨道里没有 TrackItems 列表，无法写入")
        return self.xml[:track.start] + new_tb + self.xml[track.end:]

    def _drop_objects(self, ids: Sequence[str]) -> None:
        spans = []
        for oid in ids:
            o = self._by_id.get(oid)
            if o:
                spans.append((o.start, o.end))
        for s, e in sorted(spans, reverse=True):
            self.xml = self.xml[:s] + self.xml[e:]

    def _write(self, out_path: str) -> None:
        data = self.xml.encode("utf-8")
        if self.compressed:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
                gz.write(data)
            data = buf.getvalue()
        with open(out_path, "wb") as fh:
            fh.write(data)

    # ---------------------------------------------------------------- 校验

    def read_back(self) -> List[Tuple[str, float, float]]:
        """从（已导出的）工程里读回字幕：文本 + 起止秒。"""
        out = []
        for ch in self.find_text_chains():
            m = re.search(r"<Start>(-?\d+)</Start>", self.block(ch.ti))
            n = re.search(r"<End>(-?\d+)</End>", self.block(ch.ti))
            if not m or not n:
                continue
            out.append(
                (
                    ch.text,
                    int(m.group(1)) / TICKS_PER_SECOND,
                    int(n.group(1)) / TICKS_PER_SECOND,
                )
            )
        out.sort(key=lambda x: x[1])
        return out

    def check_integrity(self) -> dict:
        """引用完整性：未定义的 ObjectRef / 重复 ID。"""
        # 顶层 + 嵌套对象都在 ID 空间里，按全文收集，避免把嵌套引用误报成悬空
        ids = set(re.findall(r'ObjectID="(\d+)"', self.xml))
        refs = set(re.findall(r'ObjectRef="(\d+)"', self.xml))
        dangling = sorted(refs - ids, key=lambda s: int(s) if s.isdigit() else 0)
        seen, dup = set(), set()
        for o in self.objects:
            if not o.is_id:
                continue
            if o.oid in seen:
                dup.add(o.oid)
            seen.add(o.oid)
        return {
            "objects": len(self.objects),
            "ids": len(ids),
            "refs": len(refs),
            "dangling": dangling,
            "duplicate_ids": sorted(dup, key=lambda s: int(s) if s.isdigit() else 0),
            "text_items": len(self.find_text_chains()),
        }


# ---------------------------------------------------------------- 工具函数


def _remap(block: str, mapping: Dict[str, str]) -> str:
    """把块内的 ObjectID/ObjectRef 按映射改写。"""
    if not mapping:
        return block
    pattern = re.compile(r'(ObjectID|ObjectRef)="(\d+)"')

    def sub(m):
        new = mapping.get(m.group(2))
        if new is None:
            return m.group(0)
        return '%s="%s"' % (m.group(1), new)

    return pattern.sub(sub, block)


def _find_text_bytes(blob: bytes, raw: bytes) -> Optional[int]:
    """在 blob 里定位文本：前 4 字节必须等于文本字节长度。"""
    if not raw:
        return None
    idx = blob.find(raw)
    while idx != -1:
        if idx >= 4:
            (ln,) = struct.unpack_from("<I", blob, idx - 4)
            if ln == len(raw):
                return idx
        idx = blob.find(raw, idx + 1)
    return None


def _guess_text_tail(blob: bytes) -> Optional[Tuple[str, int]]:
    """兜底：从 blob 尾部找一段以 u32 长度前缀标记的 UTF-8 文本。"""
    for idx in range(len(blob) - 1, 3, -1):
        (ln,) = struct.unpack_from("<I", blob, idx - 4)
        if ln <= 0 or idx + ln != len(blob):
            continue
        try:
            text = blob[idx:].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text and all(ch.isprintable() or ch in "\n\t" for ch in text):
            return text, idx
    return None


def _replace_text_blob(blob: bytes, off: int, old_len: int, new_text: str) -> bytes:
    raw = new_text.encode("utf-8")
    out = bytearray(blob[:off] + raw + blob[off + old_len:])
    struct.pack_into("<I", out, off - 4, len(raw))
    # 部分模版在 blob 头部还有一个「总长度-12」的 u32，同步更新
    delta = len(out) - len(blob)
    if delta:
        (head_len,) = struct.unpack_from("<I", out, 0)
        if head_len == len(blob) - 12:
            struct.pack_into("<I", out, 0, len(out) - 12)
    return bytes(out)


def _set_blob(param_block: str, blob: bytes) -> str:
    b64 = base64.b64encode(blob).decode("ascii")
    return re.sub(
        r'(<StartKeyframeValue[^>]*Encoding="base64"[^>]*>)(.*?)(</StartKeyframeValue>)',
        lambda m: m.group(1) + b64 + m.group(3),
        param_block,
        count=1,
        flags=re.S,
    )


def _set_instance_name(filt_block: str, text: str) -> str:
    esc = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    if "<InstanceName>" in filt_block:
        return re.sub(
            r"<InstanceName>.*?</InstanceName>",
            "<InstanceName>%s</InstanceName>" % esc,
            filt_block,
            count=1,
            flags=re.S,
        )
    # 没有就插到 DisplayName 之后，PR 重存时位置一致
    m = re.search(r"</DisplayName>", filt_block)
    if m:
        return filt_block[: m.end()] + "\n\t\t\t<InstanceName>%s</InstanceName>" % esc + filt_block[m.end():]
    return filt_block


def _bump_workout(xml: str, ticks: int) -> str:
    """把序列工作区终点至少拉到 ticks（只放大不缩小）。"""
    m = re.search(r"<MZ.WorkOutPoint>(-?\d+)</MZ.WorkOutPoint>", xml)
    if not m or ticks <= 0:
        return xml
    cur = int(m.group(1))
    if ticks <= cur:
        return xml
    return xml[: m.start(1)] + str(ticks) + xml[m.end(1) :]


def _rename_sequence(xml: str, name: str) -> str:
    """给第一个 Sequence 改名；工程面板里同名的条目一起改，免得两处名字不一样。"""
    m = re.search(r'\n\t<Sequence ObjectUID="[^"]+"', xml)
    if not m:
        return xml
    end = xml.find("\n\t</Sequence>", m.start())
    if end == -1:
        return xml
    seg = xml[m.start():end]
    esc = _xml_escape(name)
    old_name = None
    nm = re.search(r"<Name>([^<]*)</Name>", seg)
    if nm and nm.group(1) != name:
        old_name = nm.group(1)
    if "<Name>" in seg:
        seg2 = re.sub(r"<Name>.*?</Name>", "<Name>%s</Name>" % esc, seg, count=1, flags=re.S)
    else:
        seg2 = seg.replace(
            '<Node Version="1">',
            '<Name>%s</Name>\n\t\t<Node Version="1">' % esc,
            1,
        )
    xml = xml[:m.start()] + seg2 + xml[end:]
    if old_name:
        xml = xml.replace("<Name>%s</Name>" % _xml_escape(old_name), "<Name>%s</Name>" % esc)
    return xml


def _sanitize_local_paths(xml: str) -> str:
    """抹掉工程里记着的"上次打开路径"。模版常常来自别人的机器，
    里面会留着 <用户目录>/Desktop/... 这种绝对路径，既暴露信息又会让 PR 报路径失效。"""
    pat = re.compile(
        r"<project\.settings\.lastknowngoodprojectpath>([^<]*)</project\.settings\.lastknowngoodprojectpath>"
    )

    def repl(m):
        v = m.group(1)
        if re.search(r"(/Users/|/home/|[A-Za-z]:\\\\Users\\\\|/Volumes/|/private/|/tmp/)", v):  # leakscan:ignore
            return "<project.settings.lastknowngoodprojectpath></project.settings.lastknowngoodprojectpath>"
        return m.group(0)

    return pat.sub(repl, xml)


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
