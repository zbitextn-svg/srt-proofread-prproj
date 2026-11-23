# .prproj 内部结构与母版克隆

写给要改脚本的人。看懂这一节，就能把导出器移植到别的 NLE 或者处理奇怪的模版。

---

## 1. 文件容器

`.prproj` 是 **gzip 压缩的 XML**（有些版本是 zip 或未压缩，脚本按 magic 判断）。

```python
raw = open(p, "rb").read()
if raw[:2] == b"\x1f\x8b":
    raw = gzip.decompress(raw)
xml = raw.decode("utf-8")
```

写回时保持同样的压缩方式，`gzip.compress(data, 9, mtime=0)`，
否则 Premiere 会认为文件损坏。

---

## 2. 时间基

```
1 秒 = 254016000000 ticks   （254016000000 = 2^9 × 3^4 × 5^6 × 7^2，能被 23.976/24/25/30/50/60 整除）
25fps 一帧 = 10160640000 ticks
```

`<Start>` / `<End>` 都是绝对 ticks，从 0 开始。
负值 `-91445760000000000` 出现在参数关键帧里，表示「无关键帧」，不要动。

---

## 3. 对象图

每个顶层对象是一行 `<Tag ObjectID="n" ...>` 开始、`</Tag>` 结束，
一级缩进（`\t`）。对象之间用 `ObjectRef="n"`（数字 ID）互相引用。

一条字幕条目：

```
VideoClipTrackItem          ← 时间线上的一个块，含 <Start>/<End>/<FrameRect>
├─ Components → VideoComponentChain
│   └─ Component → VideoFilterComponent   MatchName="AE.ADBE Text"
│       └─ Param[0..21] → 参数对象
│           └─ Param[0] 是「源文本」，base64 blob
└─ SubClip → Clip（图形/素材），MasterClip 用 ObjectURef 指向 UID
```

注意两种引用：

- `ObjectRef="123"` —— 数字 ID，指向 `ObjectID`
- `ObjectURef="guid"` —— GUID，指向 `ObjectUID` 定义的对象
  （Track、Sequence、MediaSource 都用这个）

轨道对象是 `VideoClipTrack ObjectUID="..."`，里面
`<ClipItems><TrackItems>` 按顺序列出本条轨的所有 `TrackItem`。
**顺序就是时间顺序，Index 必须连续从 0 开始。**

---

## 4. 文本存在哪

参数 0（`<Name>源文本</Name>`，`ParameterID=1`）的
`<StartKeyframeValue Encoding="base64">` 是一个二进制 blob。

结构（小端）：

```
[0..4)     u32   总长度 - 12（部分模版有，需要同步更新）
[4..N)     样式头（字体、字号、颜色、描边、对齐、动画曲线…）
[N-4-len..N-4)  u32  文本字节数
[N-len..N)      UTF-8 文本
```

**关键：文本偏移不是固定的。**

实测两份不同工程：

| 工程 | blob 总长 | 文本偏移 |
|---|---|---|
| A | 580 | 556 |
| B | 560 | 536 |

同一版本 Premiere、同样 22 个参数，偏移差 20 字节——因为样式头里
有些可选字段。所以**绝不能硬编码偏移**。

正确做法：拿已知原文去 blob 里搜，找到后校验前 4 字节是否等于
文本字节长度，匹配上才是真位置。

```python
idx = blob.find(text.encode("utf-8"))
while idx != -1:
    if struct.unpack_from("<I", blob, idx - 4)[0] == len(raw):
        break
    idx = blob.find(raw, idx + 1)
```

替换时：

```python
out = bytearray(blob[:off] + new_raw + blob[off + old_len:])
struct.pack_into("<I", out, off - 4, len(new_raw))
if head == len(blob) - 12:          # 头部总长字段
    struct.pack_into("<I", out, 0, len(out) - 12)
```

如果样式头里的长度字段没跟着改，Premiere 会读多/读少几个字节，
表现为文本后面带乱码或者被截断。

