---
name: ima-to-obsidian
description: 拉取IMA 知识库的条目到 Obsidian vault 落盘成 markdown 笔记，自动写入可点击的 IMA 回链，按内容指纹增量去重，敏感凭证自动阻断。落点固定为 01-Raw/1-我的文章/1-1来自IMA。触发词：拉取IMA、同步IMA到obsidian、把IMA知识库拉回obsidian、IMA笔记回写本地、从IMA同步到本地、从云端拉回笔记。
---

# IMA → Obsidian 反向同步

**拉取IMA**的文件统一保存到 `01-Raw/1-我的文章/1-1来自IMA`，并按**年份周次 / 年 / 月 / 日+星期**三层归档。

落点示例（2026-10-30 = 2026 年第 44 周的周五）：

```
01-Raw/1-我的文章/1-1来自IMA/
├── 2026年44周/
│   └── 2026/
│       ├── 10/
│       │   ├── 26周一/
│       │   ├── 27周二/
│       │   ├── 28周三/
│       │   ├── 29周四/
│       │   ├── 30周五/     ← 本次落盘在这里
│       │   └── 31周六/
│       └── 11/
│           └── 01周日/
└── 2026年45周/
    └── 2026/
        └── 11/
            ├── 02周一/
            └── … 08周日/
```

- 一周从**周一**开始、周日结束；周次用 **ISO 8601**（含当年第一个周四的那一周为第 1 周）
- 2026-10-30（周五）属 **2026年44周**（10/26 ~ 11/01）—— 与 IMA 知识库手建的「2026年第44周」口径一致
- 每次落盘前**自动建出该周完整 7 天目录**（跨月自动分到不同月目录）
- **落点镜像 IMA 云端文件夹路径**（不是按同步当天算）：
  - 云端 `2026年第44周/2026-10-30/` → 本地 `2026年44周/2026/10/30周五/`
  - 云端 `2026年第45周/`（无日期层）→ 本地 `2026年45周/` + 该周**周一** `2026/11/02周一/`
  - 云端条目挪到别的周/日期，本地**跟着迁移**，不留在旧位置
  - items.json 需带 `cloud_folder` 字段（拉取时记录云端文件夹路径）；缺失时退回 `sync_date`（默认今天）

把 IMA 知识库里的条目拉取到 Obsidian vault，每条落成一个 markdown 笔记，并在 frontmatter 里写入**可点击的 IMA 回链**，实现「本地笔记 ↔ 云端条目」互指。

> 与正向技能 `obsidian-ima-sync`（Obsidian → IMA 推送）方向相反，两者可共存。

## 触发词

- **拉取IMA**
- 同步IMA到obsidian
- 把IMA知识库拉回obsidian
- IMA笔记回写本地
- 从IMA同步到本地
- 从云端拉回笔记

## 硬规则

| 编号 | 规则 |
|---|---|
| R1 | **只拉取指定 IMA 知识库**的条目，不遍历账号下所有库 |
| R2 | **增量判断**：内容指纹与本地 `ima_sync_hash` 一致则跳过；不一致则**原地更新**已有文件，不产生重复文件。匹配键用 media_id 的**尾段**（前缀会变） |
| R3 | **敏感拦截**：命中密码/密钥/token/私钥/凭证 → 阻断该条，**不落盘到本地 vault** |
| R4 | **回链**：frontmatter 写 `ima_media_id` / `ima_kb_id` / `ima_url`，正文顶部给出「在 IMA 中查看」链接 |
| R5 | **落点**：`01-Raw/1-我的文章/1-1来自IMA/{年}年{周}周/{年}/{月}/{日+星期}/`（例 `2026年44周/2026/10/30周五/`）。周次用 **ISO 8601**；落点**镜像 IMA 云端文件夹路径**，落盘前自动建出该周 7 天目录 |
| R6 | **禁止行为**：不删除本地文件、不改写不带 `ima_media_id` 的笔记、不做双向覆盖 |
| R7 | **云端删除的条目不删本地**，改为加**前缀横线 `— `**（文件名 / 正文 H1 / frontmatter title 三处都加），并写 frontmatter `ima_cloud_status: 云端已删除`；云端重现时自动撤销（含文件名还原） |

## 关键事实

