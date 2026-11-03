#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
IMA -> Obsidian 反向同步引擎

把 IMA 知识库里的条目拉取到 Obsidian vault 落盘成 markdown 笔记，
并在 frontmatter 里写回可点击的 IMA 回链，实现「本地笔记 ↔ 云端条目」互指。

落点：拉取IMA的文件统一保存到
      01-Raw/1-我的文章/1-1来自IMA/{年}年{周}周/{年}/{月}/{日+星期}/
      例：01-Raw/1-我的文章/1-1来自IMA/2026年44周/2026/10/30周五/

      周次用 ISO 8601（周一为一周之始）；
      落点镜像 IMA 云端文件夹路径（items.json 的 cloud_folder 字段）。

子命令：
  plan     读取 IMA 条目清单 + 本地已同步状态，产出待落盘计划（只读，不写 vault）
  apply    按 plan 结果把内容写入 vault
  sync     plan + apply 一体（供 agent 一条命令跑完）

IMA 读取靠 MCP 工具（get_knowledge_list / fetch_media_content），
本脚本负责：本地状态管理、增量判定、敏感拦截、落盘排版、回链生成。

用法（agent 调用）：
  python ima_to_obsidian.py plan  --vault-json <items.json> --out <plan.json>
  python ima_to_obsidian.py apply --plan <plan.json>
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta, date

CST = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 敏感词库
# 与正向同步保持一致：命中即阻断，不落盘到本地 vault
HIGH_CONFIDENCE_PATTERNS = [
    ("私钥块", re.compile(r"-----BEGIN(?: [A-Z]+)* PRIVATE KEY-----")),
    ("AWS Access Key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("Anthropic API Key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("OpenAI API Key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{32,}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("Google OAuth", re.compile(r"\bya29\.[A-Za-z0-9\-_]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("Stripe Key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("腾讯云 SecretId", re.compile(r"\bAKID[A-Za-z0-9]{13,}\b")),
]

_EN_SENSITIVE_KEYS = (
    r"password|passwd|pass_?word|pwd|secret|secret_?key|api_?key|apikey|"
    r"access_?token|refresh_?token|auth_?token|private_?key|"
    r"client_?secret|app_?secret|app_?key|secret_?id|credential|credentials|"
    r"session_?key|signing_?key|encryption_?key|access_?key|secret_?access_?key"
)
_KEY_EXEMPT = (
    r"max_tokens|min_tokens|token_count|tokens|prompt_tokens|completion_tokens|"
    r"total_tokens|budget_tokens|n_tokens|reasoning_tokens|cached_tokens|"
    r"tokenizer|tokenize|token_usage|token_limit|token_budget"
)
_EN_ASSIGN_PAT = re.compile(
    r"(?<![A-Za-z0-9_])(" + _KEY_EXEMPT + r")(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])(" + _EN_SENSITIVE_KEYS + r")(?![A-Za-z0-9_])"
    r"\s*[:=：]\s*[\"'`]?([^\s\"'`，。；;]{6,})",
    re.IGNORECASE,
)
_CN_ASSIGN_PAT = re.compile(
    r"(密码|口令|密钥|秘钥|私钥|凭证|凭据|令牌|访问令牌|授权码)\s*[:=：]\s*[\"'`]?([^\s\"'`，。；;]{6,})"
)
PLACEHOLDER_PATTERNS = [
    re.compile(r"^(?:x{3,}|\*{3,}|\.{3,}|_{3,}|-{3,})$", re.IGNORECASE),
    re.compile(r"^(?:your|my|the|some)?[-_ ]?(?:password|secret|key|token|value|here|id)", re.IGNORECASE),
    re.compile(r"(?:placeholder|example|changeme|change_me|todo|tbd|none|null|n/?a|xxx+|示例|占位|待填|请填写|你的|你的xxx)", re.IGNORECASE),
    re.compile(r"^[<\[{].*[>\]}]$"),
    re.compile(r"^\$[A-Za-z_{]"),
    re.compile(r"^\{[^}]*\}$"),
]
MASKED_PAT = re.compile(r"^[A-Za-z0-9\-_*]{0,4}[*＊•]{2,}[A-Za-z0-9\-_*]*$")


def _is_placeholder(value: str) -> bool:
    v = (value or "").strip().rstrip(".,;，。；")
    if not v:
        return True
    if MASKED_PAT.match(v):
        return True
    return any(p.search(v) for p in PLACEHOLDER_PATTERNS)


def scan_sensitive(text: str):
    """返回 [(类别, 脱敏证据)]，命中即阻断落盘。"""
    hits = []
    for name, pat in HIGH_CONFIDENCE_PATTERNS:
        m = pat.search(text)
        if m:
            val = m.group(0)
            masked = val[:6] + "***" + val[-2:] if len(val) > 10 else "***"
            hits.append((name, masked))
    for m in _EN_ASSIGN_PAT.finditer(text):
        if m.group(1):
            continue
        key, val = m.group(2), m.group(3)
        if _is_placeholder(val):
            continue
        hits.append(("疑似%s" % key, key + "=" + val[:3] + "***"))
    for m in _CN_ASSIGN_PAT.finditer(text):
        key, val = m.group(1), m.group(2)
        if _is_placeholder(val):
            continue
        hits.append(("疑似%s" % key, key + "=" + val[:3] + "***"))
    seen, uniq = set(), []
    for cat, ev in hits:
        if cat in seen:
            continue
        seen.add(cat)
        uniq.append((cat, ev))
    return uniq


# ---------------------------------------------------------------- 文件名与 frontmatter

FM_HEAD = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", re.DOTALL)

# Windows 非法文件名字符 + Obsidian 链接分隔符
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Obsidian wiki link 不应出现的符号
_LINK_UNSAFE = re.compile(r'[\[\]#^|]')


def safe_filename(title: str, max_len: int = 60) -> str:
    """把 IMA 标题转成合法且 Obsidian 友好的文件名。"""
    t = (title or "").strip()
    t = t.replace("/", "-").replace("\\", "-")
    t = _ILLEGAL.sub("", t)
    t = _LINK_UNSAFE.sub("", t)
    # IMA 的 markdown 类条目标题自带 .md 后缀，落盘时不要出现 xxx.md.md
    t = re.sub(r"\.md$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    # 去掉末尾标点，避免出现「...。」这类尾巴
    t = re.sub(r"[。，、；：!！?？,.;:\s]+$", "", t)
    if len(t) > max_len:
        t = t[:max_len]
        t = re.sub(r"[。，、；：!！?？,.;:\s]+$", "", t)
    return t or "未命名"


def split_frontmatter(text: str):
    m = FM_HEAD.match(text)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


def parse_frontmatter(fm_raw: str) -> dict:
    """轻量解析顶层键值，tags 为 list。"""
    data = {}
    if not fm_raw:
        return data
    lines = fm_raw.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_\-\.]*)\s*:\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).strip()
        if val == "":
            items = []
            j = i + 1
            while j < len(lines):
                im = re.match(r"^\s+-\s+(.*)$", lines[j])
                if not im:
                    break
                items.append(im.group(1).strip().strip("\"'"))
                j += 1
            data[key] = items if items else None
            i = j if items else i + 1
            continue
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            data[key] = [x.strip().strip("\"'") for x in inner.split(",")] if inner else []
        elif len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            data[key] = val[1:-1]
        else:
            data[key] = val
        i += 1
    return data


def yaml_escape(v) -> str:
    s = "" if v is None else str(v)
    if s == "":
        return '""'
    if re.search(r'[:#\[\]{}"\']', s) or s.strip() != s:
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


# ---------------------------------------------------------------- 本地状态扫描


def iter_md_files(root: str):
    skip = {".obsidian", ".git", ".workbuddy", ".trash", "node_modules", ".unused", ".codex-artifacts"}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in skip]
        for f in fn:
            if f.lower().endswith(".md"):
                yield os.path.join(dp, f)


