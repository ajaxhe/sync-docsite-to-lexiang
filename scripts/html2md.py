#!/usr/bin/env python3
"""
html2md.py - 文档站正文 HTML → Markdown 转换器（纯标准库 html.parser）

输入：正文容器 HTML（site_crawler.extract_content_html 的输出）
输出：
  markdown 文本（图片/视频以本地相对路径引用）
  media 列表 [{kind: image|video|audio, src(绝对URL), filename(本地名)}]

转换能力：
  h1-h6（正文 H1 跳过，页面标题由 entry name 承担）、段落、
  粗体/斜体/删除线/行内代码、链接（站内相对链接转绝对）、
  有序/无序列表（含嵌套与 start 属性）、表格（GFM，单元格内链接）、
  代码块（fenced + 语言）、引用块、分割线、图片、视频/音频占位、
  callout 高亮块（Nextra highlightBlock → blockquote + emoji）。
"""

import re
from html.parser import HTMLParser

VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source",
             "area", "base", "col", "embed", "track", "wbr"}

# callout 图标 class → emoji（Nextra 类站点）
CALLOUT_ICONS = {
    "TxRemind": "💡",
    "TxNotice": "⚠️",
    "TxCircle": "✅",
    "TxHelp": "❓",
}


def _slug_filename(src, idx, default_ext="png"):
    """从 URL 生成本地文件名：优先取路径末段，否则 media_{idx}。"""
    name = src.split("?")[0].split("#")[0].rstrip("/")
    base = name.rsplit("/", 1)[-1] if "/" in name else ""
    if "." in base and len(base) < 120:
        return base
    return f"media_{idx}.{default_ext}"


