#!/usr/bin/env python3
"""
check_deps.py — sync-docsite-to-lexiang 依赖检查工具

独立可执行（不需要任何第三方包），用于：

  1) 安装完成后立即验证依赖（避免 sync 时才发现缺 upload-markdown-to-lexiang）
  2) 排查「未找到 upload-markdown-to-lexiang CLI」「401 鉴权失败」等问题

检查项：
  - Python 版本（>= 3.9）
  - upload-markdown-to-lexiang CLI 是否在已知路径
  - 乐享凭证（~/.config/lexiang-upload/profiles/*.json / OAuth token / LEXIANG_UPLOADER_HOME）

退出码：
  - 0：所有关键依赖就绪
  - 1：关键依赖缺失（但可跑 --probe-only / --dry-run）
  - 2：Python 版本不满足

Usage:
  python3 scripts/check_deps.py
  python3 scripts/check_deps.py --json     # 输出 JSON 摘要供程序消费
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
from typing import Any

MIN_PY = (3, 9)
UPLOADER_REL = os.path.join("upload-markdown-to-lexiang", "scripts", "lexiang_upload.py")


def _check_python() -> dict[str, Any]:
    v = sys.version_info
    ok = v >= MIN_PY
    return {
        "name": "Python",
        "need": f">= {MIN_PY[0]}.{MIN_PY[1]}",
        "have": f"{v.major}.{v.minor}.{v.micro}",
        "ok": ok,
        "fix": None if ok else f"升级 Python 到 {MIN_PY[0]}.{MIN_PY[1]} 或更高（建议用 Homebrew/系统包管理器）",
    }


def _check_uploader_cli() -> dict[str, Any]:
    """定位 upload-markdown-to-lexiang 的 lexiang_upload.py。"""
    candidates: list[tuple[str, str]] = []

    env_home = os.environ.get("LEXIANG_UPLOADER_HOME", "").strip()
    if env_home:
        candidates.append((
            f"$LEXIANG_UPLOADER_HOME/scripts/lexiang_upload.py",
            os.path.join(env_home, "scripts", "lexiang_upload.py"),
        ))

    here = os.path.dirname(os.path.abspath(__file__))
    skills_root = os.path.dirname(os.path.dirname(here))
    candidates.append((
        f"skills_root/{UPLOADER_REL}",
        os.path.join(skills_root, UPLOADER_REL),
    ))
    candidates.append((
        "~/.workbuddy/skills/upload-markdown-to-lexiang/scripts/lexiang_upload.py",
        os.path.expanduser("~/.workbuddy/skills/upload-markdown-to-lexiang/scripts/lexiang_upload.py"),
    ))

    for label, path in candidates:
        if os.path.isfile(path):
            return {
                "name": "upload-markdown-to-lexiang CLI",
                "need": "必备（正式同步依赖；probe-only/dry-run 不需要）",
                "have": label,
                "ok": True,
                "fix": None,
            }

    fix = (
        "需要先安装 upload-markdown-to-lexiang：\n"
        "    git clone https://github.com/ajaxhe/upload-markdown-to-lexiang.git \\\n"
        "      ~/.workbuddy/skills/upload-markdown-to-lexiang\n"
        "（或任何自定义 skills 根目录下；脚本会按 LEXIANG_UPLOADER_HOME > 同根 > ~/.workbuddy/skills 顺序查找）"
    )
    return {
        "name": "upload-markdown-to-lexiang CLI",
        "need": "必备（正式同步依赖；probe-only/dry-run 不需要）",
        "have": "未找到",
        "ok": False,
        "fix": fix,
    }


def _check_lexiang_credentials() -> dict[str, Any]:
    """检查乐享凭证（任一来源存在即可）。"""
    found: list[str] = []

    # 1) LEXIANG_UPLOADER_HOME 环境变量
    if os.environ.get("LEXIANG_UPLOADER_HOME", "").strip():
        found.append("$LEXIANG_UPLOADER_HOME")

    # 2) ~/.config/lexiang-upload/profiles/*.json（多企业 profile）
    profile_dir = os.path.expanduser("~/.config/lexiang-upload/profiles")
    if os.path.isdir(profile_dir):
        profs = sorted(glob.glob(os.path.join(profile_dir, "*.json")))
        for p in profs:
            if os.path.isfile(p):
                found.append(os.path.basename(p))

    # 3) WorkBuddy/QClaw/CodeBuddy 内置乐享连接器 OAuth token 文件
    for pat in (
        "~/.workbuddy/connectors/*/tokens/lexiang-*.txt",
        "~/.qclaw/connectors/*/tokens/lexiang-*.txt",
    ):
        for f in glob.glob(os.path.expanduser(pat)):
            if os.path.isfile(f):
                found.append(os.path.relpath(f, os.path.expanduser("~")))

    if found:
        return {
            "name": "乐享凭证",
            "need": "至少一份（MCP 连接器 OAuth 或 upload-markdown 个人 lxmcp_ token）",
            "have": ", ".join(found[:3]) + (" ..." if len(found) > 3 else ""),
            "ok": True,
            "fix": None,
        }

    fix = (
        "缺少乐享凭证。两种任选：\n"
        "  A) WorkBuddy/QClaw/CodeBuddy 已内置乐享连接器（OAuth token），登录 Agent 后会自动写入 ~/.workbuddy/connectors/*/tokens/lexiang-*.txt\n"
        "  B) 个人 MCP token：访问 https://lexiangla.com/ai/claw 获取 lxmcp_ 凭证，然后：\n"
        "       mkdir -p ~/.config/lexiang-upload/profiles\n"
        "       python3 ~/.workbuddy/skills/upload-markdown-to-lexiang/scripts/lexiang_upload.py auth login"
    )
    return {
        "name": "乐享凭证",
        "need": "至少一份（MCP 连接器 OAuth 或 upload-markdown 个人 lxmcp_ token）",
        "have": "未找到",
        "ok": False,
        "fix": fix,
    }


def _print_human(results: list[dict[str, Any]], exit_code: int) -> None:
    icon = {True: "✓", False: "✗"}
    print("=" * 60)
    print("sync-docsite-to-lexiang 依赖检查")
    print("=" * 60)
    for r in results:
        mark = icon[r["ok"]]
        print(f"\n[{mark}] {r['name']}")
        print(f"    需要: {r['need']}")
        print(f"    当前: {r['have']}")
        if not r["ok"] and r["fix"]:
            print(f"    修复:\n      " + r["fix"].replace("\n", "\n      "))

    print("\n" + "=" * 60)
    if exit_code == 0:
        print("结果：所有关键依赖就绪，可正式同步。")
    elif exit_code == 1:
        print("结果：关键依赖缺失。probe-only / dry-run 仍可用，正式同步前请先修复 ✗ 项。")
    elif exit_code == 2:
        print(f"结果：Python 版本不满足（需要 >= {MIN_PY[0]}.{MIN_PY[1]}），无法运行。")
    print("=" * 60)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="sync-docsite-to-lexiang 依赖检查（stdlib only）",
    )
    ap.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = ap.parse_args()

    results = [
        _check_python(),
        _check_uploader_cli(),
        _check_lexiang_credentials(),
    ]

    if results[0]["ok"] is False:
        code = 2
    elif all(r["ok"] for r in results):
        code = 0
    else:
        code = 1

    summary = {
        "exit_code": code,
        "ready": code == 0,
        "checks": results,
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_human(results, code)

    return code


if __name__ == "__main__":
    sys.exit(main())