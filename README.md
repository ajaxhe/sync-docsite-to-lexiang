# Sync Docsite to Lexiang

把公开文档站点（Next.js/Nextra、Docusaurus 等侧边栏目录树结构的文档中心 / 帮助中心）**整站或分区递归导入**到腾讯乐享知识库，保持原站目录层级与 sidebar 视觉分组，图片/音视频/附件内嵌上传；支持**增量同步**（只更新变更页）与**删除同步**（源端删除 → 乐享移入回收站）。

全程由 Python 脚本完成，文档内容不进入大模型上下文——无论 1 页还是 1000 页，token 消耗几乎为零。

## 特性

- **站点探测自适应**：自动识别 Next.js/Nextra 站点（解析菜单 JSON + sidebar HTML），其他框架可扩展适配器（见 `references/site-adapters.md`）
- **目录结构还原**：递归遍历源站菜单树，在乐享逐级重建；sidebar 视觉分组（如「通讯录 / 登录 / 企业设置」粗体标题）还原为分组目录，组内顺序与源站一致
- **媒体内嵌**：文档内图片下载后按原始顺序内嵌为 image block；视频/音频/PDF/压缩包以 attachment 卡片上传
- **增量同步**：以页面内容 hash（SHA256）判断变更，未变的页面秒级跳过
- **删除同步（安全）**：源端消失的页面移入乐享回收站文件夹 `_回收站_站点同步`（不是真删除）；消失比例 >30% 自动中止，需 `--force-delete` 才继续
- **幂等 + 断点续传**：每项成功立即落盘 manifest，中断后重跑不重不漏；状态按域名隔离，跨会话续传
- **WAF 消毒**：自动处理会触发乐享 WAF 误判的内容模式（如 LDAP 过滤器），保证上传成功

## 前置要求

- Python 3.9+
- 目标站点**公开可访问**（需要登录 / 付费墙 / 验证码的站点不支持，也不尝试绕过）
- 乐享 MCP API Token（`lxmcp_` 开头）：从 <https://lexiangla.com/ai/claw> 获取

## 安装

```bash
git clone https://github.com/ajaxhe/sync-docsite-to-lexiang.git \
  ~/.workbuddy/skills/sync-docsite-to-lexiang
```

也可复制到任意 Agent 的 skills 根目录（如 `~/.workbuddy/skills/`）。

## 快速开始

### 1. 探测站点（不写入任何东西）

```bash
cd ~/.workbuddy/skills/sync-docsite-to-lexiang/scripts
python3 sync.py --url "<站点入口URL>" \
  --target-space-id <乐享space_id> --probe-only
```

输出 JSON 含站点类型（`nextjs-nextra` / `generic`）、菜单树预览、页面总数。

### 2. dry-run 预览清单

```bash
python3 sync.py --url "<站点入口URL>" \
  --target-space-id <乐享space_id> --target-folder-id <目标目录entry_id> \
  --scope section --dry-run
```

输出待导入的目录/页面计数与清单，确认范围后再正式执行。

### 3. 正式同步

```bash
python3 sync.py --url "<站点入口URL>" \
  --target-space-id <乐享space_id> --target-folder-id <目标目录entry_id> \
  --scope section --delete-mode sync --lexiang-profile default
```

页面多时可能需要 5-20 分钟，建议后台执行。同步状态存于 `~/.config/lexiang-websync/<域名>/`，源站更新后重跑同一命令即增量同步。

## 主要参数

| 参数 | 说明 |
|------|------|
| `--url` | 站点入口 URL（必填） |
| `--target-space-id` | 乐享空间 ID（必填，从链接 `/spaces/{id}` 提取） |
| `--target-folder-id` | 目标目录 entry ID（从链接 `/pages/{id}` 提取） |
| `--scope` | `section`（默认，入口所在顶级分区）/ `subtree`（入口节点子树）/ `all`（全部分区） |
| `--delete-mode` | `sync`（默认，源删→乐享回收站）/ `keep`（只增改不删） |
| `--lexiang-profile` | 乐享凭证 profile 名，位于 `~/.config/lexiang-upload/profiles/<name>.json` |
| `--lexiang-credential-file` | 直接指定凭证文件路径（与 `--lexiang-profile` 互斥） |
| `--dry-run` | 只预览清单不写入 |
| `--probe-only` | 只探测站点结构 |
| `--max-pages` | 单次最大页面数（安全阀） |
| `--force-delete` | 消失比例 >30% 时仍继续删除同步 |
| `--no-adopt-orphan` | 关闭 sidebar 隐藏页的兜底归组（默认开启） |
| `--no-waf-sanitize` | 关闭 WAF 内容消毒 |

## 工作机制

```
探测站点 → dry-run 确认 → 逐页抓取转换（HTML→Markdown→乐享 blocks）
  → 媒体下载内嵌 → sidebar 分组目录重排 → 删除同步 → manifest 落盘
```

- 每页以源路径为唯一标识，内容 hash 判断增/改/跳过
- sidebar 分组标题只存在于源站 HTML（菜单 JSON 是平铺的），脚本双数据源交叉解析后用 `move_entry` 还原分组与顺序
- 菜单里有但 sidebar 不渲染的隐藏页，按路径前缀归入相邻分组（可用 `--no-adopt-orphan` 关闭）

## 已知限制

- 只支持**公开**文档站点；需要登录的站点不支持
- `generic` 类型站点（无已适配框架）菜单解析可能为空，需参考 `references/site-adapters.md` 新增适配器
- 删除同步只能移入乐享回收站文件夹（乐享 OpenAPI 不提供真删除）
- 源站本身 404 的图片自动回退为绝对 URL 链接

## 相关 Skill

同家族（源 → 乐享单向同步，零 token 架构）：

- [`sync-obsidian-to-lexiang`](https://github.com/ajaxhe/sync-obsidian-to-lexiang)：Obsidian vault → 乐享
- `sync-feishu-to-lexiang`：飞书在线文档/知识库 → 乐享

## License

[MIT](./LICENSE)
