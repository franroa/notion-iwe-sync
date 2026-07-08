"""Thin Notion API client + block<->markdown conversion."""
from __future__ import annotations

import re
import time

import requests

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


class Notion:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        })

    def req(self, method: str, path: str, **kw) -> dict:
        for attempt in range(6):
            r = self.s.request(method, API + path, timeout=30, **kw)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 1)) + 0.2)
                continue
            if r.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            if not r.ok:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"{method} {path}: gave up after retries")

    # ---- pages ----
    def search_pages(self):
        cursor = None
        while True:
            body = {"page_size": 100, "filter": {"property": "object", "value": "page"}}
            if cursor:
                body["start_cursor"] = cursor
            d = self.req("POST", "/search", json=body)
            yield from d["results"]
            if not d.get("has_more"):
                return
            cursor = d["next_cursor"]

    def get_page(self, page_id: str) -> dict:
        return self.req("GET", f"/pages/{page_id}")

    def create_page(self, parent_id: str, title: str) -> dict:
        return self.req("POST", "/pages", json={
            "parent": {"page_id": parent_id},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
        })

    def set_title(self, page: dict, title: str):
        prop = next((k for k, v in page["properties"].items() if v["type"] == "title"), None)
        if prop:
            self.req("PATCH", f"/pages/{page['id']}", json={
                "properties": {prop: {"title": [{"text": {"content": title}}]}}})

    # ---- blocks ----
    def children(self, block_id: str):
        cursor = None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            d = self.req("GET", path)
            yield from d["results"]
            if not d.get("has_more"):
                return
            cursor = d["next_cursor"]

    def delete_block(self, block_id: str):
        try:
            self.req("DELETE", f"/blocks/{block_id}")
        except RuntimeError as e:
            # idempotent delete: the block being gone already is success
            msg = str(e)
            if "archived" in msg or "-> 404" in msg:
                return
            raise

    def append(self, block_id: str, blocks: list[dict]):
        for i in range(0, len(blocks), 100):
            self.req("PATCH", f"/blocks/{block_id}/children",
                     json={"children": blocks[i:i + 100]})


def page_title(page: dict) -> str:
    for v in (page.get("properties") or {}).values():
        if v.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in v["title"]) or "Untitled"
    return "Untitled"


# ================= blocks -> markdown =================

def rt_to_md(rich: list[dict], link_map: dict[str, str] | None = None) -> str:
    out = []
    for t in rich:
        txt = t.get("plain_text", "")
        if t.get("type") == "mention" and t["mention"].get("type") == "page":
            pid = t["mention"]["page"]["id"]
            key = (link_map or {}).get(pid.replace("-", ""))
            out.append(f"[{txt}]({key})" if key else f"[{txt}](https://www.notion.so/{pid.replace('-', '')})")
            continue
        a = t.get("annotations", {})
        if a.get("code"):
            txt = f"`{txt}`"
        if a.get("bold"):
            txt = f"**{txt}**"
        if a.get("italic"):
            txt = f"*{txt}*"
        if a.get("strikethrough"):
            txt = f"~~{txt}~~"
        href = t.get("href")
        if href:
            m = re.fullmatch(r"https://www\.notion\.so/(?:[^/]*-)?([0-9a-f]{32})", href)
            if m and link_map and m.group(1) in link_map:
                href = link_map[m.group(1)]
            txt = f"[{txt}]({href})"
        out.append(txt)
    return "".join(out)


