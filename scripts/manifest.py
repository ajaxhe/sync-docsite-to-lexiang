#!/usr/bin/env python3
"""
manifest.py - 站点同步状态管理

状态目录：~/.config/lexiang-websync/{site_key}/
  config.json    任务配置（入口 URL、目标、范围、删除模式）
  manifest.json  同步清单（源路径 → entry_id 映射、内容 hash、更新时间）
  reports/       同步报告

site_key = 域名（www. 等前缀已剥离）。
状态跟随「站点+目标」走，跨会话可续传。
"""

import errno
import hashlib
import json
import os
import time
from pathlib import Path

STATE_ROOT = Path("~/.config/lexiang-websync").expanduser()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def site_key(entry_url):
    """从入口 URL 提取状态键：域名。"""
    import urllib.parse
    netloc = urllib.parse.urlparse(entry_url).netloc.lower()
    netloc = netloc.split("@")[-1].split(":")[0]
    for prefix in ("www.", "docs.", "help."):
        if netloc.startswith(prefix):
            netloc = netloc[len(prefix):]
    return netloc or "site"


def _safe_mkdir(p):
    """mkdir 的 broker 兼容版：先判断存在性，再 try/except 兜底 EEXIST。"""
    if p.exists():
        return
    try:
        p.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        pass
    except OSError as e:
        # 某些拦截层不透传 exist_ok 语义，errno 为 EEXIST 时视为成功
        if getattr(e, "errno", None) != errno.EEXIST or not p.exists():
            raise


def state_dir(entry_url):
    d = STATE_ROOT / site_key(entry_url)
    _safe_mkdir(d)
    _safe_mkdir(d / "reports")
    return d


def compute_hash(*parts):
    """内容 hash：多个字符串/字节串拼接后 SHA256。"""
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, str):
            p = p.encode("utf-8")
        h.update(p)
    return h.hexdigest()


def load_config(entry_url):
    path = state_dir(entry_url) / "config.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(entry_url, config):
    path = state_dir(entry_url) / "config.json"
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")


def load_manifest(entry_url):
    path = state_dir(entry_url) / "manifest.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "last_sync": "", "pages": {}, "folders": {},
            "trash_folder_id": ""}


def save_manifest(entry_url, manifest):
    path = state_dir(entry_url) / "manifest.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def report_path(entry_url):
    d = state_dir(entry_url) / "reports"
    return d / f"sync_{time.strftime('%Y%m%d_%H%M%S')}.md"


class SyncLockError(Exception):
    pass


def acquire_lock(entry_url):
    """简单文件锁：防两个进程同时同步同一站点。"""
    lock = state_dir(entry_url) / ".sync.lock"
    if lock.exists():
        age = time.time() - lock.stat().st_mtime
        if age < 3600:
            raise SyncLockError(
                f"已有同步在进行（锁存活 {int(age)}s）：{lock}。"
                "如确认无并发，删除该锁文件后重试。")
    lock.write_text(str(os.getpid()))
    return lock


def release_lock(entry_url):
    lock = state_dir(entry_url) / ".sync.lock"
    if lock.exists():
        lock.unlink(missing_ok=True)