class _MdBuilder(HTMLParser):
    """标签栈式 HTML→Markdown 转换器。"""

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url.rstrip("/")
        self.out = []            # 输出块缓冲
        self.inline = []         # 当前行内片段
        self.media = []
        # 标签栈：元素 {tag, meta}；meta 携带 callout/li 等状态
        self._stack = [{"tag": "root", "meta": {}}]
        self._skip = 0           # 跳过容器深度
        self._h_level = 0
        self._h_buf = []
        self._in_pre = False
        self._pre_buf = []
        self._pre_lang = ""
        self._a_stack = []       # 链接 href 栈
        self._in_cell = False
        self._cell_buf = []

    # ── 工具 ──────────────────────────────────────────────

    def abs_url(self, url):
        if not url:
            return url
        if url.startswith(("http://", "https://", "mailto:", "#")):
            return url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return self.base_url + url
        return self.base_url + "/" + url

    @property
    def href(self):
        return self._a_stack[-1] if self._a_stack else None

    def _flush_inline(self):
        text = "".join(self.inline)
        self.inline = []
        return text.strip()

    def _emit(self, text):
        if text and text.strip():
            self.out.append(text.strip())

    # ── 开始标签 ──────────────────────────────────────────

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        meta = {}

        # 需要跳过的容器（右侧锚点导航等）
        if tag == "aside" or re.search(r"\banchor_\w+|table-of-contents", cls):
            self._skip += 1
            return
        if self._skip:
            if tag not in VOID_TAGS:
                self._skip += 1
            return

        # 图标字体标签（iconfont/icon-）：完全跳过
        if tag in ("i", "span") and re.search(r"iconfont|icon-|Icon_", cls):
            return
        if tag == "i" and not cls:
            pass  # 纯 <i> 视为斜体（少见）

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._h_level = int(tag[1])
            self._h_buf = []
        elif tag in ("strong", "b") and not self._in_pre:
            self.inline.append("**")
        elif tag in ("em", "i") and not self._in_pre:
            self.inline.append("*")
        elif tag in ("del", "s", "strike") and not self._in_pre:
            self.inline.append("~~")
        elif tag == "code" and not self._in_pre:
            self.inline.append("`")
        elif tag == "a":
            href = self.abs_url(a.get("href") or "")
            self._a_stack.append(href if href and href != "#" else None)
        elif tag == "br":
            self.inline.append("\n")
        elif tag == "hr":
            self._emit("---")
        elif tag == "img":
            src = self.abs_url(a.get("src") or a.get("data-src") or "")
            if src:
                idx = len(self.media)
                fname = _slug_filename(src, idx)
                self.media.append(
                    {"kind": "image", "src": src, "filename": fname})
                self.inline.append(f"\n\n![{a.get('alt') or ''}]({fname})\n\n")
        elif tag in ("video", "audio"):
            kind = "video" if tag == "video" else "audio"
            src = self.abs_url(a.get("src") or "")
            if src:
                self._add_media(
                    kind, src,
                    "mp4" if kind == "video" else "mp3",
                    "【视频】" if kind == "video" else "【音频】")
        elif tag == "source":
            src = self.abs_url(a.get("src") or "")
            # 仅在 video/audio 容器内处理（栈顶判断）
            if src and self._stack[-1]["tag"] in ("video", "audio"):
                if not any(m["src"] == src for m in self.media):
                    ext = src.rsplit(".", 1)[-1][:4] if "." in src else "mp4"
                    self._add_media("video", src, ext, "【视频】")
        elif tag in ("ul", "ol"):
            start = 0
            if tag == "ol":
                try:
                    start = int(a.get("start") or 1) - 1
                except ValueError:
                    start = 0
            meta["list"] = [tag, start]
            # 列表开始：把之前未 flush 的行内内容作为独立块
            text = self._flush_inline()
            if text:
                self._emit(text)
        elif tag == "li":
            parent = self._find_parent_list()
            if parent:
                lst = parent["meta"]["list"]
                if lst[0] == "ol":
                    lst[1] += 1
                    marker = f"{lst[1]}. "
                else:
                    marker = "- "
                depth = self._list_depth()
                meta["li_marker"] = "  " * max(0, depth - 1) + marker
                # li 用独立 inline 缓冲：先保存外层
                meta["saved_inline"] = list(self.inline)
                self.inline = []
        elif tag == "pre":
            self._in_pre = True
            self._pre_buf = []
            m = re.search(r"(?:language-|lang-)([\w+#-]+)", cls)
            self._pre_lang = m.group(1) if m else ""
        elif tag == "blockquote":
            meta["quote"] = True
        elif tag == "div" and "highlightBlock" in cls:
            emoji = "💡"
            for icon, e in CALLOUT_ICONS.items():
                if icon in cls:
                    emoji = e
                    break
            meta["callout"] = emoji
            meta["saved_out"] = list(self.out)
            self.out = []
        elif tag == "table":
            meta["table"] = {"rows": []}
        elif tag == "tr":
            meta["tr"] = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell_buf = []

        if tag not in VOID_TAGS:
            self._stack.append({"tag": tag, "meta": meta})

    def _add_media(self, kind, src, ext, label):
        idx = len(self.media)
        fname = _slug_filename(src, idx, ext)
        self.media.append({"kind": kind, "src": src, "filename": fname})
        self.inline.append(f"\n\n{label}({fname})\n\n")

    def _find_parent_list(self):
        for frame in reversed(self._stack):
            if "list" in frame["meta"]:
                return frame
        return None

    def _list_depth(self):
        return sum(1 for f in self._stack if "list" in f["meta"])

    # ── 结束标签 ──────────────────────────────────────────

    def handle_endtag(self, tag):
        if self._skip:
            if tag not in VOID_TAGS:
                self._skip -= 1
            return

        # 从栈中弹出对应帧（容忍未闭合标签）
        frame = None
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i]["tag"] == tag:
                frame = self._stack.pop(i)
                # 同时弹掉内部未闭合的帧
                del self._stack[i + 1:]
                break
        meta = frame["meta"] if frame else {}

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = re.sub(r"\s+", " ", "".join(self._h_buf)).strip()
            self._h_level = 0
            self.inline = []
            if text and int(tag[1]) > 1:
                self._emit("#" * int(tag[1]) + " " + text)
        elif tag == "p":
            if not self._in_cell:
                text = self._flush_inline()
                if text:
                    self._emit(text)
        elif tag in ("strong", "b") and not self._in_pre:
            self.inline.append("**")
        elif tag in ("em", "i") and not self._in_pre:
            self.inline.append("*")
        elif tag in ("del", "s", "strike") and not self._in_pre:
            self.inline.append("~~")
        elif tag == "code" and not self._in_pre:
            self.inline.append("`")
        elif tag == "a":
            if self._a_stack:
                self._a_stack.pop()
        elif tag in ("ul", "ol"):
            text = self._flush_inline()
            if text:
                self._emit(text)
        elif tag == "li":
            if "li_marker" in meta:
                text = self._flush_inline()
                marker = meta["li_marker"]
                # 还原外层 inline
                self.inline = meta.get("saved_inline") or []
                if text:
                    self._emit(marker + text)
                rest = self._flush_inline()
                if rest:
                    self._emit(rest)
        elif tag == "pre":
            self._in_pre = False
            code = "".join(self._pre_buf)
            self._emit("```" + self._pre_lang + "\n" +
                       code.rstrip("\n") + "\n```")
            self._pre_lang = ""
        elif tag == "div":
            if "callout" in meta:
                emoji = meta["callout"]
                inner = [l for l in self.out if l.strip()]
                self.out = meta["saved_out"]
                for l in inner:
                    l = re.sub(r"^> \S+\s*", "", l)  # 去嵌套重复前缀
                    self._emit(f"> {emoji} {l}")
        elif tag == "table":
            rows = meta.get("table", {}).get("rows", [])
            self._emit_table(rows)
        elif tag == "tr":
            rows = None
            for f in reversed(self._stack):
                if "table" in f["meta"]:
                    rows = f["meta"]["table"]["rows"]
                    break
            row = meta.get("tr") or []
            if rows is not None and row:
                rows.append(row)
        elif tag in ("td", "th"):
            self._in_cell = False
            cell = "".join(self._cell_buf)
            cell = re.sub(r"\s+", " ", cell).strip().replace("|", "\\|")
            for f in reversed(self._stack):
                if "tr" in f["meta"]:
                    f["meta"]["tr"].append(cell)
                    break
            self._cell_buf = []

    def _emit_table(self, rows):
        if not rows:
            return
        width = max(len(r) for r in rows)
        for r in rows:
            r.extend([""] * (width - len(r)))
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        for r in rows[1:]:
            lines.append("| " + " | ".join(r) + " |")
        self._emit("\n".join(lines))

    # ── 文本 ──────────────────────────────────────────────

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_cell:
            if self.href and data.strip():
                self._cell_buf.append(f"[{data.strip()}]({self.href})")
            else:
                self._cell_buf.append(data)
            return
        if self._h_level:
            self._h_buf.append(data)
            return
        if self._in_pre:
            self._pre_buf.append(data)
            return
        if not data:
            return
        if data.strip() == "":
            if self.inline and not self.inline[-1].endswith(
                    (" ", "\n", "**", "*", "`", "[")):
                self.inline.append(" ")
        elif self.href:
            self.inline.append(f"[{data}]({self.href})")
        else:
            self.inline.append(data)


