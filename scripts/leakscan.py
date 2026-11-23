#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leakscan.py — 提交前的泄露扫描。

公开仓库翻车通常不是"忘了脱敏"这么简单，而是忘在某个角落：
历史提交里的旧版本、提交者邮箱、文档里的本机绝对路径、示例里残留的真名。

所以这个脚本查六类东西：

1. 工作区所有文件（含未跟踪的）里的敏感模式
2. **Git 全部对象**（不只是当前版本，包括被删掉的历史 blob）
3. 提交者 / 作者邮箱
4. 密钥类模式（AK、sk-、ghp_、私钥头、password= 之类）
5. 本机绝对路径（家目录的绝对路径、盘符路径、外接卷名）
6. 业务名（节目名、艺人名、客户名）—— 这类没法靠正则猜，
   要自己提供词表，一行一个：

       ./leakscan.py . --terms terms.local.txt

词表本身别提交（.gitignore 已经挡了 `leakscan-terms.txt`）。

用法：
    python3 scripts/leakscan.py .                       # 扫当前目录
    python3 scripts/leakscan.py . --no-git              # 只扫工作区
    python3 scripts/leakscan.py . --terms terms.txt --report scan.md

退出码：0 干净，1 有问题。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import Iterable, List, Optional, Sequence, Tuple

MAX_BYTES = 4 << 20  # 单个文件/对象最多看 4MB，再大就跳过并提示

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".mp4", ".mov", ".mxf", ".wav", ".mp3", ".aif", ".psd", ".ttf", ".otf",
    ".woff", ".woff2", ".so", ".dylib", ".exe", ".dll", ".prproj",
}

# (名称, 正则, 级别, 提示)
PATTERNS: List[Tuple[str, "re.Pattern[str]", str, str]] = [
    # ---- 本机路径 ----
    ("local_path_mac", re.compile(r"/Users/([A-Za-z0-9._-]+)"), "error", "本机用户目录绝对路径"),
    ("local_path_linux", re.compile(r"/home/([A-Za-z0-9._-]+)"), "error", "本机用户目录绝对路径"),
    ("local_path_win", re.compile(r"[A-Za-z]:\\\\?Users\\\\?([A-Za-z0-9._-]+)"), "error", "本机用户目录绝对路径"),
    ("local_path_vol", re.compile(r"/Volumes/([^/\s\"']+)"), "warn", "外接盘/挂载卷名，可能暴露项目名"),  # leakscan:ignore
    ("temp_path", re.compile(r"/private/var/folders/|/tmp/[A-Za-z0-9._-]{6,}/"), "warn", "临时目录绝对路径"),  # leakscan:ignore
    # ---- 密钥 ----
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "error", "AWS Access Key"),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "error", "API secret key"),
    ("github_pat", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "error", "GitHub token"),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "error", "Slack token"),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "error", "私钥文件内容"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "warn", "JWT"),
    ("password_assign", re.compile(r"(?i)\b(password|passwd|secret|token)\s*[:=]\s*['\"][^'\"\s]{6,}['\"]"), "error", "硬编码口令"),
    # ---- 个人信息 ----
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "warn", "邮箱"),
    ("phone_cn", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "warn", "手机号"),
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "error", "身份证号"),
]

# 这些邮箱出现在开源仓库里是正常的，不算泄露
EMAIL_OK = re.compile(r"(@users\.noreply\.github\.com|@example\.(com|org|net)|@anthropic\.com)$", re.I)

# 不该被跟踪的文件类型（真实素材）
RISKY_TRACKED = re.compile(
    r"\.(prproj|aep|aepx|mogrt|mp4|mov|mxf|wav|mp3|aif|psd)$", re.I
)
# 示例文件例外
SAFE_TRACKED = re.compile(r"(^|/)examples/", re.I)


class Hit:
    __slots__ = ("where", "kind", "level", "snippet", "hint")

    def __init__(self, where: str, kind: str, level: str, snippet: str, hint: str) -> None:
        self.where = where
        self.kind = kind
        self.level = level
        self.snippet = snippet
        self.hint = hint

    def __str__(self) -> str:
        return "[%s] %-16s %s  ← %s" % (self.level.upper(), self.kind, self.where, self.snippet)


def _decodable(data: bytes) -> Optional[str]:
    if b"\x00" in data[:4096]:
        return None
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def scan_text(text: str, where: str, terms: Sequence[str]) -> List[Hit]:
    hits: List[Hit] = []
    lines = text.splitlines()
    # 行级豁免：这一行确实要写敏感字面量（比如规则定义本身）时用它
    ignored = {i + 1 for i, ln in enumerate(lines) if "leakscan:ignore" in ln}
    for name, pat, level, hint in PATTERNS:
        for m in pat.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            snippet = lines[line_no - 1].strip()[:120] if line_no - 1 < len(lines) else m.group(0)
            if name == "email" and EMAIL_OK.search(m.group(0)):
                continue
            if line_no in ignored:
                continue
            hits.append(Hit("%s:%d" % (where, line_no), name, level, snippet, hint))
    for term in terms:
        if not term:
            continue
        for m in re.finditer(re.escape(term), text):
            line_no = text.count("\n", 0, m.start()) + 1
            if line_no in ignored:
                continue
            snippet = lines[line_no - 1].strip()[:120] if line_no - 1 < len(lines) else term
            hits.append(Hit("%s:%d" % (where, line_no), "term", "error", snippet, "业务名/人名：%s" % term))
    return hits


def _skip_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in BINARY_EXT:
        return True
    base = os.path.basename(path)
    return base in (".DS_Store",)


