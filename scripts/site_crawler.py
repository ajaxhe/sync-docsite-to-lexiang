#!/usr/bin/env python3
"""
site_crawler.py - 文档站点探测与抓取（纯标准库）

职责：
  1. probe_site(entry_url)：识别站点类型，提取侧边栏菜单树
  2. fetch_page(url)：抓取单页，返回正文 HTML + 元信息（updateTime 等）
  3. extract_content(html)：按适配器提取正文容器 HTML

已适配站点类型：
  - nextjs-nextra：Next.js SSG + Nextra 类框架（菜单树内嵌在 _app chunk）
  - generic：通用 SSR/静态文档站（正文取 <main>/<article>，菜单从 nav 链接推导）

菜单树节点结构（统一输出）：
  {title, path, url, directory(bool), children: [...]}
path 为站点内相对路径（如 guides/Directory），url 为绝对 URL。

仅依赖 Python 标准库。抓取结果不进入 LLM 上下文。
"""

import json
import re
import urllib.parse
import urllib.request
import urllib.error

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

FETCH_TIMEOUT = 30


class CrawlError(Exception):
    pass


def http_get(url, binary=False, timeout=FETCH_TIMEOUT):
    """带 UA 的 GET，返回文本或字节。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise CrawlError(f"HTTP {e.code} 抓取失败: {url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CrawlError(f"网络错误 抓取失败: {url}: {e}") from e
    if binary:
        return data
    return data.decode("utf-8", "replace")


# ── Next.js / Nextra 适配 ─────────────────────────────────────

def _extract_next_data(html):
    """提取 __NEXT_DATA__ JSON；不存在返回 None。"""
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _balance_array(js, start):
    """从 js[start]（应为 '['）开始括号配平提取数组文本。"""
    depth = 0
    i = start
    in_str = False
    esc = False
    while i < len(js):
        c = js[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return js[start:i + 1]
        i += 1
    raise CrawlError("JS 数组括号不平衡（chunk 截断？）")


def _js_literal_to_json(text):
    """JS 对象/数组字面量 → JSON 文本（key 加引号、!0/!1、变量引用置 null）。"""
    out = []
    i = 0
    n = len(text)
    in_str = False
    esc = False

    def emit_value(j):
        """值位置的裸标识符处理；返回新的 i。"""
        k = j
        while k < n and text[k] in " \n\t":
            k += 1
        if k < n and (text[k].isalpha() or text[k] in "_$"):
            e = k
            while e < n and (text[e].isalnum() or text[e] in "_$"):
                e += 1
            word = text[k:e]
            if word in ("true", "false", "null"):
                out.append(word)
            else:
                out.append("null")  # 变量引用（如编译后的 MDX 模块）
            return e
        return j

    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "!" and i + 1 < n and text[i + 1] in "01":
            out.append("true" if text[i + 1] == "0" else "false")
            i += 2
            continue
        if c == "{" or c == ",":
            out.append(c)
            i += 1
            j = i
            while j < n and text[j] in " \n\t":
                j += 1
            if j < n and (text[j].isalpha() or text[j] == "_"):
                k = j
                while k < n and (text[k].isalnum() or text[k] in "_$"):
                    k += 1
                if k < n and text[k] == ":":
                    out.append('"' + text[j:k] + '":')
                    i = k + 1
                    i = emit_value(i)
                    continue
            continue
        if c == ":":
            out.append(c)
            i += 1
            i = emit_value(i)
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _find_hub_array(js):
    """
    在 _app chunk 中定位「文档菜单树」数组（originMdxHub 引用的变量）。
    返回数组文本或 None。
    """
    # 策略 1：originMdxHub=VAR → 找 VAR=[ 定义
    m = re.search(r"originMdxHub=([A-Za-z_$][\w$]*)", js)
    if m:
        var = m.group(1)
        for dm in re.finditer(
                r"[,;]" + re.escape(var) + r"=\[", js):
            start = dm.end() - 1
            try:
                return _balance_array(js, start)
            except CrawlError:
                continue
        # 变量可能带前导逗号以外的形式
        for dm in re.finditer(
                r"\b" + re.escape(var) + r"\s*=\s*\[", js):
            start = js.find("[", dm.start())
            try:
                return _balance_array(js, start)
            except CrawlError:
                continue
    # 策略 2 兜底：扫描所有 [{ 开头的数组，找含 path+title+children 结构的
    best = None
    for am in re.finditer(r"=\[\{", js):
        start = am.start() + 1
        try:
            text = _balance_array(js, start)
        except CrawlError:
            continue
        if ('"path"' in text or "path:" in text) and \
                ('"title"' in text or "title:" in text) and \
                len(text) > 2000:
            if best is None or len(text) > len(best):
                best = text
    return best


def _normalize_tree(nodes, docs_prefix, base_url):
    """把站点菜单树节点归一化：补 url、剥离无关字段。"""
    result = []
    for nd in nodes or []:
        if not isinstance(nd, dict):
            continue
        path = str(nd.get("path") or "").strip("/")
        if not path:
            continue
        url = base_url + "/" + docs_prefix + "/" + path + "/"
        item = {
            "title": nd.get("title") or path,
            "path": path,
            "url": url,
            "directory": bool(nd.get("directory")),
        }
        children = _normalize_tree(
            nd.get("children"), docs_prefix, base_url)
        if children:
            item["children"] = children
        result.append(item)
    return result


def probe_nextjs_site(entry_url):
    """
    探测 Next.js/Nextra 文档站。
    返回 {site_type, base_url, docs_prefix, menu_tree, build_id} 或 None。
    """
    html = http_get(entry_url)
    nd = _extract_next_data(html)
    if not nd:
        return None
    build_id = nd.get("buildId", "")
    # assetPrefix（如 /docs）：菜单树 path 相对 assetPrefix
    asset_prefix = (nd.get("assetPrefix") or "").strip("/")
    query_slug = (nd.get("query") or {}).get("slug") or []
    if not isinstance(query_slug, list):
        query_slug = [query_slug] if query_slug else []
    if not query_slug:
        return None
    parsed = urllib.parse.urlparse(entry_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    # docs_prefix = assetPrefix（菜单 path 形如 guides/Directory，URL = base/assetPrefix/path）
    docs_prefix = asset_prefix

    # 找 _app chunk URL
    app_chunk = None
    for sm in re.finditer(r'src="([^"]*_app-[\w]+\.js)"', html):
        app_chunk = sm.group(1)
        break
    if not app_chunk:
        return None
    if app_chunk.startswith("//"):
        app_chunk = "https:" + app_chunk
    elif app_chunk.startswith("/"):
        app_chunk = base_url + app_chunk

    chunk = http_get(app_chunk)
    arr_text = _find_hub_array(chunk)
    if not arr_text:
        return None
    try:
        raw_tree = json.loads(_js_literal_to_json(arr_text))
    except json.JSONDecodeError as e:
        raise CrawlError(f"菜单树 JS 字面量解析失败: {e}")

    # 归一化：补 url（base/assetPrefix/path）、剥无关字段
    menu_tree = _normalize_tree(raw_tree, docs_prefix, base_url)
    if not menu_tree:
        return None

    # 解析 sidebar HTML 中的视觉分组（如「通讯录/登录/企业设置」等组标题）
    # ——Nextra 把组标题只放在 sidebar HTML 里，菜单 JSON 完全没有这层。
    # 返回 sidebar_section_groups: {section_path: [{title|None, children:[node_titles 按 sidebar 顺序]}], ...}
    try:
        sidebar_groups = _parse_sidebar_groups(html, menu_tree)
    except Exception:
        sidebar_groups = {}

    return {
        "site_type": "nextjs-nextra",
        "base_url": base_url,
        "docs_prefix": docs_prefix,
        "build_id": build_id,
        "menu_tree": menu_tree,
        "sidebar_section_groups": sidebar_groups,
    }


# ── Sidebar 视觉分组解析（Nextra 专用）────────────────────────

# 两条相邻的 div class="sideBar_menuItem__1fwe_" 之间用 < 看前面是否还有空 div。
# 改用 stack 配平提取：scan 一个 div 块、找到 </div> 与 start 配平的那一个。
_SBAR_ITEM_OPEN = '<div class="sideBar_menuItem__1fwe_">'


def _parse_sidebar_groups(html, menu_tree):
    """
    从入口页 sidebar HTML 解析视觉分组结构。

    识别规则（Nextra identity 站实测）：
      - 顶层 menuItem 块以 '<div class="sideBar_menuItem__1fwe_">' 开头
      - 块内首段若仅含 itemHeader（无 linkContainer）= 纯分组标题（如「通讯录」）
      - 块内首段若 itemHeader + linkContainer 序列 = 「分组标题 + 其下子项」
      - 块内若仅 linkContainer 序列（无 itemHeader）= 上一组的延续子项
    返回 {section_path: [{group_title or None, children_path: [NodePath 的 path 列表顺序]}]}
    至少保证 children_path 是 menu_tree 里能找到 title 的节点（按 sidebar 顺序）。
    """
    # 按 menu_tree 顶层（一般为「使用指南」「开发文档」）分 section
    # ——入口页 sidebar 默认渲染第一个 section 的展开视图
    sections = {}
    for top in menu_tree:
        children = top.get("children") or []
        sec_groups = []
        # 在 sidebar HTML 里找包含当前 section 子树任意 title 的 linkContainer
        # 但通用做法：粗略地，按 sidebar 顺序逐项匹配 children
        child_titles = [c["title"] for c in children]
        # 把整个 sidebar HTML 切成 顶层 menuItem 块
        blocks = _split_sidebar_top_items(html, top.get("title", ""))
        cur_group = {"group_title": None, "children_path": []}
        # 反向匹配：blocks 给出的每个条目（含 group header / 子节点 title）vs children
        # 我们需要「sidebar item 序列」按出现顺序 ↔ children 顺序
        flat = []  # [(kind, title), ...]  kind: 'header' | 'link'
        for blk in blocks:
            hdr = re.search(
                r'sideBar_itemHeader__nogwv[^>]*>(?:<div>)?([^<]+)<',
                blk)
            # itemHeader 紧跟着的 linkContainer 链（每个含一个子项 title）
            link_titles = re.findall(
                r'class="sideBar_linkContent__ObogE"[^>]*>(?:<i[^>]*></i>)?<span>([^<]+)</span>',
                blk)
            if hdr and not link_titles:
                flat.append(("header", hdr.group(1).strip()))
            elif hdr and link_titles:
                flat.append(("header", hdr.group(1).strip()))
                for t in link_titles:
                    flat.append(("link", t.strip()))
            else:
                for t in link_titles:
                    flat.append(("link", t.strip()))
        # 把 flat 与 child_titles 按顺序对齐：每个 link title 应在 child_titles 中；
        # 但偶尔 sidebar 也包含更深层（三级）条目，所以我们按出现顺序一对一消费
        child_iter = iter(range(len(children)))
        child_idx = 0
        for kind, title in flat:
            if kind == "header":
                # 新的 group 标题；若当前 group_title 与新的一致则合并
                if cur_group["children_path"] or cur_group["group_title"]:
                    sec_groups.append(cur_group)
                cur_group = {"group_title": title, "children_path": []}
                continue
            # link：从 child_titles 中按出现顺序找一个未消费的匹配项
            matched = None
            for j in range(child_idx, len(children)):
                if children[j]["title"] == title:
                    matched = j
                    break
            if matched is None:
                # 兜底：消费位置 ≥ child_idx 但 title 未匹配（depth>2 子页）
                # 不做特殊处理，跳过
                continue
            cur_group["children_path"].append(children[matched]["path"])
            child_idx = matched + 1
        if cur_group["children_path"] or cur_group["group_title"]:
            sec_groups.append(cur_group)
        sections[top["path"]] = sec_groups
    return sections


def _split_sidebar_top_items(html, section_title):
    """
    把 sidebar HTML（限定到主容器 sideBar 内部）中顶层
    <div class="sideBar_menuItem__1fwe_">...</div> 块切出来。
    实现：从首个 menuItem 开始，按 div 配平切块。
    """
    # 限制在侧边栏主容器内（'sideBar_' 标记最近的容器）
    sb_start = html.find('class="sideBar_')
    if sb_start < 0:
        return []
    # 主容器不是 menuItem 本体——从该容器之后第一个 menuItem 开始
    pos = html.find(_SBAR_ITEM_OPEN, sb_start)
    blocks = []
    while pos >= 0:
        # 在 pos 之后用 div 配平找匹配的 </div>
        depth = 0
        i = pos
        end = -1
        while i < len(html):
            if html[i] != "<":
                i += 1
                continue
            if html.startswith("<div", i):
                depth += 1
                # 跳到 '>' 后
                i = html.find(">", i) + 1
                continue
            if html.startswith("</div>", i):
                depth -= 1
                if depth == 0:
                    end = i + len("</div>")
                    break
                i += len("</div>")
                continue
            i += 1
        if end < 0:
            break
        blocks.append(html[pos:end])
        pos = html.find(_SBAR_ITEM_OPEN, end)
    return blocks


# ── 通用站点适配 ──────────────────────────────────────────────

def probe_generic_site(entry_url):
    """
    通用探测：抓入口页，提取站内文档链接（限同前缀），按 URL 路径推导层级。
    层级 = URL 路径段。仅当无法提取结构化菜单树时使用。
    """
    html = http_get(entry_url)
    parsed = urllib.parse.urlparse(entry_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    path_parts = [p for p in parsed.path.split("/") if p]
    # 文档前缀：去掉最后一段（页面本身）作为兄弟页面公共前缀
    prefix_parts = path_parts[:-1]
    docs_prefix = "/".join(prefix_parts)

    links = set(re.findall(r'href="(/' + re.escape(docs_prefix) +
                           r'/[^"#?]+)"', html))
    # 归一化：去尾部斜杠、去重
    norm = sorted({l.rstrip("/") for l in links})
    tree = {}
    for l in norm:
        parts = [p for p in l.split("/") if p][len(prefix_parts):]
        if not parts:
            continue
        node = tree
        for idx, seg in enumerate(parts):
            key = "/".join(parts[: idx + 1])
            if key not in node:
                node[key] = {"title": seg, "path": key,
                             "url": base_url + "/" + key + "/",
                             "directory": idx < len(parts) - 1, "_ch": {}}
            node = node[key]["_ch"]
    def unfold(d):
        out = []
        for k in sorted(d):
            item = dict(d[k])
            ch = item.pop("_ch", {})
            if ch:
                item["children"] = unfold(ch)
            out.append(item)
        return out
    return {
        "site_type": "generic",
        "base_url": base_url,
        "docs_prefix": docs_prefix,
        "build_id": "",
        "menu_tree": [],
        "link_tree": unfold(tree),
    }


def probe_site(entry_url):
    """探测站点：优先 Next.js/Nextra 结构化菜单树，失败降级通用链接推导。"""
    entry_url = entry_url.rstrip("/") + "/"
    try:
        profile = probe_nextjs_site(entry_url)
        if profile and profile.get("menu_tree"):
            return profile
    except CrawlError:
        pass
    profile = probe_generic_site(entry_url)
    return profile


# ── 页面抓取与正文提取 ────────────────────────────────────────

# 正文容器探测顺序（第一个命中的优先）
CONTENT_SELECTORS = [
    ('id', 'layout-content-container'),   # identity.tencent.com（Nextra 定制）
    ('id', 'content'),
    ('id', 'main-content'),
    ('class', 'theme-doc-markdown'),      # Docusaurus
    ('tag', 'main'),
    ('tag', 'article'),
]


def _find_tag_end(html, start):
    """从 '<div' 开始找匹配的 '>'，跳过属性内的引号。"""
    i = start
    in_str = None
    while i < len(html):
        c = html[i]
        if in_str:
            if c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
        elif c == ">":
            return i
        i += 1
    return len(html) - 1


def _extract_div_block(html, needle):
    """
    提取 <div ...needle...> 开始的完整 div 块（同层闭合配平）。
    返回块内 HTML（含外层 div）或 None。
    """
    idx = html.find(needle)
    if idx < 0:
        return None
    # 回退到 '<'
    tag_start = html.rfind("<", 0, idx)
    if tag_start < 0:
        return None
    tag_end = _find_tag_end(html, tag_start)
    # 向后配平 <div 与 </div>
    depth = 1
    i = tag_end + 1
    in_str = False
    esc = False
    n = len(html)
    while i < n and depth > 0:
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            i += 1
            continue
        if c == "<":
            if html.startswith("</div", i):
                depth -= 1
                i += 5
                continue
            if html.startswith("<div", i):
                depth += 1
                i += 4
                continue
        if c == '"':
            in_str = True
        i += 1
    return html[tag_start:i]


def extract_content_html(html):
    """按选择器探测顺序提取正文 HTML；全部失败返回 None。"""
    for kind, sel in CONTENT_SELECTORS:
        if kind == "id":
            block = _extract_div_block(html, f'id="{sel}"')
        elif kind == "class":
            m = re.search(r'class="([^"]*\b' + re.escape(sel) +
                          r'\b[^"]*)"', html)
            block = _extract_div_block(html, m.group(0)) if m else None
        else:  # tag
            m = re.search(r"<" + sel + r"[\s>]", html)
            block = None
            if m:
                close = html.find("</" + sel + ">", m.start())
                block = html[m.start(): close + len(sel) + 3] if close > 0 \
                    else None
        if block:
            return block
    return None


def fetch_page(url):
    """
    抓取单页。返回：
      {url, html, content_html, update_time, title}
    update_time：__NEXT_DATA__.pageProps.updateTime（Next.js 站点），
                 其他站点为页面 Last-Modified 代理（常为空）。
    title：正文 H1 文本（提取失败为空）。
    """
    url = url.rstrip("/") + "/"
    html = http_get(url)
    page = {"url": url, "html": html, "update_time": "",
            "title": "", "content_html": ""}
    nd = _extract_next_data(html)
    if nd:
        props = (nd.get("props") or {}).get("pageProps") or {}
        page["update_time"] = str(props.get("updateTime") or "")
    content = extract_content_html(html)
    page["content_html"] = content or ""
    if content:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.S)
        if m:
            page["title"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return page


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else \
        "https://identity.tencent.com/docs/guides/Directory/"
    prof = probe_site(target)
    print(json.dumps({
        k: (v if k != "menu_tree" else
            [{"top": n.get("name"), "title": n.get("title"),
              "children": len(n.get("children") or [])}
             for n in v])
        for k, v in prof.items()}, ensure_ascii=False, indent=1))