def convert(content_html, base_url, title=""):
    """正文 HTML → (markdown, media_list)。解析异常时降级纯文本。"""
    # 剥掉脚本/样式（容器配平溢出到页面尾部时可能带上）
    content_html = re.sub(r"<script[^>]*>.*?</script>", " ", content_html,
                          flags=re.S)
    content_html = re.sub(r"<style[^>]*>.*?</style>", " ", content_html,
                          flags=re.S)
    content_html = re.sub(r"<svg[^>]*>.*?</svg>", " ", content_html,
                          flags=re.S)
    p = _MdBuilder(base_url)
    try:
        p.feed(content_html)
        p.close()
        md = "\n\n".join(p.out)
        rest = p._flush_inline()
        if rest:
            md += "\n\n" + rest
    except Exception:
        text = re.sub(r"<[^>]+>", " ", content_html)
        md = re.sub(r"\s+", " ", text).strip()
        return md, []
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md, p.media


if __name__ == "__main__":
    import sys
    import json
    from site_crawler import fetch_page
    url = sys.argv[1] if len(sys.argv) > 1 else \
        "https://identity.tencent.com/docs/guides/Directory/CreatUser/"
    page = fetch_page(url)
    md, media = convert(page["content_html"], "/".join(url.split("/")[:3]))
    print("=== MARKDOWN (%d chars) ===" % len(md))
    print(md[:1500])
    print("=== MEDIA ===")
    print(json.dumps(media, ensure_ascii=False, indent=1))