---

## 5. 已知原文从哪来

三个来源，按可靠性排序：

1. `VideoFilterComponent` 里的 `<InstanceName>` —— Premiere 重存工程时会写，
   内容就是字幕文本。最稳。
2. 参数对象的 `<Name>源文本</Name>` + 从 blob 尾部反推文本。
3. 同一条目在 SRT 里的文本（前提是模版就是从这份字幕生成的）。

`prproj.py` 用 1 和 2。如果模版是从没重存过的工程（InstanceName 为空），
会走 2。

---

## 6. 克隆步骤

1. 遍历所有 `VideoClipTrackItem`，找到第一个「链条里有 `AE.ADBE Text`」的，
   作为母版
2. 记下母版链上所有对象块原文：TI、chain、filter、22 个 param、SubClip
3. `max_id = max(全文所有 ObjectID)`，新对象从 `max_id + 1` 递增
4. 每条字幕：
   - 给链上每个对象分配新 ID，建 `旧→新` 映射
   - 用正则把块内的 `ObjectID="旧"` / `ObjectRef="旧"` 一次性替换
   - 改 TI 的 `<Start>`/`<End>`
   - 改文本 param 的 blob
   - 同步改 filter 的 `<InstanceName>`
   - SubClip 共享母版的 `Clip`（不复制素材对象）
5. 重写轨道的 `<TrackItems>` 列表
6. 删掉旧条目及其对象（TI、chain、filter、params、SubClip）——
   **不要删 Clip 和 MediaSource**，它们被新条目共享
7. 新对象插到 `</PremiereData>` 之前，每块前补 `\n` 保证行首对齐

**为什么不用新建对象的方式**：新建要自己填 22 个参数的
`ClassID`、`ParameterID`、默认值，错一个 Premiere 就静默忽略整条字幕。
克隆天然继承，没有这个风险。

---

## 7. 自检

导出后必须回读，不能只看「没报错」：

```python
pr = Prproj(out)
pr.check_integrity()   # objects / refs / dangling / duplicate_ids / text_items
pr.read_back()         # [(文本, 起秒, 止秒)]，按时间排序
```

判定标准：

- `dangling` 为空 —— 所有 `ObjectRef` 都能找到目标
- `duplicate_ids` 为空 —— 没有 ID 撞车
- `text_items` 等于字幕条数
- 回读文本逐条相等
- 时间偏差 ≤1 帧（帧对齐本身的极限是 0.5 帧）

注意：`check_integrity` 收集 ID 时要扫全文，不能只扫一级缩进对象。
`Project` 内部还有嵌套对象（`ProjectViewState` 等）也占 ID 空间，
只扫顶层会把正常引用误报成悬空。

---

## 8. 兼容性

- 模版和输出用同一个 Premiere 版本最稳。跨大版本（2020 → 2024）
  对象 `Version` 属性不同，克隆通常仍可用，但参数数量可能变化
- 模版里如果有多条视频轨，`--track N` 指定用哪条；不指定取第一条带文本的
- 模版原有的其他轨道（视频素材、音乐）不会被碰
- 输出工程可以被再次当模版用（幂等，已验证两轮）

---

## 9. 打不开怎么查

按顺序：

1. `gzip.decompress` 能不能过 —— 容器坏了
2. `xml.etree.ElementTree.fromstring` 能不能过 —— XML 不合法（标签没闭合）
3. `check_integrity()` 的 `dangling` —— 有没有引用指向不存在的对象
4. `duplicate_ids` —— 新 ID 有没有撞上嵌套对象
5. 轨道 `<TrackItems>` 的 `Index` 是否从 0 连续
6. 文本 blob 的头部长度字段是否同步

前四项脚本已覆盖。真打不开时，先把模版原样复制一份、只替换一条字幕的
文本来测，能定位是克隆逻辑还是写入逻辑的问题。
