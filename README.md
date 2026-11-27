# srt-proofread-prproj

`v3.0.0` · 更新内容见 [CHANGELOG.md](CHANGELOG.md)

校对字幕，然后输出 Premiere 能直接打开的 `.prproj`。两种用法：

**A. 有字幕文件，要出工程** —— 拿你的工程当模版，克隆一条现成的字幕，
只换文字和时间码。

```bash
python3 scripts/srt2pr.py 字幕.srt --template 模版.prproj --out 交付.prproj
```

**B. 字幕已经在工程里，只要校文字** —— 抽出来校，再写回原位置。
字体、描边、位置、时间码一个字节都不动。

```bash
python3 scripts/prtext.py 工程.prproj --workdir 交付        # 抽出文字
python3 scripts/prwrite.py 工程.prproj --changes 改动.json \
        --out 交付/校对版.prproj --workdir 交付               # 写回去
python3 scripts/prverify.py 交付/校对版.prproj \
        --template 工程.prproj --against 工程.prproj          # 交付前校验
```

两条路线的校对规则完全一样，差的只是怎么读进来、怎么写回去。

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

安装器只往已知的技能目录写（`~/.workbuddy/skills`、`~/.claude/skills`），
**不删除目标目录里原有的任何文件**。目标已存在且不是本技能的安装点时会拒绝，
`--force` 也是「先整目录改名备份，再装新的」。从旧版（无标记文件）升级需要
`--force` 一次。

---

## 安全边界

字幕常常是唯一的原始素材，所以脚本不会「尽力而为」，而是**做不到就拒绝**：

| 约束 | 行为 |
|---|---|
| 写入白名单 | 所有输出必须落在 `--workdir` 里（默认输入字幕所在目录），写出去直接拒绝 |
| 保护原始素材 | `--out` 指向任何输入文件时拒绝；跑完校验输入 sha256 未变 |
| 覆盖要批准 | 已存在且非本工具生成的文件，必须 `--approve-overwrite` |
| 全程留痕 | `<workdir>/.srt-proofread/changes.jsonl` 记录路径/大小/sha256/批准状态 |

```bash
# 素材只读，输出单独一个目录
python3 scripts/srt2pr.py 素材/第一组.srt --template 素材/模版.prproj \
    --workdir 交付 --out 交付/第一组.prproj
```

细节（需要覆盖哪些目录、日志长什么样、install.sh 的每一条检查）见
[`references/safety.md`](references/safety.md)。

---

## 直接改已有工程（路线 B）

字幕已经在 Premiere 里做好了，只是文字要校。这时候不要重做工程。

### 1. 抽出文字

```bash
python3 scripts/prtext.py 工程.prproj --workdir 交付
```

得到 `字幕清单.txt`（`序号<TAB>起-止<TAB>文本`）和 `字幕清单.json`。
摘要会报：条目数、时间范围、**有多少条文字靠 `BinaryHash` 引用别人**、
读不到文字的有几条、重复文本有几种。

### 2. 改清单

落成「序号 → 新文本」，`from` 写原文可以让脚本核对版本：

```json
{"changes": [
  {"no": 31, "text": "你好 姐姐", "from": "Hello 姐姐", "reason": "英文改中文"}
]}
```

### 3. 写回

```bash
python3 scripts/prwrite.py 工程.prproj --changes 改动.json \
    --out 交付/校对版.prproj --workdir 交付 --report 交付/修改清单.md
```

默认 dry-run。末尾会打印回读结果，**三项都是 0 才加 `--apply`**：

```
回读：文本不符 0｜读不到文字 0｜时间码变化 0
```

只碰四处：源文本 base64、`InstanceName`、`SubClip/Name`、`MasterClip/Name`。
共用同一段文字的其它条目会先被拆开独立，不会被一起改掉。

### 4. 交付前校验

```bash
python3 scripts/prverify.py 交付/校对版.prproj \
    --template 工程.prproj --against 工程.prproj --md 交付/校验.md
```

七项全过才算完成：引用完整性、文本可读、版本/画幅/帧率、逐对象字节、
样式字节（字体/字号/描边）、字体集合、位置分布。

其中「样式字节」没法替代 —— 字体和描边编码在 base64 blob 的二进制里，
XML 里看不到。只比 XML 属性是查不出字体被换的。

### 只有 FCP7 XML 时

```bash
python3 scripts/xmltext.py 唱词.xml --extract --txt 清单.txt
python3 scripts/xmltext.py 唱词.xml --edits 改动.tsv -o 交付/唱词_校对.xml
```

写回时会把 `clipitem/name` 和图形层名一起改掉，导回 PR 后标签和画面对得上。

---

## 从字幕文件到工程（路线 A）

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

### 3.5 走 XML 交付时，同步剪辑名（可选）

Premiere 里改完字、导出 FCP7 XML 后，`clipitem/name` 往往还是旧字——
因为 name 是剪辑名，改文字不会改它。真正渲染的字在 `源文本` 参数的 base64 里。