def scan_local(vault_path: str):
    """扫描 vault 里所有已带 ima_media_id 的笔记，建立 media_key -> 记录 的索引。

    键用 media_key()（尾段稳定），不用完整 media_id —— 云端可能换前缀。
    """
    index = {}
    for path in iter_md_files(vault_path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            continue
        fm_raw, body = split_frontmatter(text)
        if not fm_raw:
            continue
        fm = parse_frontmatter(fm_raw)
        mid = fm.get("ima_media_id")
        if not mid:
            continue
        key = media_key(mid)
        index[key] = {
            "path": path,
            "rel_path": os.path.relpath(path, vault_path).replace("\\", "/"),
            "ima_media_id": str(mid),
            "ima_sync_hash": fm.get("ima_sync_hash"),
            "ima_doc_id": fm.get("ima_doc_id"),
            "last_sync_time": fm.get("last_sync_time"),
            "title": fm.get("title"),
            # 本地这版正文是否完整（false = 只有摘要）
            "ima_content_full": str(fm.get("ima_content_full", "true")).strip().lower() != "false",
            # 云端是否已删除（本地保留并打了横线标记）
            "ima_cloud_status": str(fm.get("ima_cloud_status") or "").strip(),
        }
    return index


def read_text(path: str) -> str:
    """安全读文本文件，失败返回空串。"""
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def read_local_body(path: str) -> str:
    """读本地笔记的正文部分（去掉 frontmatter 和渲染出的 H1 / 来源提示块）。"""
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return ""
    _, body = split_frontmatter(text)
    # 去掉渲染出的 H1 与 "> [!info] IMA 来源" 提示块
    lines = body.split("\n")
    out = []
    in_info = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("# ") and not out:
            continue
        if s.startswith("> [!info]"):
            in_info = True
            continue
        if in_info:
            if s.startswith(">"):
                continue
            in_info = False
        if s == "---":
            continue
        out.append(ln)
    return "\n".join(out).strip()


def content_hash(title: str, content: str) -> str:
    """内容指纹。

    与渲染口径保持一致（同样剔除开头重复的 H1），否则「内容未变」也会误判成
    「有更新」，导致每次同步都重写一遍。
    """
    body = strip_leading_title(content or "", title or "")
    norm = (title or "").strip() + "\n" + body.replace("\r\n", "\n").rstrip() + "\n"
    return hashlib.md5(norm.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- IMA 回链

IMA_WEB = "https://ima.qq.com/wikis/"


def media_key(media_id: str) -> str:
    """提取 media_id 的稳定匹配键。

    IMA 的 media_id 形如 `<type>_<prefix32>_<tail40>`：
      - 上传时 create_media 返回的 prefix 与之后 get_knowledge_list 返回的 **不同**
        （例：上传拿到 `markdown_dade8a5b...`，列表查询拿到 `markdown_0ed7901f...`）
      - 尾段（第二段 hash）在同一账号下稳定不变

    所以增量匹配不能用完整 media_id，否则云端换了前缀就会把旧条目误判成
    「新条目」→ 重复落盘。这里统一用 `<type>_<tail>` 做键。
    """
    mid = str(media_id or "").strip()
    if not mid:
        return ""
    parts = mid.split("_")
    if len(parts) >= 3:
        return "%s_%s" % (parts[0], parts[-1])
    return mid


def build_kb_link(kb_id: str) -> str:
    return "%s?knowledgeBaseId=%s" % (IMA_WEB, kb_id)


def build_item_link(kb_id: str, media_id: str) -> str:
    return "%s?knowledgeBaseId=%s&mediaId=%s" % (IMA_WEB, kb_id, media_id)


# ---------------------------------------------------------------- 孤儿标记（云端已删除）

# 云端条目被移除、本地文件仍保留时，给标题加的前缀横线。
ORPHAN_PREFIX = "— "
ORPHAN_STATUS = "云端已删除"
ORPHAN_NOTE = "> ⚠️ 云端已删除，本地保留（该条目已从 IMA 知识库移除，按规则不自动删除本地文件）"


def set_frontmatter_field(fm_raw: str, key: str, value: str) -> str:
    """在 frontmatter 文本中设置（或更新）一个顶层字段，其余行原样保留。"""
    lines = fm_raw.replace("\r\n", "\n").split("\n")
    pat = re.compile(r"^([A-Za-z_][A-Za-z0-9_\-\.]*)\s*:")
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m and m.group(1) == key:
            lines[i] = "%s: %s" % (key, yaml_escape(value))
            return "\n".join(lines)
    lines.append("%s: %s" % (key, yaml_escape(value)))
    return "\n".join(lines)


def orphan_file_name(title: str) -> str:
    """云端已删除笔记的显示名（文件名 + 前缀横线）。"""
    t = (title or "").strip()
    if t.startswith(ORPHAN_PREFIX):
        return t
    return ORPHAN_PREFIX + t


def rename_orphan_file(path: str, marked: bool):
    """重命名文件以带上/摘掉前缀横线。

    marked=True  → 「2026-10-30的日历.md」→「— 2026-10-30的日历.md」
    marked=False → 反向摘掉
    返回新路径（未发生变化时返回原路径）
    """
    d = os.path.dirname(path)
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    if marked:
        if stem.startswith(ORPHAN_PREFIX):
            return path
        new_stem = ORPHAN_PREFIX + stem
    else:
        if not stem.startswith(ORPHAN_PREFIX):
            return path
        new_stem = stem[len(ORPHAN_PREFIX):].lstrip()
        if not new_stem:
            return path
    target = os.path.join(d, new_stem + ext)
    if os.path.exists(target):
        return path  # 目标已存在，不覆盖
    try:
        os.rename(path, target)
    except Exception:
        return path
    return target


def mark_orphan(path: str, ts: str):
    """给「云端已删除、本地保留」的笔记打横线标记。

    - 文件名加前缀横线：2026-10-30的日历.md → — 2026-10-30的日历.md
    - frontmatter title 加前缀横线（Obsidian 属性面板可见）
    - 正文 H1 加前缀横线：# 2026-10-30的日历  →  # — 2026-10-30的日历
    - frontmatter 写入 ima_cloud_status / ima_orphan_marked_at
    - IMA 来源块追加一行状态提示

    幂等：已打过标记的文件再跑不会重复加横线。
    返回 (changed, new_path)
    """
    if not path or not os.path.exists(path):
        return False, path
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return False, path

    fm_raw, body = split_frontmatter(text)
    if not fm_raw:
        return False, path

    fm = parse_frontmatter(fm_raw)
    already_status = str(fm.get("ima_cloud_status") or "").strip() == ORPHAN_STATUS

    # 1) frontmatter title 加前缀
    raw_title = str(fm.get("title") or "").strip()
    if raw_title and not raw_title.startswith(ORPHAN_PREFIX):
        fm_raw = set_frontmatter_field(fm_raw, "title", ORPHAN_PREFIX + raw_title)

    lines = body.split("\n")
    # 2) 正文 H1 加前缀横线（幂等）
    title_touched = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("# "):
            t = s[2:].strip()
            if not t.startswith(ORPHAN_PREFIX):
                lines[i] = "# %s%s" % (ORPHAN_PREFIX, t)
                title_touched = True
        break
    body = "\n".join(lines)

    # 3) 来源块追加状态提示行（幂等）
    if ORPHAN_NOTE not in body:
        for i, ln in enumerate(body.split("\n")):
            if ln.strip().startswith("> 同步时间"):
                bl = body.split("\n")
                bl.insert(i + 1, "")
                bl.insert(i + 2, ORPHAN_NOTE)
                body = "\n".join(bl)
                break
        else:
            body = body.rstrip("\n") + "\n\n" + ORPHAN_NOTE + "\n"

    # 4) frontmatter 写状态字段
    fm_raw = set_frontmatter_field(fm_raw, "ima_cloud_status", ORPHAN_STATUS)
    fm_raw = set_frontmatter_field(fm_raw, "ima_orphan_marked_at", ts)

    new_text = "---\n" + fm_raw + "\n---\n\n" + body.lstrip("\n")
    changed = new_text != text
    if changed:
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_text)
        except Exception:
            return False, path

    # 5) 文件名加前缀横线
    new_path = rename_orphan_file(path, True)
    return changed or (new_path != path), new_path


def unmark_orphan(path: str):
    """云端条目重新出现时，撤掉本地的「云端已删除」标记。

    幂等：没有标记的文件直接返回 (False, path)。
    返回 (changed, new_path)
    """
    if not path or not os.path.exists(path):
        return False, path
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return False, path

    fm_raw, body = split_frontmatter(text)
    if not fm_raw:
        return False, path
    fm = parse_frontmatter(fm_raw)
    if str(fm.get("ima_cloud_status") or "").strip() != ORPHAN_STATUS:
        return False, path

    # 1) frontmatter title 去前缀
    raw_title = str(fm.get("title") or "").strip()
    if raw_title.startswith(ORPHAN_PREFIX):
        fm_raw = set_frontmatter_field(fm_raw, "title", raw_title[len(ORPHAN_PREFIX):].lstrip())

    # 2) 去掉正文标题前缀横线
    lines = body.split("\n")
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("# "):
            t = s[2:].strip()
            if t.startswith(ORPHAN_PREFIX):
                lines[i] = "# %s" % t[len(ORPHAN_PREFIX):].strip()
        break
    body = "\n".join(lines)

    # 3) 去掉状态提示行与其后的空行
    bl = body.split("\n")
    bl = [x for x in bl if x.strip() != ORPHAN_NOTE]
    body = "\n".join(bl).replace("\n\n\n", "\n\n")

    # 4) frontmatter 移除状态字段
    keep = []
    for ln in fm_raw.replace("\r\n", "\n").split("\n"):
        if re.match(r"^(ima_cloud_status|ima_orphan_marked_at)\s*:", ln):
            continue
        keep.append(ln)
    fm_raw = "\n".join(keep).rstrip("\n")

    new_text = "---\n" + fm_raw + "\n---\n\n" + body.lstrip("\n")
    changed = new_text != text
    if changed:
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_text)
        except Exception:
            return False, path

    # 5) 文件名摘掉前缀横线
    new_path = rename_orphan_file(path, False)
    return changed or (new_path != path), new_path


