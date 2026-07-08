# notion-iwe

**Mirror a personal Notion workspace as a local markdown vault — and keep the two in sync, in both directions.**

Built for [IWE](https://iwe.md) (the markdown LSP / knowledge-graph tool), but the vault is
just plain markdown files: any editor works, and so does any tool that speaks markdown
(Obsidian, grep, an AI agent…).

```
Notion page  ⇄  ~/notion/<slug>-<id8>.md
```

- Edit a file in Neovim → it lands in Notion a few seconds after you save.
- Edit a page in Notion → it lands on disk on the next pull (default: every 5 minutes).
- Neither side is ever silently lost: on a conflict, the losing version is backed up
  to `.conflicts/` with a timestamp.

---

## Install

From a checkout, the installer sets up everything (CLI, config template, systemd user unit):

```bash
./install.sh            # install; prints the remaining manual steps
./install.sh --enable   # install and start the watch daemon right away
```

Or by hand:

```bash
uv tool install notion-iwe-sync        # or: pipx install notion-iwe-sync
# from a checkout:
uv tool install -e .
```

Python ≥ 3.11. Single runtime dependency: `requests`.

## Notion setup

1. Create an **internal integration** at <https://www.notion.so/my-integrations>
   (read + update + insert content capabilities) and copy its secret (`ntn_…`).
2. Share the pages you want mirrored with the integration — sharing a top-level
   page includes everything under it.

## Configuration — `~/.config/notion-iwe/config.toml`

```toml
token = "ntn_..."          # the integration secret — chmod 600 this file, never commit it
vault = "/home/you/notion" # where the markdown mirror lives
pull_interval = 300        # seconds between pulls in watch mode
new_page_parent = ""       # Notion page id under which NEW local .md files are
                           # created as pages; empty = new local files are skipped
```

If the vault should be an IWE workspace, drop your `.iwe/config.toml` inside it —
`notion-iwe` doesn't care, it only touches `*.md` at the vault root.

## CLI

| Command | What it does |
| --- | --- |
| `notion-iwe pull` | Notion → vault. Fetches every page the integration can see; only pages whose `last_edited_time` changed are rewritten. **Remote wins** — if the local copy also changed, it is backed up to `.conflicts/` first. |
| `notion-iwe push` | vault → Notion. Pushes every file whose content hash differs from the last sync. **Local wins** — if the remote page also changed, its version is backed up to `.conflicts/` first. `--force` re-pushes every local file regardless of the hash, and `notion-iwe push <file.md> [...]` re-pushes just the named file(s) — both heal local/remote drift. Pages archived (trashed) on the Notion side are skipped. |
| `notion-iwe sync` | `pull` then `push`. |
| `notion-iwe watch` | Daemon: pushes ~3 s after a file is saved (debounced), pulls every `pull_interval` seconds. |
| `notion-iwe status` | Shows the vault path, tracked page count, and which files would be pushed. |

### Examples

```console
$ notion-iwe pull
[18:09:19]   ← groceries-d36c19b1.md  (Groceries)
[18:09:19] pull done — 2 page(s) updated, 59 total

$ echo "- [ ] water the plants" >> ~/notion/weekly-review-a1b2c3d4.md
$ notion-iwe push
[18:12:26]   → weekly-review-a1b2c3d4.md  (Weekly review)

$ notion-iwe status
vault: /home/you/notion
tracked pages: 59
locally changed (would push): 0
```

## How it works

**Mapping.** One Notion page ↔ one file, named `<slug>-<last-8-of-page-id>.md`. The page id
and URL live in the file's frontmatter; the first `# heading` is the page title:

```markdown
---
notion-id: 36b4c85ae57d80f7ace7db3db90a778e
notion-url: https://www.notion.so/Workflow-36b4c85ae57d80f7ace7db3db90a778e
---

# Workflow

…content…
```

**Links.** Page mentions and links between synced pages become extension-less local links
(`[Workflow](workflow-db90a778e)` — IWE's format) on pull, and are mapped back to
`notion.so` URLs on push. Links to pages that aren't synced stay as Notion URLs.

**State.** `~/.local/state/notion-iwe/state.json` records, per page: the file name, the
remote `last_edited_time`, and the local content hash. That's how each direction knows
what changed — and how a push avoids re-triggering the next pull.

**Concurrency.** Every pull/push takes an exclusive file lock
(`~/.local/state/notion-iwe/lock`) and re-reads the state file first, so the watch daemon
and a manual CLI invocation can never replace the same page's blocks at the same time
(previously that race could leave a page truncated). Block deletion is idempotent — a
block already deleted by a competing writer is not an error. After each push the block
count is verified against what was sent; on mismatch a warning suggests `push --force`.

**Conflicts.** The direction that runs decides (pull → remote wins, push → local wins),
and the other side's version is written to `<vault>/.conflicts/<name>-<timestamp>.md`.
Diff and reconcile at your leisure; nothing is lost.

**New files.** A vault file without a `notion-id` becomes a new Notion page under
`new_page_parent` (its id is written back into the frontmatter). Left unconfigured,
new files are skipped with a warning — so a stray scratch file never creates pages
by accident.

## Run it as a service (systemd user unit)

`~/.config/systemd/user/notion-iwe-sync.service`:

```ini
[Unit]
Description=Notion <-> IWE vault bidirectional sync (watch mode)
After=network-online.target

[Service]
ExecStart=%h/.local/bin/notion-iwe watch
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now notion-iwe-sync
journalctl --user -u notion-iwe-sync -f
```

## Markdown ↔ blocks coverage

Pull and push cover: paragraphs, headings 1–3, bulleted/numbered lists, to-dos, code
blocks (with language), quotes, callouts, dividers, tables, external images, bookmarks,
equations, and inline **bold** / *italic* / `code` / ~~strikethrough~~ / links.

## Limitations (v1)

- The round-trip is best-effort, not lossless: nested list levels flatten on push, and
  exotic blocks (synced blocks, columns, child databases) pull as placeholders.
- `push` **replaces** the page's blocks (the title is updated as a real Notion title, and
  comments attached to replaced blocks don't survive).
- Database *rows* sync their page content, not their properties.
- Brand-new Notion pages can take a few minutes to appear (the Notion search API indexes
  lazily); edits to existing pages show up on the next pull.
- The Notion API is rate-limited (~3 req/s); the client backs off on 429 automatically,
  so a first pull of a big workspace just takes a little while.

## License

MIT