- **IMA 回链格式**：`https://ima.qq.com/wikis/?knowledgeBaseId=<kb_id>&mediaId=<media_id>`
  （`/wikis` 是 IMA 知识库页路由，实测 200 且 title 为「腾讯ima - 知识库」）
- **落点目录**：`D:/OB/01-Raw/1-我的文章/1-1来自IMA/{年}年{周}周/{年}/{月}/{日+星期}/`（用户 2026-10-31 指定三层归档）
  - 例：`1-1来自IMA/2026年44周/2026/10/30周五/`
  - 一周从**周一**开始、周日结束；周次用 **ISO 8601**（含当年第一个周四的那一周为第 1 周）
- 2026-10-30（周五）属 **2026年44周**（10/26 ~ 11/01）—— 与 IMA 知识库手建的「2026年第44周」口径一致
- 跨年周归 **ISO 年**（含周四的年份），不出现「0周」
  - 每次落盘前自动建出该周**完整 7 天**目录，跨月时分到不同月目录（如 `2026/10/31周六` 与 `2026/11/01周日`）
  - 落点**镜像云端文件夹**：`cloud_folder` = `/2026年第44周/2026-10-30` → `2026年44周/2026/10/30周五/`；只有周目录时落到该周周一
  - 注意：`01-Raw/外部剪藏/` 是**其他来源**的外部剪藏（网页剪藏、公开课转写），**不是** IMA 同步落点
- **vault 有 GitHub 远端**（`obsidian-efood.git`）—— 敏感拦截不是摆设，漏网的凭证会被提交到仓库
- **IMA 正文可能为空**：某些 NOTE 类型条目 `fetch_media_content` 返回空，但 `introduction` 有摘要。此时用摘要兜底，并标注来源
- **markdown 类条目取不到正文 —— 是「解析未完成」，不是接口限制**（2026-10-30 22:52 更正早前判断）
  - 早期记录称 `fetch_media_content` 对 `media_type=MD` 恒返回 `code 220030`，是**误判**。真实原因：当时条目的后台解析还没跑完
  - 实测：等解析完成（`parse_progress=100`）后，**用上传时返回的旧 media_id 也能取到全文**
  - 正确做法：遇到取不到正文时，先看 `parse_progress`；未到 100 说明解析未完成，稍后重试即可，不要当成永久限制
  - 若确实只能拿到 `introduction`，那是**截断的摘要**（实测 516 字节正文只剩 290 字节、753 字节只剩 293 字节，末尾被切断）。落盘须标 `partial: true` + frontmatter `ima_content_full: false` + 正文顶部 `> [!warning] 正文不完整`
- **media_id 前缀会变，增量匹配必须用尾段**（重要）
  - media_id 形如 `<type>_<prefix32>_<tail40>`。上传时 `create_media` 返回的前缀，与之后 `get_knowledge_list` 返回的**不同**（例：上传拿到 `markdown_dade8a5b...`，列表查询拿到 `markdown_0ed7901f...`）
  - 尾段（第二段 hash）在同一账号下稳定 → 引擎用 `media_key()` 取 `<type>_<tail>` 做匹配键
  - 若按完整 media_id 匹配，云端换前缀就会把旧条目误判成「新条目」→ **重复落盘**
  - **回链 URL 用列表查询拿到的 media_id**（那个能正常打开）
- **云端删除的条目：不删本地，改打横线标记**（守硬规则 R6，用户 2026-10-30 23:05 明确要求）
  - plan 会输出 `orphans` 列表：本地有 `ima_media_id`、但本次云端清单里没有的条目
  - apply 阶段对每条 orphan **原地打标记**（幂等，重复跑不会叠加横线），**三处都加**：
    - **文件名**：`2026-10-30的日历.md` → `— 2026-10-30的日历.md`
    - **正文 H1**：`# 2026-10-30的日历` → `# — 2026-10-30的日历`
    - **frontmatter title**：`title: — 2026-10-30的日历`
    - frontmatter 追加 `ima_cloud_status: 云端已删除` 与 `ima_orphan_marked_at`
    - 来源块下方追加一行 `> ⚠️ 云端已删除，本地保留（该条目已从 IMA 知识库移除，按规则不自动删除本地文件）`
  - **为什么三处都要加**：Obsidian 默认在正文顶部显示「来自文件名的标题」（inline title），用户第一眼看到的是**文件名**，只在 H1 加横线会「看不见标志线」。文件名加前缀 → 文件列表 / 标签面板 / 大纲面板都能立刻分辨
  - **改名的影响**：wiki 链接会随之失效，索引页表格要同步改成 `[[— 标题]]`
  - 云端条目**重新出现**时，`apply` 会自动撤掉标记（去掉三处横线、删状态字段、**文件名还原**）