def iter_worktree(root: str, skip_dirs: Iterable[str]) -> Iterable[Tuple[str, bytes]]:
    skip = set(skip_dirs)
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in skip)
        for n in sorted(names):
            full = os.path.join(base, n)
            if _skip_file(full) or os.path.islink(full):
                continue
            try:
                if os.path.getsize(full) > MAX_BYTES:
                    continue
                yield os.path.relpath(full, root), open(full, "rb").read()
            except OSError:
                continue


def _git(root: str, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", root] + list(args), capture_output=True, text=True, errors="replace"
    )
    return p.stdout if p.returncode == 0 else ""


def is_git_repo(root: str) -> bool:
    return bool(_git(root, "rev-parse", "--git-dir").strip())


def git_blobs(root: str) -> Iterable[Tuple[str, bytes]]:
    """遍历全部历史对象（含被删除文件），这是最容易被忘掉的一处。"""
    listing = _git(root, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype) %(objectsize)")
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        sha, _, size = parts
        if int(size) > MAX_BYTES:
            continue
        p = subprocess.run(
            ["git", "-C", root, "cat-file", "blob", sha], capture_output=True
        )
        if p.returncode == 0:
            yield sha[:10], p.stdout


def git_authors(root: str) -> List[Tuple[str, str, str]]:
    out = _git(root, "log", "--all", "--format=%an|%ae|%cn|%ce")
    seen = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 4 and line not in [s[0] for s in seen]:
            seen.append((line, parts[0], parts[1]))
    return seen


def git_tracked_risky(root: str) -> List[str]:
    files = _git(root, "ls-files").splitlines()
    return [f for f in files if RISKY_TRACKED.search(f) and not SAFE_TRACKED.search(f)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="提交前泄露扫描")
    ap.add_argument("path", nargs="?", default=".", help="仓库根目录")
    ap.add_argument("--terms", default=None, help="业务词表（一行一个），查节目名/艺人名/客户名")
    ap.add_argument("--no-git", action="store_true", help="跳过 git 对象与历史作者检查")
    ap.add_argument("--report", default=None, help="输出 Markdown 报告")
    ap.add_argument("--quiet", action="store_true", help="只打印结论行")
    args = ap.parse_args(argv)

    root = os.path.realpath(os.path.abspath(args.path))
    if not os.path.isdir(root):
        print("目录不存在：%s" % root, file=sys.stderr)
        return 2

    terms: List[str] = []
    if args.terms:
        try:
            with open(args.terms, "r", encoding="utf-8") as fh:
                terms = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
        except OSError as e:
            print("读不了词表：%s" % e, file=sys.stderr)
            return 2

    hits: List[Hit] = []
    scanned_files = 0
    for rel, data in iter_worktree(root, skip_dirs=[".git", "__pycache__", ".srt-proofread", "node_modules"]):
        text = _decodable(data)
        scanned_files += 1
        if text:
            hits += scan_text(text, rel, terms)

    extra_notes: List[str] = []
    if not args.no_git and is_git_repo(root):
        for sha, data in git_blobs(root):
            text = _decodable(data)
            if text:
                hits += scan_text(text, "git:%s" % sha, terms)
        for line, an, ae in git_authors(root):
            _, _, cn, ce = line.split("|")
            for who in (ae, ce):
                if who and not EMAIL_OK.search(who):
                    hits.append(Hit("git-author", "author_email", "error", "%s <%s>" % (an, who), "提交者真实邮箱"))
            if re.search(r"@(qq|163|126|gmail|outlook|hotmail|foxmail|sina)\.", ae + ce):
                extra_notes.append("提交邮箱是常见个人邮箱，建议换成 GitHub noreply")
        risky = git_tracked_risky(root)
        if risky:
            extra_notes.append("仓库跟踪了真实素材文件：%s" % ", ".join(risky[:10]))

    # 去重（同一位置同一规则只留一条）
    uniq: List[Hit] = []
    seen = set()
    for h in hits:
        k = (h.where, h.kind, h.snippet)
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    hits = uniq

    errors = [h for h in hits if h.level == "error"]
    warns = [h for h in hits if h.level != "error"]

    lines = ["# 泄露扫描报告", ""]
    lines.append("- 扫描目录：`%s`" % root)
    lines.append("- 工作区文件：%d 个（跳过二进制/超 %dMB）" % (scanned_files, MAX_BYTES >> 20))
    lines.append("- Git 对象：%s" % ("已扫描（含历史 blob）" if not args.no_git and is_git_repo(root) else "跳过"))
    lines.append("- 业务词表：%s" % ("%d 条" % len(terms) if terms else "未提供（节目名/人名无法自动发现）"))
    lines.append("- 结果：**%d 严重 / %d 提示**" % (len(errors), len(warns)))
    if extra_notes:
        lines += ["", "## 附加提示", ""] + ["- " + n for n in extra_notes]
    if hits:
        lines += ["", "## 明细", "", "| 位置 | 类型 | 级别 | 内容 | 说明 |", "|---|---|---|---|---|"]
        for h in hits:
            lines.append(
                "| `%s` | %s | %s | `%s` | %s |"
                % (h.where, h.kind, h.level, h.snippet.replace("|", "\\|"), h.hint)
            )
    else:
        lines += ["", "没有发现问题。"]

    md = "\n".join(lines) + "\n"
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(md)
    if args.quiet:
        print("%d 严重 / %d 提示" % (len(errors), len(warns)))
    else:
        print(md)
    if not args.quiet and not hits:
        print("干净。", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