# ---------------------------------------------------------------- 落盘渲染

def strip_leading_title(content: str, title: str) -> str:
    """去掉正文开头与标题重复的一级标题行。

    IMA 笔记正文常以「# 标题」开头，落盘时若再渲染一个 H1 会出现两个同名标题，
    Obsidian 大纲视图会显示重复条目。这里保留渲染出的 H1，删掉正文里的那份。
    """
    lines = content.split("\n")
    t = (title or "").strip()
    # 找到首个非空行
    idx = None
    for i, ln in enumerate(lines):
        if ln.strip():
            idx = i
            break
    if idx is None:
        return content
    head = lines[idx].strip()
    if not head.startswith("#"):
        return content
    head_text = head.lstrip("#").strip()
    # 空标题（如 IMA 笔记正文首行是「# 」）—— 直接去掉，渲染侧已有 H1
    if not head_text:
        return "\n".join(lines[idx + 1:]).lstrip("\n")
    # 标题一致（忽略 .md 后缀与全角标点差异）才算重复
    norm_a = re.sub(r"[\s。．.、,，!！?？:：]+$", "", head_text.replace(".md", ""))
    norm_b = re.sub(r"[\s。．.、,，!！?？:：]+$", "", t.replace(".md", ""))
    if norm_a != norm_b:
        return content
    return "\n".join(lines[idx + 1:]).lstrip("\n")


