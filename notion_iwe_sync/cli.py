"""notion-iwe — mirror a personal Notion workspace as an IWE markdown vault.

pull   Notion -> vault (remote wins; changed local copies are backed up to .conflicts/)
push   vault -> Notion (local wins; changed remote copies are backed up to .conflicts/)
sync   pull then push
watch  daemon: push on file save, pull every N seconds
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import sys
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .notion import Notion, blocks_to_md, md_to_blocks, page_title

CONFIG = Path.home() / ".config/notion-iwe/config.toml"
STATE = Path.home() / ".local/state/notion-iwe/state.json"
LOCK = STATE.parent / "lock"


@contextmanager
def locked():
    """Cross-process mutex around any pull/push.

    The watch daemon and a manual CLI invocation may run at the same time; without
    this, both replace the same page's blocks concurrently and one aborts between
    delete and append, leaving the page truncated.
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def log(msg: str):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_config() -> dict:
    if not CONFIG.exists():
        sys.exit(f"missing {CONFIG} — create it with token/vault (see README)")
    cfg = tomllib.loads(CONFIG.read_text())
    cfg.setdefault("vault", str(Path.home() / "notion"))
    cfg.setdefault("pull_interval", 300)
    cfg.setdefault("new_page_parent", "")
    if "token" not in cfg:
        sys.exit("config has no token")
    return cfg


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"pages": {}}


