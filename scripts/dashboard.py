#!/usr/bin/env python3
"""
dashboard.py - 同步预览 / 实时看板 / 最终报告（三态同一模板）

- preview.html：静态自包含（状态 JSON 内嵌），dry-run 后供用户确认目录层级
- 实时看板：sync 期间内置 127.0.0.1 HTTP 服务 + status.json 原子写，页面 1.5s 轮询
- report.html：静态自包含，同步完成后的最终留档（可 present_files / 转发）

全程 stdlib，无第三方依赖，文档内容不经过本模块。
"""
import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

TEMPLATE_NAME = "dashboard.html"
INJECT_MARK = "<!--__STATE_INJECT__-->"


def _template_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "references", TEMPLATE_NAME)


def load_template():
    with open(_template_path(), encoding="utf-8") as f:
        return f.read()


def render_static(state):
    """状态 JSON 内嵌进模板 → 可双击打开的自包含 HTML。"""
    payload = json.dumps(state, ensure_ascii=False).replace("</", "<\\/")
    inject = "<script>window.__SYNC_STATE__ = " + payload + ";</script>"
    return load_template().replace(INJECT_MARK, inject)


def write_static(out_dir, name, state):
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(render_static(state))
    return p


def write_status(out_dir, state):
    """原子写 status.json（先写临时文件再 rename，避免页面读到半截 JSON）。"""
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, ".status.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(out_dir, "status.json"))


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 静默访问日志
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.path = "/" + TEMPLATE_NAME
        super().do_GET()


def start_server(out_dir):
    """
    启动 127.0.0.1 临时端口静态服务（daemon 线程，进程退出自动销毁）。
    实时模式的模板不含内嵌状态 → 页面自动走 /status.json 轮询。
    返回 (server, url)。
    """
    with open(os.path.join(out_dir, TEMPLATE_NAME), "w", encoding="utf-8") as f:
        f.write(load_template().replace(INJECT_MARK, ""))
    handler = functools.partial(_Handler, directory=out_dir)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


def build_tree_items(scope_nodes, nodes, profile, section_path, root_title):
    """
    生成预览树（扁平 items，带 depth）。
    有 sidebar 分组时按「最终落地布局」展示（section → group → children），
    让用户确认的就是最终在乐享看到的样子；无分组时按菜单树原序。
    """
    by_path = {nd["path"]: nd for nd in nodes}
    root_path = scope_nodes[0]["path"] if scope_nodes else ""
    base_depth = nodes[0]["depth"] if nodes else 0

    def kind_of(nd):
        return "folder" if (nd["has_children"] or nd["directory"]) else "page"

    items = []
    emitted_depth = {}  # key → depth（parent_path 链计算真实视觉深度，
    #                     path 段差不可靠：mapping 的树父是 SyncConfig 而非 configItem）
    if root_title and root_path:
        items.append({"key": root_path, "title": root_title,
                      "kind": "folder", "depth": 0})
        emitted_depth[root_path] = 0

    def depth_of(nd, fallback_parent, fallback_depth):
        pp = nd.get("parent_path")
        if pp in emitted_depth:
            return emitted_depth[pp] + 1
        return fallback_depth + 1

    groups = (profile.get("sidebar_section_groups") or {}).get(section_path) or []
    if not groups:
        for nd in nodes:
            if nd["path"] == root_path:
                continue
            d = depth_of(nd, root_path, 0)
            items.append({"key": nd["path"], "title": nd["title"],
                          "kind": kind_of(nd), "depth": d})
            emitted_depth[nd["path"]] = d
        return items

    emitted = {root_path}
    # 所有被 sidebar 组显式引用的 path：它们由组循环按 child_depth  emit，
    # emit_descendants 不得按 path 前缀捕获（menu tree 平铺 vs sidebar 分组
    # 语义冲突：mapping 的 path 前缀像 standard 的后代，但 sidebar 里它是
    # 登录组的直属条目）
    grouped = set()
    for g in groups:
        for cp in g.get("children_path") or []:
            grouped.add(cp)

    def emit_descendants(parent_key):
        prefix = parent_key + "/"
        for nd in nodes:
            p = nd["path"]
            if p.startswith(prefix) and p not in emitted and p not in grouped:
                emitted.add(p)
                d = depth_of(nd, parent_key,
                             emitted_depth.get(parent_key, 0))
                items.append({"key": p, "title": nd["title"],
                              "kind": kind_of(nd), "depth": d})
                emitted_depth[p] = d

    for g in groups:
        t = (g.get("group_title") or "").strip()
        child_depth = 1
        if t:
            gkey = "_sidebar_group__%s__%s" % (section_path, t)
            items.append({"key": gkey, "title": t, "kind": "group", "depth": 1})
            emitted_depth[gkey] = 1
            child_depth = 2
        for cp in g.get("children_path") or []:
            nd = by_path.get(cp)
            if not nd or cp in emitted:
                continue
            emitted.add(cp)
            items.append({"key": cp, "title": nd["title"],
                          "kind": kind_of(nd), "depth": child_depth})
            emitted_depth[cp] = child_depth
            emit_descendants(cp)

    # 兜底：menu JSON 有但 sidebar 未渲染的直属子级
    # （sync 主流程会按路径兄弟归入相邻分组，预览里如实标注）
    for nd in nodes:
        p = nd["path"]
        if p in emitted:
            continue
        if nd.get("parent_path") == root_path:
            emitted.add(p)
            items.append({"key": p, "title": nd["title"], "kind": kind_of(nd),
                          "depth": 1,
                          "note": "sidebar 隐藏页，将按路径归入相邻分组"})
            emitted_depth[p] = 1
            emit_descendants(p)

    # 最终兜底：任何未覆盖节点按 parent_path 链附上（防御性，正常不会走到）
    for nd in nodes:
        p = nd["path"]
        if p not in emitted:
            emitted.add(p)
            d = depth_of(nd, root_path, 0)
            items.append({"key": p, "title": nd["title"], "kind": kind_of(nd),
                          "depth": d})
            emitted_depth[p] = d
    return items
