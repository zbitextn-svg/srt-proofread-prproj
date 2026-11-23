# 权限边界：这个脚本能动什么、不能动什么

字幕文件通常是**唯一的原始素材**——剪辑师手上一份，重录一次要花半天。
所以这一层的设计原则是：**宁可拒绝执行，也不要"猜用户应该不介意"。**

四条约束，代码在 `scripts/safety.py`，违反任何一条直接退出码 3。

---

## 1. 写入白名单：只能写进 workdir

`workdir` 默认取**输入字幕所在目录**，可用 `--workdir` 改。

```
输入：/项目/第一组/字幕.srt
默认白名单：/项目/第一组/
```

三个输出参数（`--out`、`--report`、`--save-srt`）都必须落在白名单里，
写出去就拒绝：

```
[proofread] 已拒绝执行：字幕输出 不在工作目录白名单内：
  路径   /tmp/outside.srt
  白名单 /项目/第一组
```

要写到别处，只有一个正当做法：把那个目录显式声明成工作目录。

```bash
python3 scripts/proofread.py 字幕.srt --workdir /项目/交付 --report /项目/交付/报告.md
```

同时硬拒绝这些位置：`/`、`/System`、`/usr`、`/etc`、`/Library`、`C:\Windows`、
家目录本身、以及 `~/Desktop`、`~/Downloads`、`~/Documents`、`~/.ssh` 这类
"里面全是用户自己的东西"的目录。防的是 `--workdir ~` 这种手滑。

---

## 2. 原始素材不可覆盖

输出路径等于任何一个输入文件（字幕、模版工程、音视频）时，**直接拒绝**，
不给"要不要覆盖"的选项：

```
[proofread] 已拒绝执行：拒绝覆盖原始素材：/项目/第一组/字幕.srt
这个文件是本轮的输入。请换一个输出文件名，原文件必须保持原样。
```

跑完还会再验一次：所有输入文件在运行前后的 sha256 必须一致。
不一致就报错——说明有别的进程动过，或者脚本有 bug。

```
[proofread] 已拒绝执行：输入素材在运行期间被改动，请立即检查：
  /项目/第一组/字幕.srt（内容被改动）
```

想就地改？明确换个名字。

```bash
python3 scripts/proofread.py 字幕.srt --out 字幕_校对版.srt --apply
```

---

## 3. 覆盖陌生文件要人点头

目标文件已存在时，分三种情况：

| 情况 | 行为 |
|---|---|
| 本工具以前写出来的（日志里有记录） | 直接覆盖，重跑幂等 |
| 陌生文件，带了 `--approve-overwrite` | 覆盖，日志标 `approved: true` |
| 陌生文件，没带参数 | **拒绝**，退出码 3 |

```
目标文件已存在且未获批准：/项目/第一组/报告.md
改个输出名；确认内容无误要覆盖的话，加 --approve-overwrite 重跑
```

人坐在终端前想被问一句，加 `--interactive`：

```
⚠️  报告输出 已存在，且不是本工具生成的：
   /项目/第一组/报告.md
   覆盖？[y/N]
```

**agent 不要加 `--interactive`。** 没有 TTY 时它不会阻塞（直接当拒绝），
但有些宿主会给脚本分配伪终端，一旦傻等就成了挂死。

---

## 4. 变更日志

每次有写入，就往 `<workdir>/.srt-proofread/changes.jsonl` 追加一行：

```json
{
  "ts": "2026-11-23T17:15:20+0800",
  "tool": "proofread",
  "workdir": "/项目/第一组",
  "inputs": [
    {"path": "/项目/第一组/字幕.srt", "bytes": 412, "sha256": "21d1d86b…"}
  ],
  "outputs": [
    {"path": "/项目/第一组/报告.md", "bytes": 1359, "sha256": "49c029ba…", "approved": false}
  ],
  "note": "10 issues",
  "args": {"srt": "字幕.srt", "report": "报告.md", "apply": false, "fps": 25.0}
}
```

用途：

- 事后追"这份工程是谁生成的、从哪份字幕来的、动没动原始素材"
- 判断某个文件能不能安全覆盖（"自己人"名单就是这个日志）
- 交付时把日志一起给出去，等于一份操作凭证

日志只记路径、大小、哈希，**不记字幕内容**——它本身不该成为新的泄露源。

---

## 建议的目录布局

```
项目/
├── 素材/                # 只读：字幕、音视频、模版工程
│   ├── 第一组.srt
│   ├── 模版.prproj
│   └── 原片.mp4
└── 交付/                # 白名单：所有输出都进这里
    ├── 第一组_校对版.srt
    ├── 第一组_修改清单.md
    ├── 第一组.prproj
    └── .srt-proofread/changes.jsonl
```

```bash
python3 scripts/srt2pr.py 素材/第一组.srt \
    --template 素材/模版.prproj --align 素材/原片.mp4 \
    --workdir 交付 --out 交付/第一组.prproj --join 0.2
```

输入在 `素材/`，输出只能落在 `交付/`。中间产物不可能混进素材目录。

---

## install.sh 的安全行为

老版本的安装脚本用了 `rsync -a --delete`，目标目录里**原有的东西会被删掉**。
如果有人手滑 `./install.sh ~`，那就是一场灾难。现在：

| 检查 | 行为 |
|---|---|
| 目标不是绝对路径 / 含 `..` / 是 `/` / 是家目录 | 拒绝 |
| 目标不在 `~/.workbuddy/skills`、`~/.claude/skills`、`~/.config/agent/skills` 下 | 拒绝 |
| 目标已存在且非空、又不是本技能的安装点 | 拒绝，提示换目录或加 `--force` |
| `--force` 且目标非空 | **改名为 `<目标>.bak.<时间戳>` 备份**，然后装新的 |
| 安装方式 | 逐个 `cp`，没有任何删除操作 |

装完在目标目录留一个 `.srt-proofread-installed` 标记，下次升级就靠它认门。
从 v1（无标记）升级 v2，需要 `--force` 一次。

想验证脚本确实不删东西，可以自己试：

```bash
echo test > ~/.workbuddy/skills/srt-proofread-prproj/MY-FILE.txt
./install.sh
ls ~/.workbuddy/skills/srt-proofread-prproj/MY-FILE.txt   # 还在
```

---

## 公开仓库的分界线

代码进仓库，业务数据不进。`.gitignore` 挡的是这些：

| 类别 | 例子 |
|---|---|
| 字幕与扒词稿 | `*.srt`、`*.ass`、`*唱词*.txt`、`*修改清单*.md` |
| 剪辑工程 | `*.prproj`、`*.aep`、`*.fcpxml` |
| 音视频素材 | `*.mp4`、`*.mov`、`*.wav` |
| 业务词表 | `*词表*.json`、`*名单*.csv`、`rules.local.json` |
| 凭据 | `.env`、`*.pem`、`*.key` |
| 运行产物 | `.srt-proofread/`、`*.bak`、`out/` |

`examples/demo.srt` 是例外（`!examples/demo.srt`）——空的示例留着，
新人 clone 下来才跑得通。

提交前跑一次扫描：

```bash
python3 scripts/leakscan.py . --terms terms.local.txt
```

它查六类：工作区文件、**Git 全部历史对象**（删掉的文件仍在里面）、
提交者邮箱、密钥模式、本机绝对路径、业务名（要自己给词表）。
退出码 1 就别推。

历史里已经有敏感内容时，改文件没用——得 `filter-repo` 或重建仓库。
这也是为什么这个脚本默认连 `.git` 里的旧 blob 一起扫。