```bash
python3 scripts/syncnames.py 唱词.xml -o 交付/唱词_name同步.xml
```

脚本以 base64 源文本为唯一真源，反写 `clipitem/name` 和图形层名。
只动 name，时间码/样式/源文本不动，默认不覆盖输入，可重复跑。
只带 `<hash>` 的条目会用同 hash 的其它条目反查文本。

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
| `--workdir DIR` | 写入白名单目录，默认输入字幕所在目录 |
| `--approve-overwrite` | 批准覆盖已存在且非本工具生成的文件 |
| `--interactive` | 覆盖前在终端询问（人用；agent 别加） |

### audioalign.py

| 参数 | 说明 |
|---|---|
| `--max-shift` | 最大吸附位移，默认 0.25s |
| `--join` | 连上阈值，默认 0.2s，`0` 关闭 |
| `--split-suggest` | 提示条内气口（拆条参考） |
| `--fps` | 帧率 |
| `--workdir DIR` | 写入白名单目录 |
| `--approve-overwrite` | 批准覆盖陌生文件 |
| `--interactive` | 覆盖前在终端询问 |

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
| `--workdir DIR` | 写入白名单目录，输出/存档/报告都要在里面 |
| `--approve-overwrite` | 批准覆盖陌生文件 |
| `--interactive` | 覆盖前在终端询问 |

### syncnames.py

| 参数 | 说明 |
|---|---|
| `xml` | 输入的 FCP7 XML（必填） |
| `-o, --out` | 输出路径，默认 `<输入>_name同步.xml`，不允许等于输入 |
| `--limit-preview` | 终端里最多打印多少条差异样例，默认 10 |

退出码：0 全部对齐；2 有条目定位不到文本（原样保留并列出）。

### prtext.py

| 参数 | 说明 |
|---|---|
| `prproj` | 要读的工程（必填） |
| `--track N` | 只取第几条视频轨（0 起） |
| `--txt / --json` | 清单输出路径，默认 `<工作目录>/字幕清单.txt / .json` |
| `--quiet` | 只打摘要，不打印全部条目 |

退出码：0 正常；2 有条目读不到文字（会列出序号）。

### prwrite.py

| 参数 | 说明 |
|---|---|
| `prproj` | 源工程（必填，不会被修改） |
| `--changes / --edits` | 改动 JSON 或 TSV（二选一，必填） |
| `--out` | 输出工程（必填，不能等于输入） |
| `--report` | 修改清单 Markdown |
| `--apply` | 真的写文件，默认 dry-run |
| `--workdir DIR` | 写入白名单目录 |
| `--approve-overwrite` | 批准覆盖陌生文件 |

退出码：0 成功；3 清单/工程对不上；4 回读校验没过（不写文件）。

### prverify.py

| 参数 | 说明 |
|---|---|
| `prproj` | 要校验的输出工程（必填） |
| `--template` | 模版工程，校验版本号/画幅/帧率/字体/位置 |
| `--against` | 对照工程，做逐对象字节与样式字节比对（最严格） |
| `--md / --json` | 报告输出路径 |
| `--max-pos-drift` | 位置允许偏移，默认 0.02（占画面比例） |

退出码：0 全过；5 有不一致；2 没给 `--template` 无法校验。

### xmltext.py

| 参数 | 说明 |
|---|---|
| `xml` | FCP7 XML（必填） |
| `--extract` | 列出每条字幕的序号/时间码/文本 |
| `--changes / --edits` | 改动 JSON 或 TSV |
| `-o, --out` | 输出 XML（改文本时必填） |
| `--no-sync-names` | 只改文本，不同步两处 name |
| `--txt` | `--extract` 的输出文件 |

### leakscan.py（公开仓库前自检）

| 参数 | 说明 |
|---|---|
| `--terms FILE` | 业务词表，一行一个（节目名、艺人名、客户名） |
| `--no-git` | 只扫工作区，跳过历史对象 |
| `--report MD` | 输出 Markdown 报告 |

查六类：工作区文件、Git 全部历史对象（含已删除的 blob）、提交者邮箱、
密钥模式、本机绝对路径、业务名。**退出码 1 就别推。**

```bash
python3 scripts/leakscan.py . --terms terms.local.txt
```

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

字体名、字号、描边、填充也都在同一段 blob 里，XML 属性里看不到。所以
「字体有没有被换」只能剥掉 blob 里的文本做指纹比对（`prverify.py` 的
样式字节检查）。另外 blob 总长必须是 4 的倍数，改完文字要补零对齐。

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
- **`prwrite.py` 只能改文字**，不能增删条目。要增删就在 Premiere 里做，
  或者走路线 A 用 `srt2pr.py` 重建整轨
- **写入被限制在 `--workdir` 内**，跨目录输出要先设 `--workdir`。这是刻意的，
  不是缺陷
- 安装器不删文件，所以从旧版升级后目标目录里可能残留已废弃的脚本；
  需要干净重装就手动移走旧目录或 `--force`

---

## License

MIT
