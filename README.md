# srt-proofread-prproj

校对字幕，然后把字幕套进你自己的 Premiere 模版，导出 `.prproj`。

不用重设字体、颜色、描边、位置——脚本从你给的工程里克隆一条现成的字幕，
只换文字和时间码。

```bash
python3 scripts/srt2pr.py 字幕.srt --template 模版.prproj --out 交付.prproj
```

---

## 解决什么问题

1. **字幕切在字中间。** 语音识别给的时间码普遍不贴气口。本工具用音频能量
   把切点吸到人换气的位置，报告里能看到每个切点比局部语音低多少 dB。
2. **字幕一会儿上一会儿下。** 相邻两条之间留了 1–200ms 空隙，字幕闪一下。
   工具把 ≤5 帧的间隙直接连上。
3. **套格式太麻烦。** 手动在 Premiere 里复制样式、一条条改文本，几百条要命。
   工具克隆母版，22 个参数原样继承。
4. **改了什么说不清。** 校对只给建议不给结论，每处改动都带条目号、
   原文、改后、依据，可以直接贴给导演看。

---

## 安装

```bash
git clone https://github.com/zbitextn-svg/srt-proofread-prproj.git
cd srt-proofread-prproj
```

没有第三方 Python 依赖。音频对齐需要 `ffmpeg`：

```bash
brew install ffmpeg        # macOS
```

作为 agent skill 使用（Claude Code / WorkBuddy / Cursor 等）：

```bash
./install.sh               # 复制到 ~/.workbuddy/skills/
```

---

## 分步用

### 1. 校对文字

```bash
python3 scripts/proofread.py 字幕.srt --report 报告.md
```

输出一张表：条目号、时间码、原文、建议、规则、级别。
默认**不改文件**，只给建议。

确认后可以自动应用其中安全的三类（英文替换、书名号、句末标点）：

```bash
python3 scripts/proofread.py 字幕.srt --rules 我的词表.json \
    --out 校对版.srt --apply --join --report 报告.md
```

### 2. 对齐时间轴

```bash
python3 scripts/audioalign.py 视频.mp4 校对版.srt \
    --out 对齐版.srt --report 对齐报告.md --fps 25 --join 0.2 --split-suggest
```

### 3. 导出工程

```bash
python3 scripts/srt2pr.py 对齐版.srt \
    --template 模版.prproj --out 交付.prproj \
    --fps 25 --sequence-name "字幕 第一组"
```

一步到位：

```bash
python3 scripts/srt2pr.py 校对版.srt --template 模版.prproj --out 交付.prproj \
    --align 视频.mp4 --join 0.2 --save-srt 对齐版.srt
```

### 4. 看自检结果

```json
{
  "ok": true,
  "verify": {
    "items_in_project": 59,
    "items_expected": 59,
    "text_all_match": true,
    "max_time_dev_frames": 0.0,
    "dangling_refs": [],
    "duplicate_ids": []
  }
}
```

`ok: false` 就说明工程有问题，别交付。

---

## 模版怎么准备

不需要特殊格式。随便一个 Premiere 工程，只要里面**有一条已经排好样式的字幕**
（文本工具打的、或者基本图形里做的），存成 `.prproj` 就行。

脚本会自动找第一条带 `AE.ADBE Text` 滤镜的条目当母版。
模版里原有的字幕会被替换掉，其他轨道（视频、音乐）不动。

想保留模版原有字幕：加 `--keep-template-items`。

---

## 命令行参数

### proofread.py

| 参数 | 说明 |
|---|---|
| `--rules JSON` | 自定义词表（英文映射、作品名、组合名、字数上限等） |
| `--report MD` | 输出 Markdown 报告 |
| `--out SRT` | 输出修正后字幕 |
| `--apply` | 应用安全规则 |
| `--join` | 连上 ≤200ms 的间隙 |
| `--fps` | 帧率，默认 25 |

### audioalign.py

| 参数 | 说明 |
|---|---|
| `--max-shift` | 最大吸附位移，默认 0.25s |
| `--join` | 连上阈值，默认 0.2s，`0` 关闭 |
| `--split-suggest` | 提示条内气口（拆条参考） |
| `--fps` | 帧率 |

### srt2pr.py

| 参数 | 说明 |
|---|---|
| `--template` | 模版 `.prproj`（必填） |
| `--out` | 输出 `.prproj`（必填） |
| `--track N` | 模版里第几条视频轨（0 起） |
| `--sequence-name` | 输出序列名 |
| `--align` | 先做音频对齐的素材 |
| `--join` | 连上阈值 |
| `--keep-template-items` | 保留模版原有字幕 |
| `--save-srt` | 另存处理后的 SRT |

---

## 词表

`examples/rules.example.json`：

```json
{
  "en_map": { "OK": "好", "cue": "提", "vlog": "视频日记" },
  "works": ["作品甲", "作品乙"],
  "groups": ["组合甲"],
  "max_line_chars": 14,
  "max_cps": 9.0,
  "join_gap": 0.2
}
```

`works` 里填全称，脚本会给它加书名号；`groups` 里的加双引号。
代码里不写死任何具体名字，全靠这个词表。

---

## 原理

`.prproj` 是 gzip 压缩的 XML，时间基 `254016000000 ticks/秒`。
一条字幕是 `VideoClipTrackItem → 组件链 → 文本滤镜 → 22 个参数 → SubClip`。

文本存在参数 0 的 base64 blob 末尾，前面 4 字节是 UTF-8 字节长度。
**这个偏移在不同工程里不一样**（实测 556 和 536），所以脚本靠「已知原文」
反查位置，不硬编码任何偏移或对象 ID。

克隆时给链上每个对象分配新 ID，替换引用，改时间和文本，旧条目连对象一起删。
详见 [`references/prproj-internals.md`](references/prproj-internals.md)。

---

## 校对口径

不是通用规范，是从反复返工里总结的具体做法：语气词删哪些留哪些、
重复字什么时候不能删、英文怎么换、人名为什么不能猜。
见 [`references/rules-zh.md`](references/rules-zh.md)。

拆条和时间轴规则见 [`references/timeline.md`](references/timeline.md)。

---

## 限制

- 只处理**文本类字幕**（文本工具 / 基本图形）。SRT 轨、开放字幕、第三方插件
  生成的字幕不支持
- 一个模版只克隆一种样式。同一部片要多种样式（标题 / 正文 / 强调），
  分多次导出，或者每种样式准备一个模版
- 跨 Premiere 大版本（如 2020 → 2024）的模版没做过完整测试
- 音频对齐依赖 `ffmpeg`，纯音乐/强噪声素材效果会变差
- 不自动判断说话人。换人处的切点需要人工确认

---

## License

MIT