- **云端只返回摘要时不要降级覆盖本地完整正文**
  - 引擎会比对摘要与本地正文（忽略标点与空白差异）；若摘要内容已被本地涵盖 → 跳过保留本地
  - 只有摘要里有本地没有的新内容，才用摘要更新
- **音频类条目（`media_type=15`，`soundrecording_` 前缀）正文可正常获取**，取到的是**完整转写稿**（实测 2766 字 / 22 段，远超 `introduction` 里那段摘要）
  - `introduction` 对音频条目只是开头几百字的摘要，**不能当正文用** —— 必须调 `fetch_media_content` 取全文
  - 转写稿是口语连读、无标点结构，段落边界由 `\n\n` 分隔；直接原样落盘即可，不要擅自改写
  - `type_label` 取 `media_type_info.name`（音频 / 笔记 / MD / PDF 等），原样写进 frontmatter 的「类型」字段

## 执行流程

### 第 1 步：定位知识库

用户没指定库名时，用 `mcp__ima-mcp__search_knowledge_base`（参数 `query` + `limit`）搜库；
指定了就直接用。需要区分同名不同 ID 的库（账号下有 `Obsidian-codex` 与 `obsidian` 两个）。

### 第 2 步：拉取条目清单（**必须递归**）

```
mcp__ima-mcp__get_knowledge_list
  knowledge_base_id=<kb_id>  limit=50  sort_type=UPDATE_TS_DESC_SORT_TYPE
```

- **云端有文件夹层级**（`2026年第44周/2026-10-30/`），不递归只列根目录会漏掉全部条目
- 根列表返回的 `media_type=99` 是**文件夹**，取 `folder_info.folder_id` 带着 `folder_id` 参数再查一层，直到没有子文件夹
- 每层都要**记录路径**（如 `/2026年第44周/2026-10-30`），后面写进 items.json 的 `cloud_folder`，落点据此镜像
- `limit` 上限是 **50**，传 100 会直接报错
- 文件夹本身不算条目，不落盘

#### 备用通道：MCP 工具没进索引时

若本会话里 `mcp__ima-mcp__*` 工具不可见（检索不到），改用直连脚本：

```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
S="C:/Users/Administrator/.workbuddy/skills/ima-to-obsidian/scripts/ima_mcp_direct.py"

"$PY" "$S" list                          # 列出可用工具（确认连接正常）
"$PY" "$S" tree ima_kb_sKpjKAEk          # 递归打印云端文件夹树
"$PY" "$S" fetch ima_kb_sKpjKAEk items.json   # 拉全部条目+正文 → items.json
```

它从 WorkBuddy 本地连接器凭证库解密 `ima-mcp` 的 Authorization 头，直连
`https://ima.qq.com/mcp`。需要 `pip install cryptography`。
`fetch` 产出的 items.json **已带 `cloud_folder`**，可直接喂给 plan。

### 第 3 步：拉取正文

对每条需要落盘的条目调用 `mcp__ima-mcp__fetch_media_content`（参数 `media_id`）。

- 用**列表查询拿到的 media_id**（上传时返回的那个 ID 也能用，但优先用列表的）
- 正文为空时用条目 `introduction` 兜底，并标 `partial: true`
- 取不到正文先看 `parse_progress`：未到 100 是**解析未完成**，稍后重试，不要当成接口限制
- 音频类必须取全文，`introduction` 只是开头摘要

### 第 4 步：组装 items.json

```json
{
  "knowledge_base_id": "ima_kb_xxx",
  "knowledge_base_name": "库名",
  "vault_path": "D:/OB",
  "sync_date": "2026-10-30",
  "items": [
    {
      "media_id": "note_xxx",
      "title": "条目标题",
      "type_label": "IMA笔记",
      "created": "2026-10-30",
      "cloud_folder": "/2026年第44周/2026-10-30",
      "tags": [],
      "content": "正文全文",
      "partial": false
    }
  ]
}
```

