#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ttm.py — IMA拉取入库前（语音转写结构化）
把非结构化语音转写（会议纪要 / 面谈闲聊 / 微信语音）落成结构化纪要卡片。

设计分工（重要）：
  - 本脚本只做**确定性机械活**：扫描、去重、时间/时长/地点/人名/项目的**候选**提取、渲染、落盘。
  - 语义活（认人、定主题、抓待办、写结论）由执行时的模型完成，脚本只接收模型填好的 card.json。
  不要在脚本里加任何「猜内容」的逻辑，猜错比不猜更糟。

子命令：
  scan   --vault D:/OB [--file 关键词] [--json out.json]
  render --card card.json [--vault D:/OB] [--dry-run] [--force]
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

DEFAULT_VAULT = Path("D:/OB")
IMA_ROOT = "01-Raw/1-我的文章/1-1来自IMA"
PROJECT_ROOT = "01-Raw/5-项目一手资料"
WIKI_CLIENT = "02-wiki/3-客户"
WIKI_PROJ = "02-wiki/7-项目档案"

FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
TS_RE = re.compile(r"\b(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\b")
DATE_FULL_RE = re.compile(
    r"(\d{4})\s*[年.\-/]\s*(\d{1,2})\s*[月.\-/]\s*(\d{1,2})\s*日?\s*(\d{1,2}):(\d{2})"
)
DATE_ONLY_RE = re.compile(r"(\d{4})\s*[年.\-/]\s*(\d{1,2})\s*[月.\-/]\s*(\d{1,2})")
PROJ_DIR_RE = re.compile(r"^(5-\d+)-(.+)$")
PERSON_DIR_RE = re.compile(r"^5-[\d-]+-(.+?)档案$")

# 称呼：杭总 / 李工 / 顾老师 / 王部长 —— 只取称呼，全名交给模型判定。
# 必须用姓氏白名单，否则「人工 / 加工 / 老总 / 施工」全被当人名。
SURNAMES = (
    "李王张刘陈杨黄赵周吴徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾萧田董袁潘于蒋蔡"
    "余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛段雷侯龙"
    "史陶黎贺顾毛郝龚邵万钱严武戴莫孔向汤殷杭修宋欧葛"
)
ADDR_RE = re.compile(
    r"([%s][\u4e00-\u9fa5]{0,2}?)(总|工|老师|经理|部长|总监|老板|主任|书记)" % SURNAMES
)
# 误报：董事长总 / 总经理 / 老总 / 大老板 —— 尾字是「长」或本身是官衔词
ADDR_BAD = ("董事长", "总经理", "经理", "老总", "大老板", "长总", "的老板", "作工", "员工")

# 地点：只认显式标注「地址 / 地点 / 位于」的句子，且长度受限。
# 不要用裸「在」—— 口语里「在上海成立一个运营中心」会被误当地址。
PLACE_HINT_RE = re.compile(
    r"(?:地址|地点|位于)[：:是在]{0,2}"
    r"([\u4e00-\u9fa5A-Za-z0-9·（）()\-]{2,16}"
    r"(?:路\d*号?|街\d*号?|镇|村|大厦|中心|广场|园区|酒店|宾馆|咖啡|饭店|餐厅|会所|公馆|茶馆))"
)

TYPES = ("会议纪要", "接触纪要", "电话纪要")


# ---------------- 基础工具 ----------------

def read_text(p: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "utf-16"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return p.read_text(encoding="utf-8", errors="replace")


def parse_frontmatter(text: str):
    m = FM_RE.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), text[m.end():]
    fm = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            continue
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, body


def clean_body(body: str) -> str:
    """去掉 H1、IMA 来源 callout、首部分隔线，保留原始转写文字（一字不改）。"""
    lines = body.splitlines()
    out, i, n = [], 0, len(lines)
    started = False
    while i < n:
        s = lines[i].strip()
        if not started:
            if not s or s == "---" or s.startswith("# "):
                i += 1
                continue
            if s.startswith("> [!info]"):
                i += 1
                while i < n and lines[i].strip().startswith(">"):
                    i += 1
                conn = i < n and lines[i].strip() == "---"
                i += 1 if conn else 0
                continue
            if s.startswith(">"):
                i += 1
                continue
            started = True
        out.append(lines[i])
        i += 1
    return "\n".join(out).strip()


