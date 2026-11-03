#!/usr/bin/env python
"""IMA MCP 直连备用通道。

当 `mcp__ima-mcp__*` 工具没进本会话的工具索引时，用它直连 IMA 的 MCP 端点。

原理：从 WorkBuddy 本地连接器凭证库（connector-states.json v4）解密
ima-mcp 的 Authorization 头，再以 streamable-http 方式调用
https://ima.qq.com/mcp。

用法：
    python ima_mcp_direct.py list                          # 列出工具
    python ima_mcp_direct.py tree <kb_id>                  # 递归打印文件夹树
    python ima_mcp_direct.py fetch <kb_id> <out.json>      # 拉全部条目+正文 → items.json

依赖：cryptography（pip install cryptography）
"""
import base64, hashlib, hmac, json, os, sys, time, urllib.request

USER_ID = "cd2d0119-4a2c-4bd5-bf1c-2e894ed09be4"
CRED_DIR = os.path.expanduser("~/.workbuddy/connectors/" + USER_ID)
MASTER = os.path.join(CRED_DIR, ".master.key")
STATES = os.path.join(CRED_DIR, "connector-states.json")
URL = "https://ima.qq.com/mcp"
INFO = b"workbuddy-oauth-credentials-v1"


def _hkdf(ikm, salt, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, t = b"", b""
    for i in range(1, length // 32 + 2):
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
    return okm[:length]


def get_auth():
    """解密 ima-mcp 的 Authorization 头。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    master = open(MASTER, "rb").read()
    st = json.load(open(STATES, encoding="utf-8"))
    enc = st["encryption"]
    salt = base64.b64decode(enc["salt"])
    key = _hkdf(master + USER_ID.encode("utf-8"), salt, INFO, 32)
    env = st["headerOverrides"]["ima-mcp"]["Authorization"]
    aad = ("%s|connector-states:headerOverrides:ima-mcp|Authorization" % USER_ID).encode("utf-8")
    return AESGCM(key).decrypt(base64.b64decode(env["iv"]),
                                base64.b64decode(env["ct"]) + base64.b64decode(env["tag"]),
                                aad).decode("utf-8")


class MCP:
    def __init__(self, auth):
        self.auth = auth
        self.sid = None
        self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "workbuddy-ima-direct", "version": "1.0"}}})
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except Exception:
            pass

    def _post(self, payload):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "Authorization": self.auth,
             "MCP-Protocol-Version": "2025-06-18"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(URL, data=json.dumps(payload).encode("utf-8"),
                                     headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self.sid = sid
            raw = r.read().decode("utf-8", "ignore")
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        for line in raw.splitlines():
            if line.startswith("data:"):
                c = line[5:].strip()
                if c:
                    return json.loads(c)
        return {"_raw": raw[:500]}

    def call(self, method, params=None, _id=2):
        return self._post({"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}})

    def list_tools(self):
        return self.call("tools/list").get("result", {}).get("tools", [])

    def tool(self, name, args):
        return self.call("tools/call", {"name": name, "arguments": args})


def text_of(res):
    return "\n".join(c.get("text", "") for c in res.get("result", {}).get("content", [])
                     if c.get("type") == "text")


def list_kb(m, kb_id, folder_id=None):
    args = {"knowledge_base_id": kb_id, "limit": 50, "sort_type": "UPDATE_TS_DESC_SORT_TYPE"}
    if folder_id:
        args["folder_id"] = folder_id
    r = m.tool("get_knowledge_list", args)
    t = text_of(r)
    if r.get("result", {}).get("isError"):
        raise RuntimeError(t[:300])
    return json.loads(t)


def walk(m, kb_id, folder_id, path, out):
    """递归遍历，记录每条的云端文件夹路径到 _cloud_folder。"""
    for it in list_kb(m, kb_id, folder_id).get("knowledge_list", []) or []:
        if it.get("media_type") == 99:  # 文件夹
            fi = it.get("folder_info", {}) or {}
            fid = fi.get("folder_id") or it.get("media_id")
            name = fi.get("name") or it.get("title")
            print("  [文件夹] %s/%s" % (path, name))
            walk(m, kb_id, fid, path + "/" + name, out)
        else:
            print("  [条目]   %s/%s  (%s)" % (path, it.get("title"),
                                            (it.get("media_type_info") or {}).get("name")))
            it["_cloud_folder"] = path
            out.append(it)
    return out


def fetch_content(m, media_id):
    r = m.tool("fetch_media_content", {"media_id": media_id})
    txt = text_of(r)
    if r.get("result", {}).get("isError"):
        return "", True
    try:
        obj = json.loads(txt)
        for k in ("content", "media_content", "text", "markdown", "note_content"):
            v = obj.get(k) if isinstance(obj, dict) else None
            if isinstance(v, str) and v.strip():
                return v, False
        return json.dumps(obj, ensure_ascii=False, indent=1), True
    except Exception:
        return txt, False


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    m = MCP(get_auth())

    if cmd == "list":
        for t in m.list_tools():
            print("-", t["name"], "|", (t.get("description") or "")[:70])
        return

    kb_id = sys.argv[2]
    if cmd == "tree":
        walk(m, kb_id, None, "", [])
        return

    if cmd == "fetch":
        out_path = sys.argv[3]
        items = walk(m, kb_id, None, "", [])
        recs = []
        for it in items:
            content, partial = fetch_content(m, it["media_id"])
            recs.append({
                "media_id": it["media_id"],
                "title": it.get("title", ""),
                "type_label": (it.get("media_type_info") or {}).get("name", ""),
                "created": time.strftime("%Y-%m-%d", time.localtime(int(it.get("create_time", 0)) / 1000)),
                "cloud_folder": it.get("_cloud_folder", ""),
                "tags": it.get("tags") or [],
                "content": content,
                "partial": partial,
            })
            print("    正文 %d 字%s" % (len(content), "（摘要）" if partial else ""))
        json.dump({"knowledge_base_id": kb_id, "knowledge_base_name": "", "items": recs},
                  open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("已写出 %d 条 → %s" % (len(recs), out_path))


if __name__ == "__main__":
    main()