`cloud_folder` 是该条目在 IMA 云端的**文件夹路径**（逐层递归列表时记录），决定落点。
有它就镜像云端结构（周次用 ISO 8601 换算）；没有它则退回 `sync_date`（默认今天）统一落点。
`sync_date` 只在没有 `cloud_folder` 时起作用。
`type_label` 取 IMA 的 `media_type_info.name`（音频 / 笔记 / MD / PDF / 图片 等）。
`partial` 标 true 表示正文没取全、只用了截断的摘要。
`media_id` 用**列表查询返回的那个**（前缀会变，但尾段稳定，引擎靠尾段匹配）。

### 第 5 步：生成计划

```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
"$PY" "C:/Users/Administrator/.workbuddy/skills/ima-to-obsidian/scripts/ima_to_obsidian.py" plan \
  --items ima_items.json --out ima_plan.json
```

输出 `summary`：`write` / `skip` / `block`。**只读 vault，不写文件**，可安全先跑。

### 第 6 步：落盘

```bash
"$PY" ".../ima_to_obsidian.py" apply --plan ima_plan.json --out ima_apply.json
```

可选 `--out-dir` 指定其他落点目录（默认 `01-Raw/1-我的文章/1-1来自IMA`）。

## 落盘格式

```markdown
---
title: 今天晚上吃大餐
类型: IMA笔记
来源: IMA 知识库 · Obsidian-codex
ima_media_id: note_0ed7901fc35d9be985815c7f7e9ef929_75218856255442417519189443038808
ima_kb_id: ima_kb_sKpjKAEk
ima_kb_name: Obsidian-codex
ima_sync_hash: 4506fd6cf454d496ca0d6808087cbdec
last_sync_time: "2026-10-30 19:12:46"
创建: 2026-10-30
ima_url: "https://ima.qq.com/wikis/?knowledgeBaseId=ima_kb_sKpjKAEk&mediaId=note_..."
标签:
  - IMA同步
---

# 今天晚上吃大餐

> [!info] IMA 来源
> 知识库：Obsidian-codex
> 在 IMA 中查看：https://ima.qq.com/wikis/?knowledgeBaseId=...
> 同步时间：2026-10-30 19:12:46

---

接入obsidian
```

## 几个容易踩的点（已修，别回退）

1. **文件名双后缀**：IMA 的 markdown 类条目标题自带 `.md`，落盘前必须去掉，否则出现 `xxx.md.md`
2. **标题重复**：IMA 正文常以 `# 标题` 开头，若再渲染一个 H1 会产生两个同名标题（Obsidian 大纲显示重复）。渲染时剔除正文开头与标题重复的那行
3. **hash 与渲染口径必须一致**：内容指纹和渲染都基于「剔除重复 H1 后的正文」，否则每次同步都误判「有更新」而重写
4. **更新必须原地写**：已有本地副本时按 `ima_media_id` 找回原路径覆盖，绝不能新建文件（否则每次同步多一份）
5. **文件名末尾标点**：长标题截断后可能残留 `。`、`、`，需 strip 掉

## 敏感词库

**高置信凭据**：私钥块、AWS Access Key、GitHub Token、GitHub PAT、Anthropic API Key、OpenAI API Key、Slack Token、Google API Key、Google OAuth、JWT、Stripe Key、腾讯云 SecretId。

**赋值型敏感字段**（key + 赋值符 + 6 位以上值）：中英文均识别，类别形如 `疑似password`、`疑似api_key`。

**误报豁免**：占位符（`xxx`、`your_key_here`、`changeme`、`<...>`、`${VAR}`）不触发；打码值（`sk-****`）不触发；技术参数名（`max_tokens`、`token_count`、`tokenizer` 等）列入白名单。

命中即整条阻断，不落盘，告警给出类别与脱敏证据。

## 返回格式

```
同步完成，清单：落盘×条，跳过×条，敏感阻断×条，云端已删保留×条
```

附每条明细：落盘项列出本地路径与 mode（create/update）；跳过项列出标题与原因；阻断项列出标题、类别与证据；云端已删项列出打上横线标记的文件。