def max_timestamp(text: str) -> int:
    """转写里的时间码最大值（IMA 音频转写每段开头带 00:00 式时间码）。"""
    best = 0
    for m in TS_RE.finditer(text):
        if m.group(3):
            sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        else:
            sec = int(m.group(1)) * 60 + int(m.group(2))
        best = max(best, sec)
    return best


def human_duration(sec: int) -> str:
    if sec <= 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"约 {h} 小时 {m:02d} 分" if m else f"约 {h} 小时"
    return f"约 {m} 分钟" if m else f"约 {s} 秒"


def guess_datetime(text: str):
    head = text[:600]
    m = DATE_FULL_RE.search(head) or DATE_FULL_RE.search(text[:3000])
    if m:
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}", f"{hh:02d}:{mm:02d}"
        except ValueError:
            pass
    m = DATE_ONLY_RE.search(head)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}", ""
    return "", ""


def guess_place(text: str) -> str:
    for seg in re.split(r"[\n。！？]", text[:1200]):
        m = PLACE_HINT_RE.search(seg)
        if m:
            cand = m.group(1).strip()
            if 4 <= len(cand) <= 28:
                return cand
    return ""


# ---------------- 库内已有实体 ----------------

def load_known_entities(vault: Path):
    """收集库内已知项目名与人物姓名，用于候选匹配。"""
    projects, people = {}, {}

    proot = vault / PROJECT_ROOT
    if proot.is_dir():
        for d in proot.iterdir():
            if not d.is_dir():
                continue
            m = PROJ_DIR_RE.match(d.name)
            if m:
                projects[m.group(2)] = str(d.relative_to(vault)).replace("\\", "/")
            for sub in d.rglob("*-*档案"):
                if not sub.is_dir():
                    continue
                pm = PERSON_DIR_RE.match(sub.name)
                if pm:
                    name = pm.group(1).strip()
                    if 2 <= len(name) <= 4:
                        people[name] = str(sub.relative_to(vault)).replace("\\", "/")

    # wiki 客户卡：3-17-买方伙伴-修克忠.md
    for base in (WIKI_CLIENT, WIKI_PROJ):
        d = vault / base
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            stem = f.stem
            parts = stem.split("-")
            if len(parts) >= 4:
                name = parts[-1].strip()
                if 2 <= len(name) <= 4:
                    people.setdefault(name, str(f.relative_to(vault)).replace("\\", "/"))
    return projects, people


def collect_processed(vault: Path) -> set:
    """已结构化的转写：卡片 frontmatter 里带 source_transcript。"""
    done = set()
    for base in ("01-Raw", "02-wiki"):
        d = vault / base
        if not d.is_dir():
            continue
        for f in d.rglob("*.md"):
            try:
                txt = read_text(f)
            except OSError:
                continue
            if "source_transcript:" not in txt:
                continue
            fm, _ = parse_frontmatter(txt)
            src = fm.get("source_transcript", "").strip()
            if src:
                done.add(src.replace("\\", "/"))
    return done


def is_audio_note(fm: dict, body: str) -> bool:
    if fm.get("类型", "") == "音频":
        return True
    if fm.get("ima_media_id", "").startswith("soundrecording_"):
        return True
    return "soundrecording_" in fm.get("ima_url", "")


def load_skip_list(vault: Path) -> dict:
    """昭屹标了「跳过」的转写：授课/分享/无洽谈价值的内容，不建卡也不再提示。
    存放：<vault>/.workbuddy/ttm_skip.json  →  {"相对路径": "跳过的原因"}
    """
    p = vault / ".workbuddy" / "ttm_skip.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).replace("\\", "/"): str(v) for k, v in data.items()}


# ---------------- scan ----------------