def blocks_to_md(api: Notion, block_id: str, link_map: dict[str, str] | None = None,
                 depth: int = 0, numbered_idx: list | None = None) -> list[str]:
    lines: list[str] = []
    ind = "  " * depth
    n = 0
    for b in api.children(block_id):
        t = b["type"]
        data = b.get(t, {})
        rt = rt_to_md(data.get("rich_text", []), link_map)
        n = n + 1 if t == "numbered_list_item" else 0
        kids = b.get("has_children") and t not in ("child_page", "child_database")

        if t == "paragraph":
            lines.append(ind + rt if rt else "")
        elif t in ("heading_1", "heading_2", "heading_3"):
            lines.append("#" * int(t[-1]) + " " + rt)
        elif t == "bulleted_list_item" or t == "toggle":
            lines.append(f"{ind}- {rt}")
        elif t == "numbered_list_item":
            lines.append(f"{ind}{n}. {rt}")
        elif t == "to_do":
            box = "x" if data.get("checked") else " "
            lines.append(f"{ind}- [{box}] {rt}")
        elif t == "code":
            lang = data.get("language", "")
            lang = "" if lang == "plain text" else lang
            lines += [f"```{lang}", *rt_to_md(data.get("rich_text", [])).split("\n"), "```"]
            kids = False
        elif t == "quote":
            lines.append(f"{ind}> {rt}")
        elif t == "callout":
            ico = data.get("icon") or {}
            emo = ico.get("emoji", "💡") if ico.get("type") == "emoji" else "💡"
            lines.append(f"{ind}> {emo} {rt}")
        elif t == "divider":
            lines.append("---")
        elif t == "child_page":
            pid = b["id"].replace("-", "")
            key = (link_map or {}).get(pid)
            title = data.get("title", "Untitled")
            lines.append(f"{ind}- [{title}]({key or 'https://www.notion.so/' + pid})")
            kids = False
        elif t == "child_database":
            lines.append(f"{ind}- 🗃️ *database: {data.get('title', '')}*")
        elif t == "image":
            src = (data.get("external") or data.get("file") or {}).get("url", "")
            cap = rt_to_md(data.get("caption", []))
            lines.append(f"{ind}![{cap}]({src})")
        elif t in ("bookmark", "embed", "link_preview"):
            lines.append(f"{ind}<{data.get('url', '')}>")
        elif t == "table":
            rows = list(api.children(b["id"]))
            for i, row in enumerate(rows):
                cells = [rt_to_md(cell, link_map) for cell in row["table_row"]["cells"]]
                lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
                if i == 0:
                    lines.append("|" + " --- |" * len(cells))
            kids = False
        elif t == "equation":
            lines.append(f"$${data.get('expression', '')}$$")
        else:
            if rt:
                lines.append(ind + rt)
        if kids:
            lines += blocks_to_md(api, b["id"], link_map, depth + 1)
        if t in ("paragraph", "heading_1", "heading_2", "heading_3", "code", "divider",
                 "quote", "callout", "table", "image") and not kids:
            lines.append("")
    # collapse trailing blanks
    while lines and lines[-1] == "":
        lines.pop()
    return lines


# ================= markdown -> blocks =================

INLINE = re.compile(r"(\*\*.+?\*\*|~~.+?~~|`[^`]+`|\[[^\]]*\]\([^)]+\)|\*[^*\n]+\*)")


def _chunks(s: str, n: int = 2000):
    return [s[i:i + n] for i in range(0, len(s), n)] or [""]


def md_inline_to_rt(text: str, url_map: dict[str, str] | None = None) -> list[dict]:
    rt = []

    def plain(s, **ann):
        for part in _chunks(s):
            item = {"type": "text", "text": {"content": part}}
            if ann.get("link"):
                item["text"]["link"] = {"url": ann.pop("link")}
            if ann:
                item["annotations"] = {k: True for k in ann}
            rt.append(item)

    pos = 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            plain(text[pos:m.start()])
        tok = m.group(0)
        if tok.startswith("**"):
            plain(tok[2:-2], bold=True)
        elif tok.startswith("~~"):
            plain(tok[2:-2], strikethrough=True)
        elif tok.startswith("`"):
            plain(tok[1:-1], code=True)
        elif tok.startswith("["):
            lm = re.fullmatch(r"\[([^\]]*)\]\(([^)]+)\)", tok)
            label, target = lm.group(1), lm.group(2)
            if "://" not in target and url_map and target in url_map:
                target = url_map[target]
            if "://" in target:
                plain(label, link=target)
            else:
                plain(label)  # unresolvable local link -> plain text
        elif tok.startswith("*"):
            plain(tok[1:-1], italic=True)
        pos = m.end()
    if pos < len(text):
        plain(text[pos:])
    return rt