def save_state(st: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:60] or "untitled"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def parse_front(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            meta = {}
            for ln in text[4:end].splitlines():
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    meta[k.strip()] = v.strip()
            return meta, text[end + 5:].lstrip("\n")
    return {}, text


def make_front(meta: dict) -> str:
    body = "\n".join(f"{k}: {v}" for k, v in meta.items())
    return f"---\n{body}\n---\n\n"


def split_title(body: str) -> tuple[str, str]:
    lines = body.split("\n")
    if lines and lines[0].startswith("# "):
        return lines[0][2:].strip(), "\n".join(lines[1:]).lstrip("\n")
    return "", body


def backup_conflict(vault: Path, name: str, content: str):
    d = vault / ".conflicts"
    d.mkdir(exist_ok=True)
    p = d / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.md"
    p.write_text(content)
    log(f"  ⚠ conflict copy saved: {p.relative_to(vault)}")


# --------------- pull ---------------

def pull(api: Notion, cfg: dict, st: dict):
    vault = Path(cfg["vault"])
    vault.mkdir(parents=True, exist_ok=True)
    pages = list(api.search_pages())
    # link map: notion id (no dashes) -> local key (filename without extension)
    link_map = {}
    file_of = {}
    for p in pages:
        pid = p["id"].replace("-", "")
        known = st["pages"].get(p["id"])
        fname = known["file"] if known else f"{slugify(page_title(p))}-{pid[-8:]}.md"
        link_map[pid] = fname[:-3]
        file_of[p["id"]] = fname
    changed = 0
    for p in pages:
        pid, fname = p["id"], file_of[p["id"]]
        fpath = Path(cfg["vault"]) / fname
        known = st["pages"].get(pid)
        if known and known.get("last_edited") == p["last_edited_time"] and fpath.exists():
            continue
        title = page_title(p)
        md_lines = blocks_to_md(api, pid, link_map)
        content = (make_front({"notion-id": pid, "notion-url": p.get("url", "")})
                   + f"# {title}\n\n" + "\n".join(md_lines)).rstrip() + "\n"
        if fpath.exists() and known and sha(fpath.read_text()) != known.get("hash"):
            backup_conflict(vault, fpath.stem + "-local", fpath.read_text())
        fpath.write_text(content)
        st["pages"][pid] = {"file": fname, "last_edited": p["last_edited_time"],
                            "hash": sha(content)}
        changed += 1
        log(f"  ← {fname}  ({title})")
    save_state(st)
    log(f"pull done — {changed} page(s) updated, {len(pages)} total")


# --------------- push ---------------

def local_files(vault: Path):
    return sorted(p for p in vault.glob("*.md") if not p.name.startswith("."))


def changed_files(cfg: dict, st: dict, force: bool = False) -> list[Path]:
    vault = Path(cfg["vault"])
    if force:
        return local_files(vault)
    by_file = {v["file"]: (k, v) for k, v in st["pages"].items()}
    out = []
    for f in local_files(vault):
        known = by_file.get(f.name)
        if not known or sha(f.read_text()) != known[1].get("hash"):
            out.append(f)
    return out


def push_file(api: Notion, cfg: dict, st: dict, fpath: Path) -> bool:
    vault = Path(cfg["vault"])
    text = fpath.read_text()
    meta, body = parse_front(text)
    title, md_body = split_title(body)
    title = title or fpath.stem
    # url map: local key -> notion url (for links inside pushed text)
    url_map = {v["file"][:-3]: f"https://www.notion.so/{k.replace('-', '')}"
               for k, v in st["pages"].items()}
    pid = meta.get("notion-id", "").replace("-", "") or None

    if not pid:
        parent = cfg.get("new_page_parent", "")
        if not parent:
            log(f"  ✗ {fpath.name}: no notion-id and no new_page_parent configured — skipped")
            return False
        page = api.create_page(parent, title)
        pid = page["id"].replace("-", "")
        meta = {"notion-id": pid, "notion-url": page.get("url", "")} | meta
        text = make_front(meta) + body
        fpath.write_text(text)
        log(f"  + created page for {fpath.name}")

    page = api.get_page(pid)
    if page.get("archived") or page.get("in_trash"):
        log(f"  - {fpath.name}: remote page is archived/in trash — skipped")
        return False
    known = st["pages"].get(page["id"])
    if known and known.get("last_edited") != page["last_edited_time"]:
        # remote changed since our last sync -> keep a copy of the remote before clobbering
        remote_md = f"# {page_title(page)}\n\n" + "\n".join(blocks_to_md(api, pid))
        backup_conflict(vault, fpath.stem + "-remote", remote_md)
    if page_title(page) != title:
        api.set_title(page, title)
    for b in api.children(pid):
        api.delete_block(b["id"])
    blocks = md_to_blocks(md_body, url_map)
    api.append(pid, blocks)
    remote_n = sum(1 for _ in api.children(pid))
    if remote_n != len(blocks):
        log(f"  ⚠ {fpath.name}: remote has {remote_n} top-level blocks, expected "
            f"{len(blocks)} — verify the page; `notion-iwe push --force` re-pushes it")
    page = api.get_page(pid)  # re-read for the post-push last_edited_time
    st["pages"][page["id"]] = {"file": fpath.name,
                               "last_edited": page["last_edited_time"],
                               "hash": sha(fpath.read_text())}
    save_state(st)
    log(f"  → {fpath.name}  ({title})")
    return True


def push(api: Notion, cfg: dict, st: dict, force: bool = False,
         only: list[str] | None = None):
    if only:
        vault = Path(cfg["vault"])
        files = []
        for name in only:
            f = vault / (name if name.endswith(".md") else f"{name}.md")
            if not f.exists():
                log(f"  ✗ {f.name}: not found in vault")
                continue
            files.append(f)
    else:
        files = changed_files(cfg, st, force=force)
    if not files:
        log("push — nothing changed")
        return
    for f in files:
        try:
            push_file(api, cfg, st, f)
        except Exception as e:  # keep the daemon alive
            log(f"  ✗ {f.name}: {e}")


# --------------- watch ---------------

def pull_op(api: Notion, cfg: dict):
    """Locked pull with state re-read, safe against concurrent CLI/daemon runs."""
    with locked():
        pull(api, cfg, load_state())


def push_op(api: Notion, cfg: dict, force: bool = False,
            only: list[str] | None = None):
    """Locked push with state re-read, safe against concurrent CLI/daemon runs."""
    with locked():
        push(api, cfg, load_state(), force=force, only=only)


def watch(api: Notion, cfg: dict):
    vault = Path(cfg["vault"])
    interval = int(cfg["pull_interval"])
    log(f"watching {vault} (push on save, pull every {interval}s)")
    try:
        pull_op(api, cfg)
    except Exception as e:
        log(f"initial pull failed: {e}")
    last_pull = time.time()
    mtimes = {f: f.stat().st_mtime for f in local_files(vault)}
    dirty_since = None
    while True:
        time.sleep(2)
        now = {f: f.stat().st_mtime for f in local_files(vault)}
        if now != mtimes:
            mtimes = now
            dirty_since = time.time()
        if dirty_since and time.time() - dirty_since > 3:  # debounce: quiet for 3s
            dirty_since = None
            try:
                push_op(api, cfg)
            except Exception as e:
                log(f"push failed: {e}")
            mtimes = {f: f.stat().st_mtime for f in local_files(vault)}
        if time.time() - last_pull >= interval:
            last_pull = time.time()
            try:
                pull_op(api, cfg)
            except Exception as e:
                log(f"pull failed: {e}")
            mtimes = {f: f.stat().st_mtime for f in local_files(vault)}


def main():
    ap = argparse.ArgumentParser(prog="notion-iwe", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["pull", "push", "sync", "watch", "status"])
    ap.add_argument("files", nargs="*",
                    help="push: vault file name(s) to push unconditionally, "
                         "e.g. `notion-iwe push my-page.md` (heals a single page)")
    ap.add_argument("--force", action="store_true",
                    help="push: re-push every local file even if the state hash says "
                         "it is unchanged (heals local/remote drift)")
    args = ap.parse_args()
    cfg = load_config()
    api = Notion(cfg["token"])
    if args.cmd == "pull":
        pull_op(api, cfg)
    elif args.cmd == "push":
        push_op(api, cfg, force=args.force, only=args.files or None)
    elif args.cmd == "sync":
        pull_op(api, cfg)
        push_op(api, cfg, force=args.force)
    elif args.cmd == "watch":
        watch(api, cfg)
    elif args.cmd == "status":
        st = load_state()
        files = changed_files(cfg, st)
        print(f"vault: {cfg['vault']}")
        print(f"tracked pages: {len(st['pages'])}")
        print(f"locally changed (would push): {len(files)}")
        for f in files:
            print(f"  {f.name}")


if __name__ == "__main__":
    main()
