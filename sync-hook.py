#!/usr/bin/env python3
"""Claude Code `Stop` hook — appends this turn's plain conversational text
(no tool calls/thinking/tool results) from the session transcript to
entries.json in this repo, then commits+pushes so the GitHub Pages site
picks it up. Registered only in this project's
.claude/settings.local.json (gitignored globally, not shared with the
coordinator repo) — see the reference_correspondence_log memory for why.

Always exits 0: a sync failure must never block Claude from finishing a
turn. Errors go to .sync.log for debugging, not stderr/stdout.
"""
import json
import os
import re
import subprocess
import sys
from html import escape

REPO = os.path.dirname(os.path.abspath(__file__))
ENTRIES_PATH = os.path.join(REPO, "entries.json")
STATE_PATH = os.path.join(REPO, ".sync-state.json")
LOG_PATH = os.path.join(REPO, ".sync.log")


def log(msg):
    with open(LOG_PATH, "a") as f:
        f.write(msg + "\n")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def text_to_html(text):
    # Pull out fenced code blocks first so backticks inside them are never
    # treated as inline-code markers.
    blocks = []

    def stash_fence(m):
        blocks.append(m.group(1))
        return "\x00FENCE%d\x00" % (len(blocks) - 1)

    text = re.sub(r"```(?:\w+\n)?(.*?)```", stash_fence, text, flags=re.S)

    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    html_parts = []
    for p in parts:
        fence_match = re.fullmatch(r"\x00FENCE(\d+)\x00", p)
        if fence_match:
            code = blocks[int(fence_match.group(1))]
            html_parts.append("<pre><code>%s</code></pre>" % escape(code))
            continue
        p = escape(p)
        p = re.sub(r"`([^`]+)`", r'<code class="inline">\1</code>', p)
        p = p.replace("\n", "<br>")
        html_parts.append("<p>%s</p>" % p)
    return "".join(html_parts)


def extract_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n\n".join(t for t in texts if t.strip())


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log("bad hook input: %r" % e)
        return 0
    transcript_path = payload.get("transcript_path")
    if not transcript_path or not os.path.exists(transcript_path):
        return 0

    state = load_json(STATE_PATH, {})
    last_line = state.get(transcript_path, 0)

    with open(transcript_path) as f:
        lines = f.readlines()

    new_entries = []
    for line in lines[last_line:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("isSidechain"):
            continue
        rec_type = rec.get("type")
        if rec_type not in ("user", "assistant"):
            continue
        message = rec.get("message") or {}
        text = extract_text(message)
        if not text:
            continue
        who = "ihar" if rec_type == "user" else "claude"
        new_entries.append({"type": "msg", "who": who, "html": text_to_html(text)})

    state[transcript_path] = len(lines)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)

    if not new_entries:
        return 0

    entries = load_json(ENTRIES_PATH, [])
    entries.extend(new_entries)
    with open(ENTRIES_PATH, "w") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    try:
        subprocess.run(["git", "add", "entries.json"], cwd=REPO, check=True, capture_output=True)
        commit = subprocess.run(
            ["git", "commit", "-m", "sync: +%d entries" % len(new_entries)],
            cwd=REPO, capture_output=True, text=True,
        )
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            log("commit failed: %s" % commit.stdout + commit.stderr)
            return 0
        push = subprocess.run(["git", "push"], cwd=REPO, capture_output=True, text=True)
        if push.returncode != 0:
            log("push failed: %s" % push.stderr)
    except Exception as e:
        log("git step raised: %r" % e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
