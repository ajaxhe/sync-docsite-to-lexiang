# 站点适配器指南（site_crawler.py 扩展参考）

`site_crawler.py` 的探测流程与适配器架构说明。遇到新站点类型时先读本文，评估走现有降级路径还是新增适配器。

## 探测流程（probe_site）

```
入口 URL
  → GET 页面
  → ① Next.js/Nextra 适配器（找 __NEXT_DATA__ + 菜单树）
  → ② 失败则通用降级（扫描页面 <a> 链接推导目录树）
  → 返回 profile: {site_type, base_url, docs_prefix, menu_tree / link_tree}
```

## 适配器 1：Next.js / Nextra（site_type=nextjs-nextra）

适用于用 Nextra 搭建的文档站（页面源码含 `__NEXT_DATA__` JSON）。已验证站点：identity.tencent.com。

**菜单树提取**（侧边栏是 div+JS 渲染，无 `<a>` 标签可抓）：

1. 从 `__NEXT_DATA__` 取 `buildId` 与 `assetPrefix`（docs_prefix = assetPrefix 去首尾斜杠，如 `docs`）
2. 定位 `_app-{hash}.js` chunk（HTML 中 `<script src>` 引用），下载
3. 在 chunk 内搜 `originMdxHub=变量名`（Nextra 菜单数据挂载点），定位 `变量名=[` 的数组起点
4. JS 对象字面量 → JSON：无引号 key 补引号、`!0/!1` → true/false、值位置裸标识符（变量引用如 `content:s`）→ null、括号配平提取
5. 兜底：若 `originMdxHub` 不存在，扫描 chunk 中所有大数组，找节点含 `path`+`title` 结构的数组
6. 节点结构 `{title, path, url, directory, children}`；页面 URL = `base_url + "/" + assetPrefix + "/" + path`

**正文提取**：`__NEXT_DATA__` 有 `pageProps.updateTime`（增量检测用）；正文容器探测顺序（CONTENT_SELECTORS）：
`id=layout-content-container` → `id=content` → Docusaurus class → `<main>` → `<article>`

**坑**：
- docs_prefix 必须取 assetPrefix，不能取入口 URL 路径前缀——菜单 path 已含分区名（如 `guides/Directory`），否则 URL 拼成 `docs/guides/guides/Directory/` 重复
- 正文 HTML 转换前必须剥 `<script>/<style>/<svg>`（`_extract_div_block` 配平可能溢出泄漏）

## 适配器 2：通用降级（site_type=generic）

从入口页与同站页面扫 `<a href>` 归纳目录树（仅适合侧边栏为真实链接的传统站点）。**SPA/JS 渲染站点会失败**（菜单为空）→ 上层停止并报告。

## 新增适配器检查清单

遇到 generic 降级失败的站点，按以下顺序判断：

1. 页面源码是否有 `__NEXT_DATA__` → 已被适配器 1 覆盖
2. 是否 Docusaurus / VitePress / rspress 等：查看 HTML 特征（`__docusaurus`、`<div id="app">` + window 对象中的 sidebar 数据）。这类框架菜单数据一般内嵌在页面或可预测的 JS 里，做法参照 Nextra：找数据源数组 → JS 字面量转 JSON
3. 纯 CSR（需执行 JS 才有内容）→ 脚本无浏览器引擎，**不支持**，如实告知用户
4. 需要登录 → **不支持**（见 SKILL.md 执行边界）

新增适配器的代码位置：`site_crawler.py` 中加一个 `probe_<framework>()` 函数并在 `probe_site()` 尝试链中插入；正文容器选择器追加到 `CONTENT_SELECTORS`。

## html2md 转换能力边界

| 源元素 | 转换结果 |
|--------|----------|
| h1-h6 | `#`-`######`（正文首个 H1 跳过，与页面标题重复） |
| 粗/斜/删除/行内代码/链接 | 对应 markdown 行内标记；相对链接自动转绝对 |
| ol/ul（含嵌套、ol start 属性） | 嵌套列表 |
| table | GFM 表格（cell 内链接支持） |
| pre/code | fenced 代码块（保留语言） |
| img | `![name](filename)` 占位 + media 列表（下载后内嵌） |
| video/audio | attachment 占位 + media 列表 |
| highlightBlock/callout | `> 💡 文本` / `> ⚠️ 文本` |
| iconfont 图标（i/span.iconfont） | 跳过（避免空斜体标记） |

已知限制：公式保持原文文本；复杂嵌套 callout 内多级列表会拍平。
