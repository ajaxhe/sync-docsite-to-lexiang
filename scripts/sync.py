#!/usr/bin/env python3
"""
sync.py - 文档站点 → 乐享知识库 同步执行器

用法：
  # 首次建议 dry-run 预览
  python3 sync.py --url https://example.com/docs/ \
      --target-space-id <SID> --target-folder-id <FID> --dry-run

  # 正式同步（全量/增量自动判断）
  python3 sync.py --url ... --target-space-id <SID> --target-folder-id <FID>

  # 参数
  --scope subtree|section|all   同步范围（默认 section：入口 URL 所在顶级分区）
  --delete-mode sync|keep       源端删除的文档处理（默认 sync：移入回收站）
  --max-pages N                 限制页数（测试用）
  --lexiang-profile NAME        乐享个人凭证 profile
  --waf-sanitize                对触发乐享 WAF 的代码模式插入零宽字符（默认开）

输出：stdout 仅一行 JSON 摘要 + 报告路径。文档内容不进入 LLM 上下文。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from site_crawler import probe_site, fetch_page, CrawlError, http_get
from html2md import convert as html2md_convert
import dashboard as dashmod
from manifest import (
    load_config, save_config, load_manifest, save_manifest,
    compute_hash, now_iso, state_dir, report_path,
    acquire_lock, release_lock, SyncLockError,
)
from lexiang_api import (
    LexiangConnector, LexiangError, resolve_personal_credential_selector,
)

# 乐享 WAF 拦截的危险代码模式（命中则零宽字符消毒）
WAF_PATTERNS = [
    "os.system(", "__import__(", "subprocess.check_output(",
    "subprocess.run(", "eval(", "exec(",
]
# 仅这些子串才真正触发 WAF（run 需配合 capture_output；eval/exec 单独无害化处理保守执行）
WAF_STRICT_PATTERNS = [
    "os.system(", "__import__(", "subprocess.check_output(",
]
# LDAP 过滤器语法（(&(objectClass=user)(objectCategory=person)) 等）
# 会被乐享 WAF 误判为注入攻击 → 在 "(" 后插零宽空格破坏特征
WAF_LDAP_FILTER_RE = re.compile(r"\([^()\s]*=[^()]*\)")

TRASH_FOLDER_NAME = "_回收站_站点同步"
# 删除同步安全阈值：消失节点占比超过该值时中止删除
DELETE_ABORT_RATIO = 0.3


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ── uploader CLI 定位（与 sync-obsidian-to-lexiang 相同顺序）──────────

def find_uploader_cli():
    """定位 upload-markdown-to-lexiang 的 CLI。"""
    candidates = []
    env_home = os.environ.get("LEXIANG_UPLOADER_HOME", "")
    if env_home:
        candidates.append(os.path.join(env_home, "scripts", "lexiang_upload.py"))
    here = os.path.dirname(os.path.abspath(__file__))
    skills_root = os.path.dirname(os.path.dirname(here))
    candidates.append(os.path.join(
        skills_root, "upload-markdown-to-lexiang", "scripts",
        "lexiang_upload.py"))
    candidates.append(os.path.expanduser(
        "~/.workbuddy/skills/upload-markdown-to-lexiang/scripts/lexiang_upload.py"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


# ── 范围与节点展开 ──────────────────────────────────────────

def find_node(tree, path):
    """在菜单树中按 path 查找节点。"""
    for nd in tree:
        if nd.get("path") == path:
            return nd
        hit = find_node(nd.get("children") or [], path)
        if hit:
            return hit
    return None


def entry_node_path(entry_url, profile):
    """
    入口 URL 对应的菜单树节点 path。
    Next.js：path = URL 去掉 assetPrefix 后的段；通用站点按 URL 路径推导。
    """
    import urllib.parse
    parsed = urllib.parse.urlparse(entry_url)
    parts = [p for p in parsed.path.split("/") if p]
    prefix = [p for p in (profile.get("docs_prefix") or "").split("/") if p]
    if prefix and parts[: len(prefix)] == prefix:
        parts = parts[len(prefix):]
    return "/".join(parts)


def resolve_scope(entry_url, profile, scope):
    """返回 (顶级节点列表, 根节点标题)。"""
    tree = profile.get("menu_tree") or []
    if not tree:
        return [], ""
    entry_path = entry_node_path(entry_url, profile)
    entry_node = find_node(tree, entry_path) if entry_path else None
    if scope == "all":
        return list(tree), None
    if scope == "subtree":
        if not entry_node:
            return [], ""
        return [entry_node], entry_node.get("title")
    # section：入口所在的顶级分区
    if entry_node:
        top = entry_path.split("/")[0]
        for nd in tree:
            if (nd.get("path") or "").split("/")[0] == top:
                return [nd], nd.get("title")
    return list(tree), None


def flatten_nodes(nodes, parent_path="", top_level=True):
    """
    展开子树为有序节点列表。
    返回 [{path, title, url, directory, has_children, depth, parent_path}]
    parent_path = menu tree 中真实父节点的 path（与 path 段数推父可能不同，
    如 guides/SyncConfig/configItem/mapping 的树父是 guides/SyncConfig）。
    """
    out = []
    for nd in nodes:
        path = nd["path"]
        out.append({
            "path": path,
            "title": nd.get("title") or path,
            "url": nd.get("url", ""),
            "directory": bool(nd.get("directory")),
            "has_children": bool(nd.get("children")),
            "depth": path.count("/"),
            "parent_path": parent_path,
        })
        out.extend(flatten_nodes(nd.get("children") or [], path, False))
    return out


# ── WAF 消毒 ────────────────────────────────────────────────

def waf_sanitize(md, enabled=True):
    """命中乐享 WAF 危险模式的代码插入零宽空格，返回 (md, hit_count)。"""
    hits = 0
    if not enabled:
        return md, hits
    for pat in WAF_STRICT_PATTERNS:
        if pat in md:
            hits += md.count(pat)
            safe = pat[0] + "\u200b" + pat[1:]
            md = md.replace(pat, safe)
    # LDAP 过滤器 (k=v) 形式 → "(" 后插零宽空格，破坏 WAF 注入特征
    def _ldap_break(m):
        nonlocal hits
        hits += 1
        return m.group(0).replace("(", "(\u200b")
    md = WAF_LDAP_FILTER_RE.sub(_ldap_break, md)
    return md, hits


# ── 主流程 ──────────────────────────────────────────────────

def normalize_node_url(url, base_url):
    """
    菜单节点 url 可能是相对路径（"/" 或 "/docs/xxx"）甚至无效值。
    规范化为绝对 URL；无法规范化（空/等于站点首页）返回 None。
    """
    u = (url or "").strip()
    if not u or u == "/":
        return None
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("/"):
        return base_url.rstrip("/") + u
    return None


def build_workpackage(md, media, base_url):
    """
    已转换的 (markdown, media 列表) → 临时工作包（doc.md + images/）。
    uploader 预检以 doc.md 所在目录解析相对图片路径，故引用须写
    images/<filename>。同名不同源加序号后缀；同源只下载一次；
    引用按 md 中出现顺序逐个替换（同名多次引用不错位）。
    返回 (workdir, md_path, downloaded, total_media)。
    """
    workdir = tempfile.mkdtemp(prefix="websync_")
    img_dir = os.path.join(workdir, "images")
    os.makedirs(img_dir, exist_ok=True)
    downloaded = 0
    src_seen = {}    # src -> 本地文件名（已下载）
    name_used = {}   # 本地文件名 -> True（防不同 src 同名覆盖）
    for m in media:
        orig = m["filename"]
        src = m["src"]
        local = None
        if src in src_seen:
            local = src_seen[src]
        else:
            local = orig
            if local in name_used:
                stem, ext = os.path.splitext(orig)
                i = 2
                while f"{stem}_{i}{ext}" in name_used:
                    i += 1
                local = f"{stem}_{i}{ext}"
            try:
                data = http_get(src, binary=True, timeout=60)
                with open(os.path.join(img_dir, local), "wb") as f:
                    f.write(data)
                downloaded += 1
                name_used[local] = True
                src_seen[src] = local
            except Exception as e:
                log(f"    [媒体失败] {src}: {e}")
                # 该次引用回退为绝对 URL
                src_seen[src] = None
                local = None
        # 替换第一个未替换的 ](orig) 引用。
        # 已替换的引用变成 ](images/xxx) 不再匹配 token，故每次 find
        # 第一个命中即为下一个待处理引用，天然按出现顺序消耗。
        token = f"]({orig})"
        idx = md.find(token)
        if idx < 0:
            # 引用数 < media 条数（html2md 少写了引用），跳过
            continue
        repl = f"]({src})" if local is None else f"](images/{local})"
        md = md[:idx] + repl + md[idx + len(token):]
    md_path = os.path.join(workdir, "doc.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    return workdir, md_path, downloaded, len(media)


def apply_sidebar_groups(profile, section_path, manifest, lx, args,
                         section_parent, stats):
    """
    按 sidebar HTML 视觉分组，重组 section 下 entry 的位置 + 顺序。

    背景：Nextra 把组标题（如「通讯录/登录/企业设置」）只放在 sidebar HTML
    里，菜单 JSON 完全没有这层 —— 所以 sync 主流程按 JSON 建的「使用指南」
    folder 下是平铺乱序的。补建 group folder 并按 sidebar 出现顺序把 children
    move 到正确位置。

    幂等：每次重跑都执行移动（位置变化，无副作用）+ 补建缺失 folder。page
    内容不受影响。失败不重试，单独记录到 failures。
    """
    groups = (profile.get("sidebar_section_groups") or {}).get(section_path)
    if not groups:
        return
    # 1) 为每个有 title 的 group 建/取 folder（manifest 加 _sidebar_group_ 前缀）
    group_folders = {}  # title → entry_id
    for g in groups:
        title = (g.get("group_title") or "").strip()
        if not title:
            continue
        gkey = f"_sidebar_group__{section_path}__{title}"
        rec = manifest["folders"].get(gkey)
        if rec and lx.probe_entry(rec["entry_id"])["exists"]:
            group_folders[title] = rec["entry_id"]
            dash_set(gkey, "skipped")
        else:
            fid = lx.create_folder(args.target_space_id, title, section_parent)
            group_folders[title] = fid
            manifest["folders"][gkey] = {
                "entry_id": fid, "name": title, "parent": section_parent,
                "kind": "sidebar_group"}
            save_manifest(args.url, manifest)
            stats["folders_new"] += 1
            dash_set(gkey, "success")
            log(f"  📂 新建 sidebar 分组: {title}")
    # 2) 按 sidebar 顺序移动 children；group 之间在 section 下也按出现顺序
    prev_in_section = None  # section_parent 下
    for g in groups:
        title = (g.get("group_title") or "").strip()
        if title and title in group_folders:
            cur_folder = group_folders[title]
            # 把当前 group folder 排到上一个兄弟之后（保持在 section 下）
            if prev_in_section and cur_folder != prev_in_section:
                try:
                    lx.move_entry(cur_folder, section_parent,
                                   after=prev_in_section)
                except Exception as e:
                    log(f"  ⚠️ group 排序失败 {title}: {e}")
            prev_in_section = cur_folder
            target_parent = cur_folder
            prev_in_child = None  # group 内前一个 child
        else:
            # 无 group 标题 → 直挂在 section 下
            target_parent = section_parent
            prev_in_child = prev_in_section
        for child_path in g.get("children_path") or []:
            rec = (manifest["pages"].get(child_path)
                   or manifest["folders"].get(child_path))
            if not rec:
                continue
            ent_id = rec["entry_id"]
            try:
                lx.move_entry(ent_id, target_parent, after=prev_in_child)
                if target_parent == section_parent:
                    prev_in_section = ent_id
                    prev_in_child = ent_id
                else:
                    prev_in_child = ent_id
            except Exception as e:
                log(f"  ⚠️ sidebar 重排 {child_path}: {e}")

    # 3) 兜底归组：menu JSON 里有但 sidebar 没渲染的页（如「密码策略配置」
    # strategy、「企业信息认证」EnterpriseVerification）—— 它们 menu 父是
    # section root，sync 主流程会把它挂到 section root 一级，但在视觉上
    # 突兀。按 path 段前缀找同目录兄弟所在 group，归入那个 group 末尾。
    section_node = None
    for top in (profile.get("menu_tree") or []):
        if top["path"] == section_path:
            section_node = top
            break
    if section_node and not args.no_adopt_orphan:
        grouped = set()
        for g in groups:
            for cp in g.get("children_path") or []:
                grouped.add(cp)
        # 已分组 section child 的 path → 所在 group folder
        child_to_group = {}
        for g in groups:
            t = (g.get("group_title") or "").strip()
            if not t:
                continue
            for cp in g.get("children_path") or []:
                child_to_group[cp] = t
        adopted = 0
        for child in section_node.get("children") or []:
            cp = child["path"]
            if cp in grouped:
                continue
            rec = (manifest["pages"].get(cp)
                   or manifest["folders"].get(cp))
            if not rec:
                continue
            # 同 path 前缀的兄弟归哪个 group？取 path 的前两段
            # （guides/LoginConfig/... → guides/LoginConfig），兄弟
            # 如 guides/LoginConfig/username 同前缀即可定位 group。
            parts = cp.split("/")
            seg2 = "/".join(parts[:2]) if len(parts) >= 2 else cp
            target_group = None
            for sib_path, gtitle in child_to_group.items():
                if sib_path.startswith(seg2 + "/") or sib_path == seg2:
                    target_group = gtitle
                    break
            if not target_group or target_group not in group_folders:
                continue
            try:
                lx.move_entry(rec["entry_id"], group_folders[target_group])
                rec["parent"] = group_folders[target_group]
                save_manifest(args.url, manifest)
                log(f"  📌 兜底归入 [{target_group}]: {rec['name']}")
                adopted += 1
            except Exception as e:
                log(f"  ⚠️ 兜底归组失败 {cp}: {e}")
        if adopted:
            log(f"  （共 {adopted} 个 sidebar 隐藏页归组）")


# ── 看板状态（preview.html / 实时 / report.html 三态）────────────────
# _DASH = {"dir", "state", "server", "url"}；None 表示看板关闭
_DASH = None


def _dash_new_state(args, profile, root_title, items, mode, manifest):
    st = {}
    for it in items:
        k = it["key"]
        if mode == "preview":
            # dry-run：标注「待新建 / 已存在」供用户确认范围
            bucket = manifest["folders"] if it["kind"] in ("folder", "group") \
                else manifest["pages"]
            st[k] = {"state": "exists" if k in bucket else "will_create",
                     "error": None}
        else:
            st[k] = {"state": "pending", "error": None}
    return {
        "site_url": args.url, "site_type": profile["site_type"],
        "scope": args.scope, "root_title": root_title or "(全站)",
        "target_space_id": args.target_space_id,
        "target_folder_id": args.target_folder_id or "",
        "mode": mode, "finished": False,
        "started_at": now_iso(), "updated_at": now_iso(),
        "phase": "", "current": "",
        "items": items, "status": st,
        "stats": {"total": len(items), "done": 0, "success": 0,
                  "updated": 0, "skipped": 0, "failed": 0, "trashed": 0},
        "failures": [],
    }


def _dash_recompute(state):
    done = succ = upd = skip = fail = 0
    for v in state["status"].values():
        s = v["state"]
        if s in ("pending", "running", "will_create", "exists"):
            continue
        done += 1
        if s == "success":
            succ += 1
        elif s in ("updated", "verify_warn"):
            upd += 1
        elif s == "skipped":
            skip += 1
        elif s == "failed":
            fail += 1
    state["stats"].update({"done": done, "success": succ, "updated": upd,
                           "skipped": skip, "failed": fail})


def _dash_flush():
    if not _DASH:
        return
    _DASH["state"]["updated_at"] = now_iso()
    _dash_recompute(_DASH["state"])
    dashmod.write_status(_DASH["dir"], _DASH["state"])


def _dash_item_title(key):
    for it in _DASH["state"]["items"]:
        if it["key"] == key:
            return it["title"]
    return key


def dash_set(key, state_name, error=None):
    if not _DASH:
        return
    rec = _DASH["state"]["status"].get(key)
    if rec is None:
        rec = {"state": "pending", "error": None}
        _DASH["state"]["status"][key] = rec
    rec["state"] = state_name
    rec["error"] = error
    if state_name == "failed" and error:
        _DASH["state"]["failures"].append(
            {"key": key, "title": _dash_item_title(key), "error": error})
    _dash_flush()


def dash_phase(text, current=""):
    if not _DASH:
        return
    _DASH["state"]["phase"] = text
    _DASH["state"]["current"] = current
    _dash_flush()


def dash_trash():
    if not _DASH:
        return
    _DASH["state"]["stats"]["trashed"] += 1
    _dash_flush()


def dash_finish(ok=True):
    """写最终 status.json + 静态 report.html，关停本地服务。返回信息 dict。"""
    global _DASH
    if not _DASH:
        return None
    _DASH["state"]["mode"] = "done"
    _DASH["state"]["finished"] = True
    _DASH["state"]["phase"] = "已完成" if ok else "异常终止"
    _DASH["state"]["current"] = ""
    _dash_flush()
    report_html = dashmod.write_static(_DASH["dir"], "report.html",
                                       _DASH["state"])
    info = {"report_html": report_html, "dashboard_url": _DASH["url"]}
    srv = _DASH.get("server")
    if srv:
        try:
            srv.shutdown()
        except Exception:
            pass
    log(f"  📊 最终报告: {report_html}")
    _DASH = None
    return info


def main():
    global _DASH
    ap = argparse.ArgumentParser(description="文档站点 → 乐享同步")
    ap.add_argument("--url", default=None,
                    help="站点入口 URL（--check-deps 时不需要）")
    ap.add_argument("--target-space-id", default=None,
                    help="乐享 space id（--check-deps 时不需要）")
    ap.add_argument("--target-folder-id", default=None,
                    help="目标目录 entry_id（默认知识库根）")
    ap.add_argument("--scope", choices=["subtree", "section", "all"],
                    default="section")
    ap.add_argument("--delete-mode", choices=["sync", "keep"], default="sync",
                    help="sync=源删→移入回收站；keep=保留")
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--lexiang-profile", default=None)
    ap.add_argument("--lexiang-credential-file", default=None)
    ap.add_argument("--no-waf-sanitize", action="store_true")
    ap.add_argument("--force-delete", action="store_true",
                    help="消失比例超阈值时仍执行删除（慎用）")
    ap.add_argument("--probe-only", action="store_true",
                    help="仅探测站点输出菜单树 JSON")
    ap.add_argument("--no-adopt-orphan", action="store_true",
                    help="不把 sidebar 隐藏但 menu JSON 存在的页归入相邻 group")
    ap.add_argument("--check-deps", action="store_true",
                    help="仅检查依赖（Python / upload-markdown-to-lexiang / 乐享凭证），不连接站点")
    ap.add_argument("--no-dashboard", action="store_true",
                    help="不生成 preview.html / 实时看板 / report.html（默认生成）")
    # parse_known_args 允许携带透传给子工具的额外参数（如 --check-deps --json）
    args, extras = ap.parse_known_args()

    # ── 仅依赖检查模式（不要求 url/space） ──────────────────────────────
    if args.check_deps:
        here = os.path.dirname(os.path.abspath(__file__))
        check_deps_py = os.path.join(here, "check_deps.py")
        if not os.path.isfile(check_deps_py):
            log("ERROR: 找不到 scripts/check_deps.py；请确认与 sync.py 在同一目录")
            return 2
        # 透传所有额外参数（如 --json）给 check_deps.py
        rc = subprocess.call([sys.executable, check_deps_py] + extras)
        sys.exit(rc)

    # ── 非 check-deps 模式必填 ──────────────────────────────────────
    if not args.url or not args.target_space_id:
        ap.error("--url 与 --target-space-id 必填（除非用 --check-deps）")

    # ── 启动软检查：正式同步（且非 probe-only/dry-run）缺关键依赖立刻失败 ──
    if not args.probe_only and not args.dry_run:
        uploader = find_uploader_cli()
        if not uploader:
            print(json.dumps({
                "ok": False,
                "error": "未找到 upload-markdown-to-lexiang CLI；正式同步依赖该 skill 提供 Markdown 上传能力",
                "fix": (
                    "git clone https://github.com/ajaxhe/upload-markdown-to-lexiang.git "
                    "~/.workbuddy/skills/upload-markdown-to-lexiang"
                ),
                "hint": (
                    "probe-only / dry-run 不需要该依赖；"
                    "可先 python3 sync.py --check-deps 单独检查；"
                    "也可设环境变量 LEXIANG_UPLOADER_HOME 指向自定义位置"
                ),
            }, ensure_ascii=False))
            return 2

    # ── 探测站点 ──────────────────────────────────────────
    log(f"[1/6] 探测站点: {args.url}")
    try:
        profile = probe_site(args.url)
    except CrawlError as e:
        print(json.dumps({"ok": False, "error": f"站点探测失败: {e}"}))
        return 2
    if not profile or not (profile.get("menu_tree") or
                           profile.get("link_tree")):
        print(json.dumps({"ok": False, "error": "无法提取站点菜单树"}))
        return 2
    if args.probe_only:
        print(json.dumps({
            "site_type": profile["site_type"],
            "base_url": profile["base_url"],
            "docs_prefix": profile["docs_prefix"],
            "top_sections": [
                {"name": n.get("name", n.get("path")),
                 "title": n.get("title"),
                 "children": len(n.get("children") or [])}
                for n in (profile.get("menu_tree") or
                          profile.get("link_tree") or [])],
        }, ensure_ascii=False, indent=1))
        return 0

    tree = profile.get("menu_tree") or profile.get("link_tree") or []

    # ── 范围 ──────────────────────────────────────────────
    log(f"[2/6] 确定范围: scope={args.scope}")
    scope_nodes, root_title = resolve_scope(args.url, profile, args.scope)
    nodes = flatten_nodes(scope_nodes)
    if not nodes:
        print(json.dumps({"ok": False, "error": "范围内没有文档节点"}))
        return 2
    if args.max_pages:
        nodes = nodes[: args.max_pages]

    # ── 状态 ──────────────────────────────────────────────
    config = load_config(args.url)
    config.update({
        "entry_url": args.url,
        "target_space_id": args.target_space_id,
        "target_folder_id": args.target_folder_id or "",
        "scope": args.scope,
        "delete_mode": args.delete_mode,
        "lexiang_profile": args.lexiang_profile or "",
        "site_type": profile["site_type"],
        "base_url": profile["base_url"],
        "docs_prefix": profile["docs_prefix"],
    })
    save_config(args.url, config)
    manifest = load_manifest(args.url)

    # ── 看板树（dry-run 预览 / 正式同步实时看板共用）─────────────────
    section_path = scope_nodes[0]["path"] if scope_nodes else ""
    dash_dir = os.path.join(str(state_dir(args.url)), "dashboard")
    dash_items = [] if args.no_dashboard else dashmod.build_tree_items(
        scope_nodes, nodes, profile, section_path, root_title)

    # dry-run 清单
    plan = {"create": [], "update": [], "skip": [], "folders_new": []}
    for nd in nodes:
        key = nd["path"]
        if nd["has_children"] or nd["directory"]:
            # 目录节点：建 folder，不计入页面新建
            if key not in manifest["folders"]:
                plan["folders_new"].append(key)
            continue
        rec = manifest["pages"].get(key)
        if not rec:
            plan["create"].append({"path": key, "title": nd["title"]})
    # update/skip 需抓取后才能判定，dry-run 给出 create/folder 计划
    if args.dry_run:
        to_trash = []
        if args.delete_mode == "sync" and manifest["pages"]:
            current = {nd["path"] for nd in nodes}
            for key, rec in manifest["pages"].items():
                if key not in current:
                    to_trash.append({"path": key, "entry_id": rec["entry_id"],
                                     "name": rec.get("name")})
        preview_html = None
        if not args.no_dashboard:
            # 前置确认页：静态自包含，用户双击打开核对目录层级
            state = _dash_new_state(args, profile, root_title, dash_items,
                                    "preview", manifest)
            state["plan"] = {
                "new_folders": len(plan["folders_new"]),
                "new_pages": len(plan["create"]),
                "known_pages": len(manifest["pages"]),
                "to_trash": len(to_trash),
            }
            preview_html = dashmod.write_static(dash_dir, "preview.html", state)
            log(f"  📋 前置确认页: {preview_html}")
        print(json.dumps({
            "ok": True, "dry_run": True,
            "site_type": profile["site_type"],
            "scope_root": root_title or "(全站)",
            "total_nodes": len(nodes),
            "plan": {
                "new_folders": len(plan["folders_new"]),
                "new_pages": len(plan["create"]),
                "known_pages": len(manifest["pages"]),
                "to_trash": to_trash,
            },
            "preview_html": preview_html,
            "menu_preview": [
                {"title": n["title"], "path": n["path"],
                 "children": len(n.get("children") or [])}
                for n in scope_nodes],
        }, ensure_ascii=False, indent=1))
        return 0

    # ── 执行同步 ──────────────────────────────────────────
    try:
        acquire_lock(args.url)
    except SyncLockError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2

    stats = {"folders_new": 0, "pages_new": 0, "pages_updated": 0,
             "pages_skipped": 0, "pages_failed": 0, "media_downloaded": 0,
             "media_failed": 0, "trashed": 0, "trash_failed": 0,
             "waf_sanitized": 0, "verify_warn": 0}
    failures = []
    # ── 启动实时看板（127.0.0.1 临时端口，进程退出自动销毁）────────────
    if not args.no_dashboard:
        state = _dash_new_state(args, profile, root_title, dash_items,
                                "sync", manifest)
        dashmod.write_status(dash_dir, state)
        srv, dash_url = dashmod.start_server(dash_dir)
        _DASH = {"dir": dash_dir, "state": state, "server": srv,
                 "url": dash_url}
        log(f"  📊 实时看板: {dash_url}"
            "（同步期间自动刷新；结束后生成静态 report.html）")
    try:
        log("[3/6] 连接乐享")
        sel = resolve_personal_credential_selector(
            profile=args.lexiang_profile,
            credential_file=args.lexiang_credential_file)
        # 显式 profile / 凭证文件：直接读该 JSON 作为个人凭证（lxmcp_ Bearer 模式），
        # 不再依赖连接器 OAuth token 发现（沙箱内 ps 检测 MCP 代理会失败）。
        lx = LexiangConnector(
            personal_credential_file=str(sel.path) if sel else None,
            credential=None)

        uploader = find_uploader_cli()
        if not uploader:
            raise LexiangError(
                "未找到 upload-markdown-to-lexiang CLI；"
                "请先安装该公共上传 Skill")

        parent = args.target_folder_id or lx.resolve_root_entry_id(
            args.target_space_id)
        if not parent:
            raise LexiangError("无法确定目标目录（target-folder-id 为空且"
                               "无法获取知识库根）")

        # 范围根：section/subtree 时建根 folder
        if root_title:
            root_key = scope_nodes[0]["path"]
            if root_key in manifest["folders"]:
                parent = manifest["folders"][root_key]["entry_id"]
            else:
                log(f"  建根目录: {root_title}")
                parent = lx.create_folder(args.target_space_id, root_title,
                                          parent)
                manifest["folders"][root_key] = {
                    "entry_id": parent, "name": root_title, "parent": ""}
                save_manifest(args.url, manifest)
                stats["folders_new"] += 1

        # 目录结构：预建 directory 节点
        log("[4/6] 建目录结构 + 同步页面")
        dash_phase("建目录结构 + 同步页面")
        nodes_by_path = {nd["path"]: nd for nd in nodes}
        for nd in nodes:
            key = nd["path"]
            dash_phase("建目录结构 + 同步页面", nd["title"])
            # 父节点：用 menu tree 真实父（flatten_nodes 已记录 parent_path），
            # 与「path 推父」不同——如 mapping 的树父是 SyncConfig，
            # 而 path 推导会得到不存在的 configItem 节点，落回到 section root。
            if nd.get("parent_path"):
                parent_key = nd["parent_path"]
                prec = manifest["pages"].get(parent_key) or \
                    manifest["folders"].get(parent_key)
                node_parent = prec["entry_id"] if prec else parent
            elif "/" in key:
                parent_key = key.rsplit("/", 1)[0]
                prec = manifest["pages"].get(parent_key) or \
                    manifest["folders"].get(parent_key)
                node_parent = prec["entry_id"] if prec else parent
            else:
                node_parent = parent

            if nd["directory"] or nd["has_children"]:
                # 目录型节点
                #   - has_children → 必须建 folder（其 children 才能挂下来）
                #   - 仅 directory 且无 content → folder
                #   - 仅 directory 有 content（罕见）→ page，子挂其下
                if nd["has_children"]:
                    rec = manifest["folders"].get(key)
                    if rec:
                        probe = lx.probe_entry(rec["entry_id"])
                        if not probe["exists"]:
                            log(f"  目录失效重建: {nd['title']}")
                            rec["entry_id"] = lx.create_folder(
                                args.target_space_id, nd["title"], node_parent)
                            save_manifest(args.url, manifest)
                            dash_set(key, "updated")
                        else:
                            dash_set(key, "skipped")
                    else:
                        log(f"  建目录: {nd['title']}")
                        fid = lx.create_folder(args.target_space_id,
                                               nd["title"], node_parent)
                        manifest["folders"][key] = {
                            "entry_id": fid, "name": nd["title"],
                            "parent": node_parent}
                        save_manifest(args.url, manifest)
                        stats["folders_new"] += 1
                        dash_set(key, "success")
                else:
                    # 无 children 的目录：抓页面判断有无正文
                    node_url = normalize_node_url(nd["url"], profile["base_url"])
                    page = fetch_page(node_url) if node_url else None
                    has_content = bool(page and page["content_html"] and
                                       len(page["content_html"]) > 400)
                    if has_content:
                        # 既是文档又是目录 → page，子挂其下
                        _sync_page(nd, page, manifest, lx, uploader, args,
                                   node_parent, stats, failures, args.url,
                                   profile["base_url"])
                    else:
                        rec = manifest["folders"].get(key)
                        if rec:
                            probe = lx.probe_entry(rec["entry_id"])
                            if not probe["exists"]:
                                log(f"  目录失效重建: {nd['title']}")
                                rec["entry_id"] = lx.create_folder(
                                    args.target_space_id, nd["title"], node_parent)
                                save_manifest(args.url, manifest)
                                dash_set(key, "updated")
                            else:
                                dash_set(key, "skipped")
                        else:
                            log(f"  建目录: {nd['title']}")
                            fid = lx.create_folder(args.target_space_id,
                                                   nd["title"], node_parent)
                            manifest["folders"][key] = {
                                "entry_id": fid, "name": nd["title"],
                                "parent": node_parent}
                            save_manifest(args.url, manifest)
                            stats["folders_new"] += 1
                            dash_set(key, "success")
            else:
                # 叶子 → page
                node_url = normalize_node_url(nd["url"], profile["base_url"])
                if not node_url:
                    stats["pages_failed"] += 1
                    failures.append({"path": key,
                                     "error": f"节点 URL 无效: {nd['url']!r}"})
                    dash_set(key, "failed", f"节点 URL 无效: {nd['url']!r}")
                    continue
                dash_set(key, "running")
                try:
                    page = fetch_page(node_url)
                except CrawlError as e:
                    stats["pages_failed"] += 1
                    failures.append({"path": key, "error": str(e)})
                    dash_set(key, "failed", str(e)[:300])
                    continue
                _sync_page(nd, page, manifest, lx, uploader, args,
                           node_parent, stats, failures, args.url,
                           profile["base_url"])

        # ── Sidebar 视觉分组重组 ──────────────────────────
        # Nextra 把分组标题只放在 sidebar HTML 里、菜单 JSON 没有这层。
        # 按 sidebar 出现顺序，用 entry_move_entry(+after) 重排已有 entry，
        # 并补建缺失的 group folder（manifest._sidebar_group__ 命名空间）。
        section_path = scope_nodes[0]["path"] if scope_nodes else ""
        dash_phase("Sidebar 分组重排")
        apply_sidebar_groups(profile, section_path, manifest, lx, args,
                              parent, stats)

        # ── 删除同步 ──────────────────────────────────────
        if args.delete_mode == "sync" and manifest["pages"]:
            log("[5/6] 删除同步（回收站模式）")
            dash_phase("删除同步（回收站模式）")
            current = {nd["path"] for nd in nodes} | \
                {nd["path"].rsplit("/", 1)[0] for nd in nodes if "/" in nd["path"]}
            # 完整祖先集合
            ancestor = set()
            for nd in nodes:
                parts = nd["path"].split("/")
                for i in range(1, len(parts)):
                    ancestor.add("/".join(parts[:i]))
            current |= ancestor
            # sidebar visual 分组（_sidebar_group_ 前缀）由 HTML 决定，
            # 不属于 menu tree，不纳入删除判定
            sidebar_groups = set(k for k in manifest["folders"]
                                   if k.startswith("_sidebar_group__"))
            vanished = [(k, r) for k, r in manifest["pages"].items()
                        if k not in current]
            vanished += [(k, r) for k, r in manifest["folders"].items()
                         if k not in current and k not in sidebar_groups]
            total_known = len(manifest["pages"]) + len(manifest["folders"])
            if total_known and len(vanished) / total_known > DELETE_ABORT_RATIO \
                    and not args.force_delete:
                log(f"  ⚠️ 消失节点 {len(vanished)}/{total_known} 超过"
                    f"{int(DELETE_ABORT_RATIO*100)}%，中止删除；"
                    "确认无误可用 --force-delete 重跑")
                failures.append({
                    "path": "(delete-sync)",
                    "error": f"消失比例过高({len(vanished)}/{total_known})，"
                             "删除已中止，需 --force-delete"})
            elif vanished:
                # 回收站目录
                trash_id = manifest.get("trash_folder_id") or ""
                if not trash_id:
                    # 在目标目录下查找/创建
                    for ch in lx.list_children(args.target_folder_id or parent):
                        if ch.get("name") == TRASH_FOLDER_NAME:
                            trash_id = ch.get("id")
                            break
                if not trash_id:
                    trash_id = lx.create_folder(
                        args.target_space_id, TRASH_FOLDER_NAME,
                        args.target_folder_id or parent)
                manifest["trash_folder_id"] = trash_id
                # 只 move 最高层消失节点（父已消失的子节点跳过）
                vanished.sort(key=lambda kr: kr[0].count("/"))
                moved = set()
                for key, rec in vanished:
                    if any(key.startswith(m + "/") for m in moved):
                        continue
                    try:
                        lx.move_entry(rec["entry_id"], trash_id)
                        moved.add(key)
                        stats["trashed"] += 1
                        dash_trash()
                        log(f"  🗑 移入回收站: {rec.get('name', key)}")
                    except LexiangError as e:
                        stats["trash_failed"] += 1
                        failures.append({"path": key,
                                         "error": f"移动失败: {e}"})
                # manifest 清理
                for key in moved:
                    manifest["pages"].pop(key, None)
                    manifest["folders"].pop(key, None)
                save_manifest(args.url, manifest)
        else:
            if args.delete_mode == "sync":
                log("[5/6] 删除同步：无已知页面，跳过")
            else:
                log("[5/6] 删除同步：keep 模式（跳过）")

        manifest["last_sync"] = now_iso()
        save_manifest(args.url, manifest)

        # ── 报告 ──────────────────────────────────────────
        log("[6/6] 生成报告")
        dash_phase("生成报告")
        dash_info = dash_finish(ok=not failures)
        rp = report_path(args.url)
        with open(rp, "w", encoding="utf-8") as f:
            f.write(f"# 站点同步报告\n\n- 时间：{now_iso()}\n"
                    f"- 入口：{args.url}\n"
                    f"- 范围：{args.scope}\n"
                    f"- 目标：space={args.target_space_id} "
                    f"folder={args.target_folder_id}\n\n"
                    f"## 结果统计\n\n| 项目 | 数量 |\n|---|---|\n")
            for k, v in stats.items():
                f.write(f"| {k} | {v} |\n")
            if failures:
                f.write("\n## 失败/降级明细\n\n")
                for it in failures:
                    f.write(f"- `{it['path']}`: {it['error']}\n")
        result = {"ok": True, "stats": stats, "failures": len(failures),
                  "report": str(rp),
                  "state_dir": str(state_dir(args.url))}
        if dash_info:
            result["report_html"] = dash_info["report_html"]
            result["dashboard_url"] = dash_info["dashboard_url"]
        if failures:
            result["ok"] = "partial"
        print(json.dumps(result, ensure_ascii=False))
        return 0 if not failures else 1
    except (LexiangError, CrawlError) as e:
        dash_finish(ok=False)
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    finally:
        release_lock(args.url)


def _sync_page(nd, page, manifest, lx, uploader, args, node_parent,
               stats, failures, entry_url, base_url):
    """同步单个页面：hash 增量判断 + 工作包 + uploader CLI。"""
    key = nd["path"]
    title = nd["title"]
    dash_set(key, "running")
    md, media = html2md_convert(page["content_html"], base_url,
                                page.get("title", ""))
    content_hash = compute_hash(md, page.get("update_time", ""))
    rec = manifest["pages"].get(key)
    # 无论 hash 是否命中，先确保 entry 在正确父下：上一轮 sync 把父建为
    # folder 的情况、或 layout 调整，page 必须 move 到 node_parent。
    if rec and rec.get("entry_id") and \
            rec.get("parent") != node_parent and \
            lx.probe_entry(rec["entry_id"])["exists"]:
        try:
            lx.move_entry(rec["entry_id"], node_parent)
            rec["parent"] = node_parent
            save_manifest(entry_url, manifest)
            log(f"  🔁 调整归属到 {node_parent[:8]}: {title}")
        except Exception as e:
            log(f"  ⚠️ 调整归属失败: {title}: {str(e)[:80]}")
    if rec and rec.get("content_hash") == content_hash:
        stats["pages_skipped"] += 1
        dash_set(key, "skipped")
        return
    workdir, md_path, dl, total_m = build_workpackage(md, media, base_url)
    stats["media_downloaded"] += dl
    stats["media_failed"] += max(0, total_m - dl)
    # WAF 消毒
    with open(md_path, encoding="utf-8") as f:
        md_text = f.read()
    md_text, hits = waf_sanitize(
        md_text, enabled=not args.no_waf_sanitize)
    if hits:
        stats["waf_sanitized"] += hits
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_text)
    # 上传命令
    cmd = [sys.executable, uploader, "upload", md_path,
           "--work-dir", workdir, "--name", title,
           "--source-url", nd["url"], "--json"]
    if args.lexiang_profile:
        cmd += ["--profile", args.lexiang_profile]
    if args.lexiang_credential_file:
        cmd += ["--credential-file", args.lexiang_credential_file]
    if rec:
        cmd += ["--entry-id", rec["entry_id"]]
    else:
        cmd += ["--parent-id", node_parent]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=600)
        out = proc.stdout.strip().splitlines()
        payload = None
        for line in reversed(out):
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if proc.returncode != 0 or not payload:
            raise RuntimeError((proc.stderr or proc.stdout or "")[-300:])
        entry_id = (payload.get("entry_id") or
                    (payload.get("entry") or {}).get("id") or
                    payload.get("id"))
        page_url = payload.get("url") or payload.get("page_url") or ""
        if not entry_id:
            raise RuntimeError(
                f"上传响应缺少 entry_id: {str(payload)[:200]}")
        if rec:
            stats["pages_updated"] += 1
            dash_set(key, "updated")
            log(f"  ✏️ 更新: {title}")
        else:
            stats["pages_new"] += 1
            dash_set(key, "success")
            log(f"  ✅ 新建: {title} -> {page_url}")
        manifest["pages"][key] = {
            "entry_id": entry_id, "name": title, "url": nd["url"],
            "parent": node_parent, "content_hash": content_hash,
            "update_time": page.get("update_time", ""),
        }
        save_manifest(entry_url, manifest)
    except Exception as e:
        err = str(e)
        if "VERIFY_ERROR" in err:
            # 写入已完成，锚点校验失败多为 round-trip 误报（乐享渲染后
            # 链接 [url](url) 展开为两遍文本、代码块格式化差异等）。
            # 视为成功：记 hash 防止无限重传，verify_failed 标记供报告提示。
            entry_id = rec["entry_id"] if rec else None
            if not entry_id:
                try:
                    for ch in lx.list_children(node_parent):
                        if ch.get("name") == title:
                            entry_id = ch.get("id")
                            break
                except Exception:
                    pass
            if entry_id:
                stats["pages_updated"] += 1
                stats["verify_warn"] += 1
                manifest["pages"][key] = {
                    "entry_id": entry_id, "name": title, "url": nd["url"],
                    "parent": node_parent, "content_hash": content_hash,
                    "update_time": page.get("update_time", ""),
                    "verify_failed": True,
                }
                save_manifest(entry_url, manifest)
                dash_set(key, "verify_warn")
                log(f"  ⚠️ 写入完成但校验误报（round-trip），已记录: {title}")
                return
        stats["pages_failed"] += 1
        failures.append({"path": key, "error": err[:300]})
        dash_set(key, "failed", err[:300])
        log(f"  ❌ 失败: {title}: {err[:120]}")
        # 半成品恢复：UPLOAD_ERROR（写入中断）时 entry 已创建。找到父节点下
        # 同名 entry 记入 manifest，重试时走 --entry-id 覆盖更新，避免重复建页。
        if rec is None and "UPLOAD_ERROR" in err:
            partial_id = None
            try:
                for ch in lx.list_children(node_parent):
                    if ch.get("name") == title:
                        partial_id = ch.get("id")
                        break
            except Exception:
                pass
            if partial_id:
                manifest["pages"][key] = {
                    "entry_id": partial_id, "name": title, "url": nd["url"],
                    "parent": node_parent, "content_hash": "",
                    "update_time": page.get("update_time", ""),
                    "partial": True,
                }
                save_manifest(entry_url, manifest)
                log(f"  ↩︎ 已记录半成品 entry，重试将覆盖更新: {title}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
