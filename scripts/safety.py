#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""safety.py — 写入边界、原始素材保护、变更日志。

这个模块存在的唯一目的：**让脚本不可能悄悄改坏用户的东西。**

四条硬约束（任何一条不满足就抛 SafetyError，直接终止）：

1. 写入白名单：只能写到 `workdir` 里面。默认 workdir 是输入文件所在目录，
   可以用 `--workdir` 显式指定。写到外面去一律拒绝，不做"问一下"。
2. 原始素材不可覆盖：输出路径与任何输入文件（字幕、模版工程、音视频）相同时
   直接拒绝。要改就在旁边生成新文件。
3. 覆盖别人的文件要人点头：目标文件已存在、且不是本工具以前写出来的，
   必须人工确认（TTY 交互，或显式 `--approve-overwrite`）。
4. 全程留痕：每次运行追加一行 JSONL 到 `<workdir>/.srt-proofread/changes.jsonl`，
   记录输入输出的路径、大小、sha256，以及是否经过人工批准。

用法：

    from safety import Guard, SafetyError

    g = Guard.build(workdir=None, inputs=[srt, template], approve=args.approve)
    out = g.check_output("交付.prproj")
    ...
    g.log(tool="srt2pr", args=vars(args), outputs=[out])
    g.verify_inputs_untouched()
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from typing import Iterable, List, Optional, Sequence

LOG_DIRNAME = ".srt-proofread"
LOG_FILENAME = "changes.jsonl"

# 绝对不允许当作写入目标的系统目录
_SYSTEM_ROOTS = (
    "/",
    "/bin",
    "/boot",
    "/cores",
    "/dev",
    "/etc",
    "/Library",
    "/opt",
    "/private",
    "/proc",
    "/sbin",
    "/System",
    "/usr",
    "/var",
    "/Applications",
)

# 不许在里面乱写文件的家目录子目录
_HOME_SENSITIVE = (
    ".ssh",
    ".aws",
    ".config",
    ".gnupg",
    ".kube",
    ".docker",
    "Library",
    ".Trash",
    "Desktop",
    "Documents",
    "Downloads",
)


class SafetyError(RuntimeError):
    """违反安全约束。调用方应把它转成人类看得懂的报错，并以非 0 退出。"""


def _real(path: str) -> str:
    """把路径规范成绝对真实路径（解开符号链接、去掉 ..）。"""
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _within(child: str, parent: str) -> bool:
    """child 是否在 parent 之内（parent 自身也算）。"""
    child = child.rstrip(os.sep) or os.sep
    parent = parent.rstrip(os.sep) or os.sep
    if child == parent:
        return True
    return child.startswith(parent + os.sep)


def _is_system_path(path: str) -> bool:
    p = path.rstrip(os.sep) or os.sep
    if p in _SYSTEM_ROOTS:
        return True
    # Windows 盘符根目录
    if len(p) == 3 and p[1] == ":" and p[2] in ("\\", "/"):
        return True
    low = p.lower()
    return low.startswith("c:\\windows") or low.startswith("c:\\program files")


def _home() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _ask(prompt: str) -> bool:
    """问人。没有 TTY 时不猜，直接当拒绝。"""
    if not sys.stdin.isatty():
        return False
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        return sys.stdin.readline().strip().lower() in ("y", "yes", "是")
    except (OSError, KeyboardInterrupt):
        return False