def render_note(item: dict, local: dict | None) -> str:
    """渲染写入 vault 的 markdown 笔记。"""
    title = item.get("title") or "未命名"
    # IMA 的 markdown 类条目标题自带 .md 后缀，展示时去掉，与文件名保持一致
    display_title = re.sub(r"\.md$", "", title, flags=re.IGNORECASE)
    content = item.get("content") or ""
    kb_id = item.get("knowledge_base_id", "")
    kb_name = item.get("knowledge_base_name", "")
    media_id = item.get("media_id", "")
    md5 = item["content_hash"]
    ts = item["sync_time"]

    fm = []
    fm.append("---")
    fm.append("title: %s" % yaml_escape(display_title))
    fm.append("类型: %s" % yaml_escape(item.get("type_label", "IMA同步")))
    fm.append("来源: %s" % yaml_escape("IMA 知识库 · %s" % kb_name if kb_name else "IMA 知识库"))
    fm.append("ima_media_id: %s" % yaml_escape(media_id))
    fm.append("ima_kb_id: %s" % yaml_escape(kb_id))
    fm.append("ima_kb_name: %s" % yaml_escape(kb_name))
    fm.append("ima_sync_hash: %s" % md5)
    fm.append("last_sync_time: %s" % yaml_escape(ts))
    fm.append("创建: %s" % yaml_escape(item.get("created", "")))
    fm.append("ima_url: %s" % yaml_escape(build_item_link(kb_id, media_id)))
    fm.append("ima_content_full: %s" % ("false" if item.get("partial") else "true"))
    tags = item.get("tags") or []
    if tags:
        fm.append("标签:")
        for t in tags:
            fm.append("  - %s" % yaml_escape(t))
    else:
        fm.append("标签:")
        fm.append("  - IMA同步")
    fm.append("---")

    lines = list(fm)
    lines.append("")
    lines.append("# %s" % display_title)
    lines.append("")
    lines.append("> [!info] IMA 来源")
    lines.append("> 知识库：%s" % kb_name)
    lines.append("> 在 IMA 中查看：%s" % build_item_link(kb_id, media_id))
    lines.append("> 同步时间：%s" % ts)
    lines.append("")

    partial = item.get("partial")
    if partial:
        lines.append("> [!warning] 正文不完整")
        lines.append("> IMA 未返回该条目的完整正文（摘要已被截断），以下为可获取的部分内容。")
        lines.append("> 完整内容请在 IMA 中查看上方链接。")
        lines.append("")

    lines.append("---")
    lines.append("")
    body = strip_leading_title(content, title).strip()
    if body:
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- plan