def cmd_scan(args):
    vault = Path(args.vault).resolve()
    root = vault / IMA_ROOT
    if not root.is_dir():
        die(f"找不到 IMA 落盘目录：{root}")

    projects, people = load_known_entities(vault)
    processed = collect_processed(vault)
    skips = load_skip_list(vault)

    pending, skipped = [], []
    for f in sorted(root.rglob("*.md"), key=lambda p: str(p)):
        rel = str(f.relative_to(vault)).replace("\\", "/")
        if args.file and args.file not in rel:
            continue
        text = read_text(f)
        fm, body = parse_frontmatter(text)
        # 本 skill 生成的卡片本身也带 ima_media_id，会被 is_audio_note 误判成转写。
        # 判据：带 source_transcript 字段，或类型就是三种纪要之一 → 是产物，不是源。
        if fm.get("source_transcript") or fm.get("类型", "") in TYPES:
            continue
        if not is_audio_note(fm, body):
            continue
        if rel in skips:
            skipped.append({"source_rel": rel, "reason": f"昭屹标记跳过（{skips[rel]}）"})
            continue
        if rel in processed:
            skipped.append({"source_rel": rel, "reason": "已结构化"})
            continue

        clean = clean_body(body)
        if len(clean) < 200:
            skipped.append({"source_rel": rel, "reason": f"正文过短（{len(clean)} 字）"})
            continue

        date_guess, time_guess = guess_datetime(clean)
        if not date_guess:
            date_guess = fm.get("创建", "") or fm.get("last_sync_time", "")[:10]
        dur = max_timestamp(clean)

        # 称呼候选：姓氏白名单 + 频次过滤（只出现一次的多半是误切）
        cnt = Counter()
        for m in ADDR_RE.finditer(clean):
            tok = m.group(1) + m.group(2)
            if len(tok) > 4 or any(b in tok for b in ADDR_BAD):
                continue
            cnt[tok] += 1
        addr_hits = [t for t, c in cnt.most_common(20) if c >= 2]
        addr_low = [t for t, c in cnt.most_common(40) if c == 1][:12]

        known_hits = [n for n in people if n in clean]
        proj_hits = []
        for pname, ppath in projects.items():
            if len(pname) >= 2 and pname in clean:
                proj_hits.append({"project": pname, "path": ppath})
        # 人物命中反推所属项目
        for n in known_hits:
            ppath = people[n]
            pdir = "/".join(ppath.split("/")[:2])
            if not any(x["path"] == pdir for x in proj_hits):
                for pname, pp in projects.items():
                    if pp == pdir:
                        proj_hits.append({"project": pname, "path": pdir, "via": n})

        pending.append({
            "source_rel": rel,
            "source_path": str(f),
            "title": fm.get("title", f.stem),
            "ima_url": fm.get("ima_url", ""),
            "ima_media_id": fm.get("ima_media_id", ""),
            "date_guess": date_guess,
            "time_guess": time_guess,
            "duration_seconds": dur,
            "duration_human": human_duration(dur),
            "place_guess": guess_place(clean),
            "address_candidates": addr_hits,
            "address_low_conf": addr_low,
            "known_people": sorted(known_hits),
            "project_candidates": proj_hits,
            "char_count": len(clean),
            "body": clean,
        })

    result = {
        "vault": str(vault),
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {"pending": len(pending), "skipped": len(skipped)},
        "pending": pending,
        "skipped": skipped,
    }

    out = args.json or ""
    if out:
        Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 终端摘要（body 不打印，避免刷屏）
    slim = json.loads(json.dumps(result, ensure_ascii=False))
    for it in slim["pending"]:
        it["body"] = f"<{it['char_count']} 字，见 json>"
    print(json.dumps(slim, ensure_ascii=False, indent=2))
    if out:
        print(f"\n[已写出] {out}", file=sys.stderr)


# ---------------- render ----------------

REQUIRED = {
    "source_rel": "源转写相对路径",
    "type": "纪要类型（会议纪要/接触纪要/电话纪要）",
    "target_dir": "卡片落点目录（相对 vault）",
    "record_date": "记录日期 YYYY-MM-DD",
    "duration": "时长",
    "location": "地点/地址",
    "subject_person": "主诉人",
    "participants": "对话人（列表）",
    "title": "标题主体，形如「杭广发（文旅项目投资与运营）」",
}