def md_to_blocks(md: str, url_map: dict[str, str] | None = None) -> list[dict]:
    blocks: list[dict] = []
    lines = md.split("\n")
    i = 0

    def rt(s):
        return md_inline_to_rt(s, url_map)

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            lang = s[3:].strip() or "plain text"
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            blocks.append({"type": "code", "code": {
                "language": lang if lang in CODE_LANGS else "plain text",
                "rich_text": [{"type": "text", "text": {"content": c}} for c in _chunks("\n".join(body))]}})
            continue
        m = re.match(r"(#{1,6})\s+(.*)", s)
        if m:
            lvl = min(3, len(m.group(1)))
            blocks.append({"type": f"heading_{lvl}", f"heading_{lvl}": {"rich_text": rt(m.group(2))}})
            i += 1
            continue
        if s in ("---", "***", "___"):
            blocks.append({"type": "divider", "divider": {}})
            i += 1
            continue
        if s.startswith(">"):
            blocks.append({"type": "quote", "quote": {"rich_text": rt(s.lstrip("> "))}})
            i += 1
            continue
        m = re.match(r"[-*]\s+\[([ xX])\]\s+(.*)", s)
        if m:
            blocks.append({"type": "to_do", "to_do": {
                "checked": m.group(1).lower() == "x", "rich_text": rt(m.group(2))}})
            i += 1
            continue
        m = re.match(r"[-*]\s+(.*)", s)
        if m:
            blocks.append({"type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": rt(m.group(1))}})
            i += 1
            continue
        m = re.match(r"\d+\.\s+(.*)", s)
        if m:
            blocks.append({"type": "numbered_list_item",
                           "numbered_list_item": {"rich_text": rt(m.group(1))}})
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s\-:|]+\|?$", lines[i + 1].strip()):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.match(r"^\|[\s\-:|]+\|?$", lines[i].strip()):
                    cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                    rows.append(cells)
                i += 1
            width = max(len(r) for r in rows)
            blocks.append({"type": "table", "table": {
                "table_width": width, "has_column_header": True, "has_row_header": False,
                "children": [{"type": "table_row", "table_row": {
                    "cells": [rt(r[c]) if c < len(r) else [] for c in range(width)]}} for r in rows]}})
            continue
        m = re.match(r"!\[([^\]]*)\]\((https?://[^)]+)\)", s)
        if m:
            blocks.append({"type": "image", "image": {
                "type": "external", "external": {"url": m.group(2)},
                "caption": rt(m.group(1))}})
            i += 1
            continue
        # paragraph: merge soft-wrapped lines
        para = [s]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt or re.match(r"(#{1,6}\s|```|>|[-*]\s|\d+\.\s|\||---$)", nxt):
                break
            para.append(nxt)
            i += 1
        blocks.append({"type": "paragraph", "paragraph": {"rich_text": rt(" ".join(para))}})
    return blocks


CODE_LANGS = {
    "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript", "c++", "c#", "css",
    "dart", "diff", "docker", "elixir", "elm", "erlang", "flow", "fortran", "f#", "gherkin",
    "glsl", "go", "graphql", "groovy", "haskell", "html", "java", "javascript", "json",
    "julia", "kotlin", "latex", "less", "lisp", "livescript", "lua", "makefile", "markdown",
    "markup", "matlab", "mermaid", "nix", "objective-c", "ocaml", "pascal", "perl", "php",
    "plain text", "powershell", "prolog", "protobuf", "python", "r", "reason", "ruby",
    "rust", "sass", "scala", "scheme", "scss", "shell", "sql", "swift", "typescript",
    "vb.net", "verilog", "vhdl", "visual basic", "webassembly", "xml", "yaml",
}