def cmd_plan(args):
    """读取 MCP 拉到的 IMA 条目 JSON + 本地状态，产出落盘计划。"""
    with open(args.items, "r", encoding="utf-8") as f:
        payload = json.load(f)

    kb_id = payload.get("knowledge_base_id", "")
    kb_name = payload.get("knowledge_base_name", "")
    items = payload.get("items", [])
    vault_path = payload.get("vault_path") or args.vault

    local_index = scan_local(vault_path)
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    plan = []
    for it in items:
        media_id = it.get("media_id") or ""
        title = it.get("title") or "未命名"
        content = it.get("content") or ""
        md5 = content_hash(title, content)
        rec = {
            "media_id": media_id,
            "title": title,
            "content": content,
            "content_hash": md5,
            "knowledge_base_id": kb_id,
            "knowledge_base_name": kb_name,
            "type_label": it.get("type_label", "IMA同步"),
            "created": it.get("created", ""),
            "tags": it.get("tags") or [],
            "sync_time": ts,
            "partial": bool(it.get("partial")),
            # IMA 云端文件夹路径（用于镜像落点）
            "cloud_folder": it.get("cloud_folder", ""),
        }
        prev = local_index.get(media_key(media_id))
        if prev and str(prev.get("ima_sync_hash") or "") == md5:
            rec["action"] = "skip"
            rec["reason"] = "内容未变"
            rec["existing_path"] = prev["rel_path"]
        elif prev and it.get("partial") and not prev.get("ima_content_full"):
            # 本地存的是完整正文、这轮云端只给了摘要 → 摘要可能更残缺，不降级覆盖
            rec["action"] = "skip"
            rec["reason"] = "云端仅返回摘要，本地已是完整正文，保留不覆盖"
            rec["existing_path"] = prev["rel_path"]
        elif prev and it.get("partial") and prev.get("ima_content_full"):
            # 本地是完整正文、这轮云端只给了摘要。
            # 摘要内容已被本地正文涵盖 → 保留本地（不降级覆盖）。
            # 摘要里有本地没有的新内容 → 用摘要更新（可能更全）。
            local_body = read_local_body(prev.get("path"))
            # 比较时忽略所有空白与标点差异（摘要常见「1Codex」vs 正文「1. Codex」）
            summary_norm = re.sub(r"[^\w\u4e00-\u9fff]+", "", content or "")
            local_norm = re.sub(r"[^\w\u4e00-\u9fff]+", "", local_body or "")
            if summary_norm and summary_norm in local_norm:
                rec["action"] = "skip"
                rec["reason"] = "云端仅返回摘要，内容已被本地正文涵盖，保留不覆盖"
                rec["existing_path"] = prev["rel_path"]
            else:
                rec["action"] = "write"
                rec["reason"] = "摘要更新（云端返回摘要，本地正文不完整）"
                rec["existing_path"] = prev["rel_path"]
        else:
            sens = scan_sensitive(content)
            if sens:
                rec["action"] = "block"
                rec["reason"] = "敏感内容阻断"
                rec["hits"] = [{"category": c, "evidence": e} for c, e in sens]
            else:
                rec["action"] = "write"
                if prev:
                    rec["reason"] = "内容有更新"
                    rec["existing_path"] = prev["rel_path"]
                else:
                    rec["reason"] = "新条目"
        plan.append(rec)

    # 云端已删除、本地仍存在的条目：只报告，不删除本地文件（硬规则 R6）
    cloud_keys = {media_key(it.get("media_id") or "") for it in items}
    orphans = []
    for key, loc in local_index.items():
        if key in cloud_keys:
            continue
        orphans.append({
            "media_key": key,
            "ima_media_id": loc.get("ima_media_id", ""),
            "path": loc["rel_path"],
            "title": loc.get("title", ""),
        })

    out = {
        "knowledge_base_id": kb_id,
        "knowledge_base_name": kb_name,
        "vault_path": vault_path,
        "generated_at": ts,
        # 落点日期：默认取今天（决定 2026年43周/2026/10/30周五 这一层路径）
        "sync_date": payload.get("sync_date") or datetime.now(CST).strftime("%Y-%m-%d"),
        "total": len(items),
        "plan": plan,
        "orphans": orphans,
    }
    out["summary"] = {
        "write": len([p for p in plan if p["action"] == "write"]),
        "skip": len([p for p in plan if p["action"] == "skip"]),
        "block": len([p for p in plan if p["action"] == "block"]),
        "orphan": len(orphans),
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps({"summary": out["summary"], "total": out["total"]}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- apply


# ---------------------------------------------------------------- 落点目录（年/周/日）

WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
IMA_ROOT_REL = ("01-Raw", "1-我的文章", "1-1来自IMA")


def week_folder_name(d) -> str:
    """「2026年44周」——ISO 8601 周编号（周一为一周之始）。

    约定（用户 2026-10-31 最终确认）：
      - 一周从**周一**开始，周日结束
      - 周次用 **ISO 8601**：含当年第一个周四的那一周为第 1 周
      - 因此 2026-10-30（周五）属于 **2026年44周**（10/26 ~ 11/01）
        —— 与 IMA 知识库里手建的「2026年第44周」完全一致
      - 跨年周归属**ISO 年**（含周四的年份），不会出现「0周」
    """
    d = date(d.year, d.month, d.day) if not isinstance(d, date) else d
    iso = d.isocalendar()
    return "%d年%d周" % (iso[0], iso[1])


def day_folder_name(d) -> str:
    """「2026/10/30周五」——按天拆成 年/月/日+星期 三层。"""
    return "%d/%02d/%02d%s" % (d.year, d.month, d.day, WEEKDAYS_CN[d.weekday()])


# ------------------------------------------------ 云端文件夹路径 → Obsidian 落点

WEEK_DIR_RE = re.compile(r"^(\d{4})年(\d+)周$")
WEEK_DIR_RE_RAW = re.compile(r"^(\d{4})年第(\d+)周$")
# 云端日期目录可能是 2026-10-30，也可能是 2026-11-1（月/日不补零）→ 月日都允许 1~2 位
DATE_DIR_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def parse_cloud_folder(cloud_folder: str):
    """把 IMA 云端文件夹路径解析成 (周目录名, 日期目录名 或 None)。

    IMA 端结构（用户 2026-10-31 建）：
        2026年第44周/2026-10-30/   → 周目录 + 日期目录
        2026年第45周/              → 只有周目录

    返回 ("2026年44周", "2026/10/30周五") 或 ("2026年45周", None)
    """
    parts = [p for p in (cloud_folder or "").split("/") if p]
    week_name = None
    day_name = None
    for p in parts:
        m = WEEK_DIR_RE_RAW.match(p)
        if m:
            week_name = "%s年%d周" % (m.group(1), int(m.group(2)))
            continue
        m = DATE_DIR_RE.match(p)
        if m:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            day_name = day_folder_name(d)
    return week_name, day_name


def week_start_from_name(week_name: str):
    """「2026年44周」→ 该周的周一（ISO）。"""
    m = WEEK_DIR_RE.match(week_name or "")
    if not m:
        return None
    return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)


def ensure_week_tree_by_name(root: str, week_name: str):
    """在 root 下建出指定周的完整 7 天目录（跨月自动分到不同月目录）。

    返回 (该周目录绝对路径, [7 个日期目录的绝对路径])
    """
    mon = week_start_from_name(week_name)
    if not mon:
        return None, []
    week_dir = os.path.join(root, week_name)
    days = []
    for i in range(7):
        d = mon + timedelta(days=i)
        p = os.path.join(week_dir, *day_folder_name(d).split("/"))
        os.makedirs(p, exist_ok=True)
        days.append(p)
    return week_dir, days


def resolve_item_out_dir(vault_path: str, item: dict, anchor=None):
    """决定单条笔记的落盘目录（绝对路径）。

    优先按 IMA 云端文件夹镜像：
      云端 2026年第44周/2026-10-30  → <root>/2026年44周/2026/10/30周五/
      云端 2026年第45周（无日期层）  → <root>/2026年45周/2026/11/02周一/
        （该周无日期子目录时，落到该周的周一，保证周目录内正好是这周的 7 天）
    云端路径缺失时，退回按同步日期 anchor 计算。
    """
    root = os.path.join(vault_path, *IMA_ROOT_REL)
    os.makedirs(root, exist_ok=True)

    week_name, day_name = parse_cloud_folder(item.get("cloud_folder") or "")

    if not week_name:
        anchor = anchor or datetime.now(CST).date()
        week_name = week_folder_name(anchor)
        if not day_name:
            day_name = day_folder_name(anchor)

    week_dir, _days = ensure_week_tree_by_name(root, week_name)

    if not day_name:
        # 云端只有周目录 → 落到该周的周一，保持「周目录内正好 7 天」的纯净结构
        mon = week_start_from_name(week_name)
        if not mon:
            created = str(item.get("created") or "")
            try:
                mon = datetime.strptime(created, "%Y-%m-%d").date()
            except Exception:
                mon = anchor or datetime.now(CST).date()
        day_name = day_folder_name(mon)

    target = os.path.join(week_dir, *day_name.split("/"))
    os.makedirs(target, exist_ok=True)
    return target


def ensure_week_tree(root: str, anchor):
    """在 root 下建出 anchor 所在周的完整 7 天目录（跨月自动分到不同月目录）。

    周次由 week_folder_name() 统一计算，7 天共用同一个周目录名
    （跨年周的周日仍归属起始年，不会分到两个周目录）。

    返回 (该周目录绝对路径, [7 个日期目录的绝对路径])
    """
    from datetime import timedelta
    mon = anchor - timedelta(days=anchor.weekday())
    week_dir = os.path.join(root, week_folder_name(anchor))
    days = []
    for i in range(7):
        d = mon + timedelta(days=i)
        p = os.path.join(week_dir, *day_folder_name(d).split("/"))
        os.makedirs(p, exist_ok=True)
        days.append(p)
    return week_dir, days


def resolve_out_dir(vault_path: str, anchor=None, out_dir_override=None):
    """决定本次落盘目录。

    规则：01-Raw/1-我的文章/1-1来自IMA/{年}年{周}周/{年}/{月}/{日+星期}/
    例：01-Raw/1-我的文章/1-1来自IMA/2026年43周/2026/10/30周五/
    """
    if out_dir_override:
        p = out_dir_override if os.path.isabs(out_dir_override) else os.path.join(vault_path, out_dir_override)
        os.makedirs(p, exist_ok=True)
        return p
    from datetime import datetime as _dt
    anchor = anchor or _dt.now(CST).date()
    root = os.path.join(vault_path, *IMA_ROOT_REL)
    os.makedirs(root, exist_ok=True)
    ensure_week_tree(root, anchor)
    return os.path.join(root, week_folder_name(anchor), *day_folder_name(anchor).split("/"))


def unique_path(dir_path: str, base: str, ext: str = ".md") -> str:
    p = os.path.join(dir_path, base + ext)
    n = 2
    while os.path.exists(p):
        p = os.path.join(dir_path, "%s-%d%s" % (base, n, ext))
        n += 1
    return p


def migrate_if_needed(target: str, out_dir: str):
    """云端把条目挪到别的周/日期目录时，本地跟着迁移。

    返回迁移后的绝对路径；无需迁移时原样返回。
    """
    if not target or not os.path.exists(target):
        return target
    try:
        if os.path.dirname(os.path.abspath(target)) == os.path.abspath(out_dir):
            return target
    except Exception:
        return target
    moved = os.path.join(out_dir, os.path.basename(target))
    if os.path.abspath(moved) == os.path.abspath(target):
        return target
    try:
        os.makedirs(out_dir, exist_ok=True)
        os.replace(target, moved)
        return moved
    except Exception:
        return target


def cmd_apply(args):
    """按 plan 落盘到 vault。"""
    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)

    vault_path = plan["vault_path"]
    # 落点：默认 01-Raw/1-我的文章/1-1来自IMA/{年}年{周}周/{年}/{月}/{日+星期}/
    anchor = plan.get("sync_date")
    if anchor:
        try:
            anchor = datetime.strptime(str(anchor), "%Y-%m-%d").date()
        except Exception:
            anchor = None
    default_out_dir = resolve_out_dir(vault_path, anchor, args.out_dir or plan.get("out_dir"))
    os.makedirs(default_out_dir, exist_ok=True)

    written, skipped, blocked, failed = [], [], [], []
    for p in plan["plan"]:
        if p["action"] == "skip":
            skipped.append({"media_id": p["media_id"], "title": p["title"], "path": p.get("existing_path")})
            # 云端条目重新出现 → 撤掉本地的「云端已删除」标记
            ep = p.get("existing_path")
            if ep:
                t = os.path.join(vault_path, ep)
                if os.path.exists(t):
                    unmarked, new_path = unmark_orphan(t)
                    if unmarked:
                        skipped[-1]["unmarked_orphan"] = True
                        skipped[-1]["new_path"] = os.path.relpath(new_path, vault_path).replace("\\", "/")
                        t = new_path
                    # 内容未变，但云端挪了目录 → 本地跟着迁移
                    if p.get("cloud_folder"):
                        out_dir_m = resolve_item_out_dir(vault_path, p, anchor)
                        moved = migrate_if_needed(t, out_dir_m)
                        if os.path.abspath(moved) != os.path.abspath(t):
                            skipped[-1]["path"] = os.path.relpath(moved, vault_path).replace("\\", "/")
                            skipped[-1]["moved"] = True
            continue
        if p["action"] == "block":
            blocked.append({
                "media_id": p["media_id"],
                "title": p["title"],
                "hits": p.get("hits", []),
            })
            continue
        base = safe_filename(p["title"])
        # 该条目的落点目录：优先按 IMA 云端文件夹镜像，其次退回本次默认目录
        out_dir = resolve_item_out_dir(vault_path, p, anchor) if p.get("cloud_folder") else default_out_dir
        os.makedirs(out_dir, exist_ok=True)
        # 已有本地副本 → 原地更新（避免每次同步产生重复文件）
        existing = p.get("existing_path")
        if existing:
            target = os.path.join(vault_path, existing)
            if not os.path.exists(target):
                target = unique_path(out_dir, base)
            else:
                # 本地曾被打上「云端已删除」标记，现在云端又有了 → 撤掉标记（含文件名）
                fm_now, _ = split_frontmatter(read_text(target))
                if parse_frontmatter(fm_now).get("ima_cloud_status") == ORPHAN_STATUS:
                    _, target = unmark_orphan(target)
                else:
                    # 云端归属目录变了（周次/日期调整）→ 原地更新时迁到新目录
                    target = migrate_if_needed(target, out_dir)
        else:
            target = unique_path(out_dir, base)
        content = render_note(p, None)
        try:
            with open(target, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            written.append({
                "media_id": p["media_id"],
                "title": p["title"],
                "path": os.path.relpath(target, vault_path).replace("\\", "/"),
                "mode": "update" if existing and os.path.exists(os.path.join(vault_path, existing)) else "create",
                "bytes": len(content.encode("utf-8")),
            })
        except Exception as e:
            failed.append({"media_id": p["media_id"], "title": p["title"], "error": str(e)})

    # 云端已删除、本地保留：不删文件，只在标题上加横线标记（硬规则 R6）
    marked = []
    for o in plan.get("orphans", []):
        rel = o.get("path") or ""
        target = os.path.join(vault_path, rel) if rel else ""
        if not target or not os.path.exists(target):
            continue
        changed, new_path = mark_orphan(target, plan.get("generated_at") or datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"))
        marked.append({
            "media_id": o.get("ima_media_id", ""),
            "title": o.get("title", ""),
            "path": rel,
            "new_path": os.path.relpath(new_path, vault_path).replace("\\", "/"),
            "marked": changed,
        })

    out = {"written": written, "skipped": skipped, "blocked": blocked, "failed": failed, "orphans_marked": marked}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps({
        "written": len(written),
        "skipped": len(skipped),
        "blocked": len(blocked),
        "failed": len(failed),
        "orphan_marked": len([m for m in marked if m["marked"]]),
        "orphan_total": len(marked),
    }, ensure_ascii=False))
    return 0 if not failed else 1


# ---------------------------------------------------------------- main


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="IMA -> Obsidian 反向同步引擎")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("plan", help="产出落盘计划（只读 vault）")
    p1.add_argument("--items", required=True, help="MCP 拉到的条目 JSON")
    p1.add_argument("--vault", default=None, help="vault 路径（items.json 内可覆盖）")
    p1.add_argument("--out", default=None)
    p1.set_defaults(func=cmd_plan)

    p2 = sub.add_parser("apply", help="按计划落盘到 vault")
    p2.add_argument("--plan", required=True)
    p2.add_argument("--out-dir", default=None,
                    help="落盘目录覆盖。默认 01-Raw/1-我的文章/1-1来自IMA/{年}年{周}周/{年}/{月}/{日+星期}/")
    p2.add_argument("--out", default=None)
    p2.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