def esc(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").strip()


def rel_link(from_dir: Path, to_file: Path) -> str:
    try:
        return Path(os.path.relpath(str(to_file), str(from_dir))).as_posix()
    except ValueError:
        return str(to_file).replace("\\", "/")


def cmd_render(args):
    card_path = Path(args.card)
    if not card_path.is_file():
        die(f"卡片数据不存在：{card_path}")
    card = json.loads(card_path.read_text(encoding="utf-8"))
    vault = Path(args.vault or card.get("vault") or DEFAULT_VAULT).resolve()

    # ---- 必填校验：缺一个都不写 ----
    missing = [f"{k}（{v}）" for k, v in REQUIRED.items() if not card.get(k)]
    card.setdefault("observers", [])  # 旁听人允许为空列表，但键必须在
    if missing:
        print(json.dumps({
            "ok": False,
            "error": "字段缺失，不要写文件，回去问用户",
            "missing": missing,
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    ctype = card["type"]
    if ctype not in TYPES:
        die(f"type 必须是 {TYPES} 之一，当前：{ctype}")

    date = card["record_date"]
    try:
        y, mo, d = (int(x) for x in date.split("-"))
        date_cn = f"{y}.{mo:02d}.{d:02d}"
    except ValueError:
        die(f"record_date 格式应为 YYYY-MM-DD，当前：{date}")

    target_dir = vault / card["target_dir"]
    if not target_dir.is_dir():
        die(f"落点目录不存在：{target_dir}（不自动建，先确认归属）")

    fname = f"{date_cn}_{ctype}_{card['title']}.md"
    fname = re.sub(r'[\\/:*?"<>|]', "／", fname).strip()
    out_path = target_dir / fname

    if out_path.exists() and not args.force:
        print(json.dumps({
            "ok": False,
            "error": "目标卡片已存在，未覆盖（确认要覆盖请加 --force）",
            "path": str(out_path),
        }, ensure_ascii=False, indent=2))
        sys.exit(3)

    src = vault / card["source_rel"]
    link = rel_link(target_dir, src) if src.exists() else ""
    participants = card.get("participants") or []
    observers = card.get("observers") or []
    topics = card.get("topics") or []
    todos = card.get("todos") or []
    numbers = card.get("numbers") or []
    doubts = card.get("doubts") or []
    project = card.get("project", "")
    transcript_note = card.get("transcript_note", "")

    L = []
    A = L.append

    # ---- frontmatter ----
    A("---")
    A(f"title: {esc(date_cn)} {ctype} · {esc(card['title'])}")
    A(f"类型: {ctype}")
    A(f"记录日期: {date}")
    if card.get("record_time"):
        A(f"记录时间: \"{esc(card['record_time'])}\"")
    A(f"时长: {esc(card['duration'])}")
    A(f"地点: {esc(card['location'])}")
    A(f"主诉人: {esc(card['subject_person'])}")
    A("对话人:")
    for p in participants:
        A(f"  - {esc(p)}")
    if observers:
        A("旁听人:")
        for p in observers:
            A(f"  - {esc(p)}")
    else:
        A("旁听人: []")
    if topics:
        A("主题:")
        for t in topics:
            A(f"  - {esc(t.get('title', t) if isinstance(t, dict) else t)}")
    A(f"待办: {len(todos)}")
    if project:
        A(f"项目: {esc(project)}")
    A(f"source_transcript: {card['source_rel']}")
    if card.get("ima_url"):
        A(f"ima_url: \"{esc(card['ima_url'])}\"")
    if card.get("ima_media_id"):
        A(f"ima_media_id: {esc(card['ima_media_id'])}")
    A("tags:")
    A("  - 语音纪要")
    A(f"  - {ctype}")
    A("---")
    A("")

    # ---- 标题 ----
    A(f"# {ctype} · {card['title']}")
    A("")

    # ---- 场合块 ----
    when = f"{date}" + (f" {card['record_time']}" if card.get("record_time") else "")
    who = " · ".join([card["subject_person"]] + list(participants))
    A(f"> **场合**：{esc(card['location'])}，{when}，{esc(card['duration'])}。")
    A(f"> **在场**：{who}" + (f"（旁听：{'、'.join(observers)}）" if observers else ""))
    if link:
        A(f"> **原始转写**：[{esc(card.get('source_title') or src.stem)}]({link})")
    A("")
    A("---")
    A("")

    # ---- 一句话结论 ----
    A("## 一句话结论")
    A("")
    A(card.get("summary") or "（待补）")
    A("")
    A("---")
    A("")

    # ---- 一、记录信息 ----
    A("## 一、记录信息")
    A("")
    A("| 项 | 内容 |")
    A("| --- | --- |")
    A(f"| 转写地址 | `{card['source_rel']}` |")
    A(f"| 时间 | {when} |")
    A(f"| 时长 | {esc(card['duration'])} |")
    A(f"| 地点 | {esc(card['location'])} |")
    subj = card["subject_person"]
    if card.get("subject_note"):
        subj += f"（{esc(card['subject_note'])}）"
    A(f"| 主诉人 | {subj} |")
    A(f"| 对话人 | {'、'.join(participants) or '—'} |")
    A(f"| 旁听人 | {'、'.join(observers) if observers else '无'} |")
    if transcript_note:
        A(f"| 转写 | {esc(transcript_note)} |")
    if card.get("prev_card"):
        A(f"| 关联 | [上次纪要]({esc(card['prev_card'])}) |")
    A("")
    A("---")
    A("")

    # ---- 二、主题 ----
    A("## 二、主题")
    A("")
    if topics:
        A("| # | 主题 | 要点 | 关键数字 |")
        A("| --- | --- | --- | --- |")
        for i, t in enumerate(topics, 1):
            if isinstance(t, dict):
                A(f"| {i} | {esc(t.get('title'))} | {esc(t.get('point'))} | {esc(t.get('numbers'))} |")
            else:
                A(f"| {i} | {esc(t)} |  |  |")
    else:
        A("（无）")
    A("")
    A("---")
    A("")

    # ---- 三、待办 ----
    A("## 三、待办")
    A("")
    if todos:
        A("| # | 待办 | 负责人 | 截止 | 依据原话 |")
        A("| --- | --- | --- | --- | --- |")
        for i, t in enumerate(todos, 1):
            if isinstance(t, dict):
                A(f"| {i} | {esc(t.get('task'))} | {esc(t.get('owner')) or '待定'} | "
                  f"{esc(t.get('due')) or '—'} | {esc(t.get('quote'))} |")
            else:
                A(f"| {i} | {esc(t)} | 待定 | — |  |")
    else:
        A("（无）")
    A("")

    # ---- 四、关键数字 ----
    if numbers:
        A("---")
        A("")
        A("## 四、关键数字")
        A("")
        A("| 项 | 数值 | 备注 |")
        A("| --- | --- | --- |")
        for n in numbers:
            if isinstance(n, dict):
                A(f"| {esc(n.get('item'))} | {esc(n.get('value'))} | {esc(n.get('note'))} |")
            else:
                A(f"| — | {esc(n)} |  |")
        A("")

    # ---- 五、存疑 ----
    if doubts:
        A("---")
        A("")
        A("## 五、存疑与待核")
        A("")
        for dd in doubts:
            A(f"- ⚠️ {esc(dd)}")
        A("")

    # ---- 六、判断 ----
    if card.get("judgment"):
        A("---")
        A("")
        A("## 六、判断")
        A("")
        A(card["judgment"])
        A("")

    A("---")
    A("")
    A(f"> 结构化自：[{esc(src.stem) if src.exists() else card['source_rel']}]({link or '#'})"
      f" · 由 IMA拉取入库前 生成 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    A("")

    content = "\n".join(L)

    if args.dry_run:
        print(content)
        return

    out_path.write_text(content, encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "path": str(out_path),
        "rel": str(out_path.relative_to(vault)).replace("\\", "/"),
        "topics": len(topics),
        "todos": len(todos),
        "linked_source": bool(link),
    }, ensure_ascii=False, indent=2))


def die(msg: str):
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False, indent=2))
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="转写 → 结构化纪要卡片")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="扫描待结构化的语音转写")
    sp.add_argument("--vault", default=str(DEFAULT_VAULT))
    sp.add_argument("--file", default="", help="只处理路径包含该关键词的条目")
    sp.add_argument("--json", default="", help="把完整结果（含正文）写到该文件")
    sp.set_defaults(func=cmd_scan)

    rp = sub.add_parser("render", help="按 card.json 渲染并落盘")
    rp.add_argument("--card", required=True)
    rp.add_argument("--vault", default="")
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument("--force", action="store_true")
    rp.set_defaults(func=cmd_render)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