class Guard:
    def __init__(
        self,
        workdir: str,
        inputs: Sequence[str] = (),
        approve_overwrite: bool = False,
        log_path: Optional[str] = None,
        interactive: bool = False,
    ) -> None:
        if not workdir:
            raise SafetyError("没有确定工作目录（workdir），拒绝写入")
        self.workdir = _real(workdir)
        if _is_system_path(self.workdir):
            raise SafetyError("工作目录不能是系统目录：%s" % self.workdir)
        if self.workdir == _home():
            raise SafetyError("工作目录不能直接是家目录，请指到具体项目目录：%s" % self.workdir)
        rel = os.path.relpath(self.workdir, _home())
        if not rel.startswith("..") and rel.split(os.sep)[0] in _HOME_SENSITIVE:
            raise SafetyError(
                "工作目录不能是家目录下的 %s（高风险，容易误伤），请指到项目子目录"
                % rel.split(os.sep)[0]
            )
        if not os.path.isdir(self.workdir):
            raise SafetyError("工作目录不存在：%s" % self.workdir)

        self.approve_overwrite = bool(approve_overwrite)
        # 只在人坐在终端前时才问。agent 跑脚本时绝不阻塞等待输入，
        # 否则会挂死 —— 必须由调用方显式带 --approve-overwrite。
        self.interactive = bool(interactive) and sys.stdin.isatty()
        self._inputs: dict = {}
        for p in inputs:
            if not p:
                continue
            rp = _real(p)
            if os.path.exists(rp):
                self._inputs[rp] = sha256_of(rp)
        self.log_path = log_path or os.path.join(self.workdir, LOG_DIRNAME, LOG_FILENAME)
        self._written: List[str] = []
        self._approved: List[str] = []

    # ---------------------------------------------------------------- 构造函数
    @classmethod
    def build(
        cls,
        workdir: Optional[str],
        primary_input: str,
        inputs: Sequence[str] = (),
        approve: bool = False,
        interactive: bool = False,
    ) -> "Guard":
        """workdir 为空时，退回到主输入文件所在目录。"""
        wd = workdir or os.path.dirname(_real(primary_input)) or os.getcwd()
        return cls(
            wd,
            inputs=list(inputs) + [primary_input],
            approve_overwrite=approve,
            interactive=interactive,
        )

    # ------------------------------------------------------------------ 检查
    def check_output(self, path: str, *, what: str = "输出") -> str:
        """校验一个待写路径，返回规范化后的绝对路径。不合格就抛 SafetyError。"""
        if not path:
            raise SafetyError("%s 路径为空" % what)
        p = _real(path)

        if _is_system_path(p):
            raise SafetyError("拒绝写入系统目录：%s" % p)

        if not _within(p, self.workdir):
            raise SafetyError(
                "%s 不在工作目录白名单内：\n  路径   %s\n  白名单 %s\n"
                "如果确实要写到那里，请加 --workdir 把该目录设为工作目录。" % (what, p, self.workdir)
            )

        if p in self._inputs:
            raise SafetyError(
                "拒绝覆盖原始素材：%s\n"
                "这个文件是本轮的输入。请换一个输出文件名（脚本自己加后缀也行），"
                "原文件必须保持原样。" % p
            )

        if os.path.isdir(p):
            raise SafetyError("%s 是一个目录，不能当文件写：%s" % (what, p))

        parent = os.path.dirname(p)
        if not os.path.isdir(parent):
            raise SafetyError("%s 所在目录不存在：%s" % (what, parent))

        if os.path.lexists(p) and os.path.getsize(p) > 0:
            if p in self._previous_outputs():
                return p  # 本工具自己以前写的，允许覆盖（幂等重跑）
            if self.approve_overwrite:
                self._approved.append(p)
                return p
            if self.interactive:
                ok = _ask("⚠️  %s 已存在，且不是本工具生成的：\n   %s\n   覆盖？[y/N] " % (what, p))
                if ok:
                    self._approved.append(p)
                    return p
                raise SafetyError("用户拒绝覆盖：%s" % p)
            raise SafetyError(
                "目标文件已存在且未获批准：%s\n"
                "改个输出名；确认内容无误要覆盖的话，加 --approve-overwrite 重跑"
                "（人在终端前也可以用 --interactive 让它当场问你）。" % p
            )
        return p

    def check_report_dir(self, path: str) -> str:
        """报告目录：可以新建，但必须落在白名单里。"""
        p = _real(path)
        if not _within(p, self.workdir):
            raise SafetyError("报告路径不在工作目录白名单内：%s" % p)
        return p

    # ------------------------------------------------------------------ 日志
    def _previous_outputs(self) -> set:
        out = set()
        try:
            with open(self.log_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    for o in rec.get("outputs", []):
                        if o.get("path"):
                            out.add(_real(o["path"]))
        except OSError:
            pass
        return out

    def log(
        self,
        tool: str,
        args: Optional[dict] = None,
        outputs: Iterable[str] = (),
        note: str = "",
    ) -> str:
        """追加一行变更记录，返回日志路径。"""
        ents = []
        for p in outputs:
            rp = _real(p)
            try:
                size = os.path.getsize(rp)
            except OSError:
                size = -1
            ents.append(
                {
                    "path": rp,
                    "bytes": size,
                    "sha256": sha256_of(rp),
                    "approved": rp in self._approved,
                }
            )
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": tool,
            "workdir": self.workdir,
            "inputs": [
                {"path": k, "bytes": os.path.getsize(k) if os.path.exists(k) else -1, "sha256": v}
                for k, v in sorted(self._inputs.items())
            ],
            "outputs": ents,
            "note": note,
        }
        if args:
            # 只留路径类的值，字典/对象一律转字符串，避免把大块内容写进日志
            rec["args"] = {
                k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
                for k, v in args.items()
                if not str(k).startswith("_")
            }
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return self.log_path

    def verify_inputs_untouched(self) -> None:
        """收尾自检：输入文件一个字节都不许变。"""
        bad = []
        for p, h in self._inputs.items():
            if not os.path.exists(p):
                bad.append((p, "文件不见了"))
            elif sha256_of(p) != h:
                bad.append((p, "内容被改动"))
        if bad:
            detail = "\n".join("  %s（%s）" % (p, why) for p, why in bad)
            raise SafetyError("输入素材在运行期间被改动，请立即检查：\n%s" % detail)

    # ---------------------------------------------------------------- 报告
    def describe(self) -> str:
        return "工作目录白名单：%s\n变更日志：%s" % (self.workdir, self.log_path)


def fail(err: Exception, tool: str) -> int:
    """统一的安全失败出口：人话报错 + 非 0 退出码，不打印堆栈。"""
    print("[%s] 已拒绝执行：%s" % (tool, err), file=sys.stderr)
    return 3
