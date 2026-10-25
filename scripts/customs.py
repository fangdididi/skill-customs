#!/usr/bin/env python3
"""customs.py - inspect a third-party agent skill folder before it is allowed in.

Standard library only. Subcommands:

  inspect  PATH [--declared CAPS] [--waive IDS] [--rev REV] [--json]
           Inventory every file, hash it, list capabilities and red flags,
           compare capabilities with what the skill's purpose needs, and
           propose a verdict: CLEAR / CLEAR-WITH-CONDITIONS / HOLD / REFUSE.
  stamp    PATH --ledger FILE --source URL --rev REV --verdict V [--declared CAPS]
           Record what was approved (per-file sha256, tree digest, capabilities).
  recheck  PATH --ledger FILE
           Compare an updated copy against its stamp: SAME / TEXT-ONLY / REINSPECT.

The script never runs, imports or executes anything from the inspected folder.
It reads bytes, nothing else. A clean result is not proof of safety; it only
means none of the listed patterns were seen.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata

CAPABILITIES = {
    "NET": "talks to the network",
    "SHELL": "runs shell commands or spawns processes",
    "SECRETS": "touches credentials, keys, tokens or the environment",
    "WIDE_FS": "reads or writes outside its own folder",
    "INSTALL": "installs packages or tools",
    "PERSIST": "changes startup files, hooks, schedulers or agent settings",
    "ELEVATE": "asks for root or loosens permissions",
    "MCP": "calls MCP / external agent tools",
}

CAP_PATTERNS = {
    "NET": [r"https?://", r"\bcurl\b", r"\bwget\b", r"\brequests\.(get|post|put)\b",
            r"\burllib\b", r"\bfetch\(", r"\bsocket\.", r"\bhttpx\b", r"\baxios\b",
            r"Invoke-WebRequest", r"\bnc\s+-"],
    "SHELL": [r"\bsubprocess\b", r"\bos\.system\b", r"\bos\.popen\b", r"\bchild_process\b",
              r"\bexecSync\b", r"\bbash\s+-c\b", r"\bsh\s+-c\b", r"\bpowershell\b",
              r"\beval\(", r"\bexec\("],
    "SECRETS": [r"\.ssh\b", r"\bid_(rsa|ed25519)\b", r"(^|[\s/'\"])\.env\b", r"\.aws/",
                r"\bcredentials?\b", r"\bkeychain\b", r"\bprintenv\b", r"os\.environ\b",
                r"process\.env\b", r"\b[A-Z][A-Z0-9_]*_(TOKEN|SECRET|API_KEY|PASSWORD)\b",
                r"\.npmrc\b", r"\.netrc\b", r"\bcookies?\.(sqlite|db)\b", r"\bwallet\b"],
    "WIDE_FS": [r"~/", r"\$HOME\b", r"\bexpanduser\(", r"(^|[\s'\"(])/etc/", r"\.\./",
                r"\bos\.walk\(\s*['\"]/", r"\bglob\([^)]*\*\*", r"(^|[\s'\"])/Users/",
                r"(^|[\s'\"])/home/", r"C:\\\\Users"],
    "INSTALL": [r"\bpip3?\s+install\b", r"\bnpm\s+(i|install)\b", r"\bnpx\s", r"\byarn\s+add\b",
                r"\bbrew\s+install\b", r"\bapt(-get)?\s+install\b", r"\bcargo\s+install\b",
                r"\bgo\s+install\b", r"\buvx?\s"],
    "PERSIST": [r"\bcrontab\b", r"\.bashrc\b", r"\.zshrc\b", r"\.profile\b", r"\bLaunchAgents\b",
                r"\bsystemctl\s+enable\b", r"\.git/hooks\b", r"\.claude/settings", r"\bhooks\.json\b",
                r"\.cursor/rules\b", r"\bAGENTS\.md\b", r"\bCLAUDE\.md\b"],
    "ELEVATE": [r"\bsudo\b", r"\bchmod\s+(-R\s+)?777\b", r"\bchown\b", r"\brunas\b",
                r"--dangerously", r"\bsetuid\b"],
    "MCP": [r"\bmcp__\w+", r"\bmcpServers\b", r"\bmcp\.json\b"],
}

# Red flags: (code, severity, regexes). REFUSE-level codes are listed in HARD.
SAY_PATTERNS = {
    "OVERRIDE": [r"ignore (all |any )?(previous|prior|above|earlier|other) (instructions|rules)",
                 r"disregard (the |your )?(system|safety|previous)", r"override (the |your )?system prompt",
                 r"you are no longer", r"忽略(之前|以上|所有)的?(指令|规则|指示)"],
    "CONCEAL": [r"(do not|don't|never) (tell|mention|show|inform|reveal)[^.\n]{0,30}\buser",
                r"without (telling|informing|asking) the user", r"\bsilently\b", r"\bquietly\b",
                r"hide (this|these|it) from", r"不要(告诉|提示|告知)用户", r"静默(执行|运行|上传)"],
    "AUTOAPPROVE": [r"skip (the )?(confirmation|permission|approval)", r"auto-?approve",
                    r"always (allow|approve|say yes)", r"\byolo\b", r"--yes-to-all",
                    r"no need to (ask|confirm)", r"跳过确认"],
    "ANTIREFUSAL": [r"never refuse", r"always comply", r"no restrictions", r"do anything now",
                    r"绝不拒绝"],
    "REMOTE_EXEC": [r"(curl|wget)[^|\n]*\|\s*(sudo\s+)?(ba|z)?sh\b", r"\biex\s*\(", r"\|\s*iex\b",
                    r"(curl|wget)[^|\n]*\|\s*python3?\b", r"exec\(\s*(urlopen|requests\.get)"],
    "DECODE_EXEC": [r"base64\s+(-d|--decode)[^|\n]*\|\s*(ba|z)?sh\b",
                    r"(exec|eval)\(\s*(base64\.b64decode|bytes\.fromhex|codecs\.decode|atob)\(",
                    r"\bmarshal\.loads\b", r"\bFunction\(\s*atob\("],
}
ACTION_VERBS = (r"\b(send|post|upload|submit|report|forward|sync|fetch|download|call|ping|beacon)\b"
                r"|发送|上传|提交|同步|下载")
BROAD_TRIGGER = [r"\bany (task|request|question)\b", r"\balways use\b", r"\bevery (task|request|message)\b",
                 r"\ball (tasks|requests)\b", r"\bfor everything\b", r"任何(任务|请求|问题)", r"总是使用"]

HARD = {"OVERRIDE", "CONCEAL", "REMOTE_EXEC", "DECODE_EXEC", "TAG_CHARS", "BIDI",
        "BYTECODE", "LINK_OUT", "EXFIL_PATH"}
SOFT = {"AUTOAPPROVE", "ANTIREFUSAL", "ZERO_WIDTH", "HTML_COMMENT", "PADDING", "BLOB",
        "BINARY", "LINK", "NO_FRONTMATTER", "BROAD_TRIGGER", "NAME_MISMATCH",
        "UNDECLARED", "UNPINNED", "HIDDEN_FILE", "BIG_FILE"}

TEXT_EXT = {".md", ".txt", ".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".json",
            ".yaml", ".yml", ".toml", ".ini", ".cfg", ".ps1", ".rb", ".go", ".rs", ".html",
            ".css", ".xml", ".csv", ".env", ".lua", ".pl", ".php", ""}
ZW = set("\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064\u180e")
BIDI = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
BIG = 1_000_000
SKIP_DIRS = {".git"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def walk(root):
    """Yield (relpath, abspath) for every entry, symlinks included, .git skipped."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        keep = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            if d in SKIP_DIRS:
                continue
            if os.path.islink(full):
                filenames.append(d)
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            yield os.path.relpath(full, root).replace(os.sep, "/"), full


def looks_binary(data):
    if not data:
        return False
    if b"\x00" in data[:8192]:
        return True
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def parse_frontmatter(text):
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    fm = {}
    key = None
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            key = m.group(1)
            fm[key] = m.group(2).strip().strip("'\"")
        elif key and line.startswith((" ", "\t")):
            fm[key] = (fm[key] + " " + line.strip()).strip()
    return fm


def find(code, rel, lineno, text, sev=None):
    return {"id": f"{code}:{rel}:{lineno}", "code": code, "file": rel, "line": lineno,
            "text": text.strip()[:160], "severity": sev or ("hard" if code in HARD else "soft")}


def cap_hits(line):
    hits = set()
    for cap, pats in CAP_PATTERNS.items():
        flags = 0 if cap == "SECRETS" else re.IGNORECASE
        if any(re.search(p, line, flags) for p in pats):
            hits.add(cap)
    return hits


def scan_text(rel, text):
    """Return (caps, flags). caps maps CAP -> list of (location, strong).

    In prose (markdown outside code fences) a bare URL only counts as NET when the
    line also asks for an action (send, post, upload, fetch ...); prose mentions are
    'weak' and do not build a leak path on their own. Scripts and fenced code are
    always 'strong'.
    """
    caps, flags = {}, []
    is_md = rel.lower().endswith((".md", ".markdown", ".txt"))
    lines = text.splitlines()
    blank_run = 0
    in_fence = False
    for i, line in enumerate(lines, 1):
        if is_md and line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        prose = is_md and not in_fence and not line.startswith("    ")
        hits = cap_hits(line)
        if prose and "NET" in hits:
            only_url = not any(re.search(p, line, re.IGNORECASE) for p in CAP_PATTERNS["NET"][1:])
            if only_url and not re.search(ACTION_VERBS, line, re.IGNORECASE):
                hits.discard("NET")
        strong = (not prose) or ({"SECRETS", "NET"} <= hits)
        for cap in hits:
            caps.setdefault(cap, []).append((f"{rel}:{i}", strong))
        for code, pats in SAY_PATTERNS.items():
            if any(re.search(p, line, re.IGNORECASE) for p in pats):
                flags.append(find(code, rel, i, line))
        chars = set(line)
        if chars & BIDI:
            flags.append(find("BIDI", rel, i, repr(line)))
        if chars & ZW or ("\ufeff" in line and not (i == 1 and line.startswith("\ufeff"))):
            flags.append(find("ZERO_WIDTH", rel, i, repr(line)))
        if any(0xE0000 <= ord(c) <= 0xE007F for c in line):
            flags.append(find("TAG_CHARS", rel, i, repr(line)))
        if re.search(r"\S {120,}\S|\t{40,}", line) or re.search(r"\S\s{200,}$", line):
            flags.append(find("PADDING", rel, i, line[:40] + " ...[long whitespace]"))
        if re.search(r"[A-Za-z0-9+/=]{120,}", line) and not re.search(r"https?://\S{120,}", line):
            flags.append(find("BLOB", rel, i, line[:60] + "..."))
        blank_run = blank_run + 1 if not line.strip() else 0
        if blank_run == 40:
            flags.append(find("PADDING", rel, i, "40+ blank lines in a row"))
    if is_md:
        for m in re.finditer(r"<!--(.*?)-->", text, re.DOTALL):
            body = m.group(1).strip()
            if body:
                lineno = text.count("\n", 0, m.start()) + 1
                flags.append(find("HTML_COMMENT", rel, lineno, "<!-- " + body.replace("\n", " ")))
    return caps, flags


def inspect(root, declared=(), waive=(), rev=None):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise SystemExit(f"not a folder: {root}")
    files, caps, flags = [], {}, []
    skill_md = None
    for rel, full in walk(root):
        if os.path.islink(full):
            target = os.path.realpath(full)
            inside = target == root or target.startswith(root + os.sep)
            flags.append(find("LINK" if inside else "LINK_OUT", rel, 0, "-> " + os.readlink(full)))
            files.append({"path": rel, "sha256": None, "size": 0, "kind": "symlink"})
            continue
        with open(full, "rb") as fh:
            data = fh.read()
        entry = {"path": rel, "sha256": sha256(data), "size": len(data), "kind": "text"}
        name = rel.rsplit("/", 1)[-1]
        if name.startswith(".") and name not in (".gitignore", ".gitattributes"):
            flags.append(find("HIDDEN_FILE", rel, 0, name))
        if rel.endswith((".pyc", ".pyo")) or "__pycache__/" in rel:
            flags.append(find("BYTECODE", rel, 0, "compiled bytecode cannot be read"))
            entry["kind"] = "binary"
        elif looks_binary(data):
            flags.append(find("BINARY", rel, 0, f"{len(data)} bytes, not readable text"))
            entry["kind"] = "binary"
        else:
            if len(data) > BIG:
                flags.append(find("BIG_FILE", rel, 0, f"{len(data)} bytes"))
            text = data.decode("utf-8")
            c, f = scan_text(rel, text)
            for k, v in c.items():
                caps.setdefault(k, []).extend(v)
            flags.extend(f)
            if rel == "SKILL.md":
                skill_md = text
        files.append(entry)

    frontmatter = parse_frontmatter(skill_md.lstrip("\ufeff")) if skill_md is not None else None
    folder = os.path.basename(root.rstrip(os.sep))
    if skill_md is None or frontmatter is None or not frontmatter.get("description"):
        flags.append(find("NO_FRONTMATTER", "SKILL.md", 1, "missing SKILL.md, frontmatter or description"))
    else:
        desc = frontmatter.get("description", "")
        if len(desc) < 25 or any(re.search(p, desc, re.IGNORECASE) for p in BROAD_TRIGGER):
            flags.append(find("BROAD_TRIGGER", "SKILL.md", 1, "description: " + desc))
        nm = frontmatter.get("name")
        if nm and nm != folder:
            flags.append(find("NAME_MISMATCH", "SKILL.md", 1, f"name '{nm}' vs folder '{folder}'"))

    strong = {k: [loc for loc, st in v if st] for k, v in caps.items()}
    private = strong.get("SECRETS") or strong.get("WIDE_FS")
    if private and strong.get("NET"):
        rel_, line_ = private[0].rsplit(":", 1)
        flags.append(find("EXFIL_PATH", rel_, int(line_),
                          "reads private data AND talks to the network: a complete leak path"))
    caps = {k: [loc for loc, _ in v] for k, v in caps.items()}
    declared = {d.strip().upper() for d in declared if d.strip()}
    undeclared = sorted(set(caps) - declared)
    for cap in undeclared:
        rel_, line_ = caps[cap][0].rsplit(":", 1)
        flags.append(find("UNDECLARED", rel_, int(line_),
                          f"{cap} ({CAPABILITIES[cap]}) is not needed by the stated purpose"))
    if not rev:
        flags.append(find("UNPINNED", "-", 0, "no exact revision recorded (commit sha / tag / archive hash)"))
    waive = set(waive)
    active = [f for f in flags if f["id"] not in waive]
    tree = sha256("\n".join(f"{x['path']}\0{x['sha256']}" for x in sorted(files, key=lambda x: x["path"])).encode())
    return {
        "folder": folder, "rev": rev, "tree_sha256": tree, "files": files,
        "frontmatter": frontmatter or {}, "capabilities": {k: list(dict.fromkeys(v)) for k, v in sorted(caps.items())},
        "declared": sorted(declared), "undeclared": undeclared, "findings": active,
        "waived": sorted(f["id"] for f in flags if f["id"] in waive),
        "verdict": verdict(active, caps, declared),
    }


def verdict(findings, caps, declared):
    codes = {f["code"] for f in findings}
    if codes & HARD:
        return "REFUSE"
    if codes & SOFT:
        return "HOLD"
    risky = set(caps) & {"NET", "SHELL", "SECRETS", "WIDE_FS", "INSTALL", "PERSIST", "ELEVATE", "MCP"}
    return "CLEAR-WITH-CONDITIONS" if risky else "CLEAR"


def render(rep):
    out = [f"# Customs report: {rep['folder']}", ""]
    out.append(f"revision: {rep['rev'] or '(none)'}   tree sha256: {rep['tree_sha256'][:16]}...")
    out.append(f"files: {len(rep['files'])}   declared: {', '.join(rep['declared']) or '(none)'}")
    fm = rep["frontmatter"]
    out.append(f"name: {fm.get('name', '-')}   allowed-tools: {fm.get('allowed-tools', '-')}")
    out.append(f"description: {fm.get('description', '-')}")
    out += ["", "## Capabilities found"]
    if not rep["capabilities"]:
        out.append("(none)")
    for cap, where in rep["capabilities"].items():
        mark = "  UNDECLARED" if cap in rep["undeclared"] else ""
        out.append(f"- {cap}: {CAPABILITIES[cap]} [{', '.join(where[:4])}{' ...' if len(where) > 4 else ''}]{mark}")
    out += ["", "## Findings"]
    if not rep["findings"]:
        out.append("(none)")
    for sev in ("hard", "soft"):
        for f in rep["findings"]:
            if f["severity"] == sev:
                out.append(f"- [{sev.upper()}] {f['code']} {f['file']}:{f['line']}  {f['text']}")
    if rep["waived"]:
        out += ["", "## Waived", *[f"- {w}" for w in rep["waived"]]]
    out += ["", f"## Proposed verdict: {rep['verdict']}",
            "A clean scan is not proof of safety. Read every file before you agree."]
    return "\n".join(out)


def load_ledger(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"entries": {}}


def stamp(root, ledger_path, source, rev, verdict_, declared=(), waive=()):
    if verdict_ not in ("CLEAR", "CLEAR-WITH-CONDITIONS"):
        raise SystemExit(f"refusing to stamp a skill with verdict {verdict_}")
    rep = inspect(root, declared, waive, rev=rev)
    if rep["verdict"] in ("HOLD", "REFUSE"):
        open_ = ", ".join(f["id"] for f in rep["findings"])
        raise SystemExit(f"refusing to stamp: inspection says {rep['verdict']} ({open_}); "
                         "resolve or explicitly --waive each finding first")
    ledger = load_ledger(ledger_path)
    ledger["entries"][rep["folder"]] = {
        "source": source, "rev": rev, "verdict": verdict_, "tree_sha256": rep["tree_sha256"],
        "files": {f["path"]: f["sha256"] for f in rep["files"]},
        "capabilities": sorted(rep["capabilities"]), "declared": rep["declared"],
        "flags": sorted({f["code"] for f in rep["findings"]} | {w.split(":")[0] for w in rep["waived"]}),
        "waived": rep["waived"],
    }
    with open(ledger_path, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return ledger["entries"][rep["folder"]]


def recheck(root, ledger_path):
    ledger = load_ledger(ledger_path)
    folder = os.path.basename(os.path.abspath(root).rstrip(os.sep))
    old = ledger["entries"].get(folder)
    if not old:
        return {"status": "UNKNOWN", "reason": f"{folder} was never stamped; run a full inspection"}
    rep = inspect(root, old.get("declared", []), rev="recheck")
    new_files = {f["path"]: f["sha256"] for f in rep["files"]}
    added = sorted(set(new_files) - set(old["files"]))
    removed = sorted(set(old["files"]) - set(new_files))
    changed = sorted(p for p in set(new_files) & set(old["files"]) if new_files[p] != old["files"][p])
    new_caps = sorted(set(rep["capabilities"]) - set(old["capabilities"]))
    new_flags = sorted({f["code"] for f in rep["findings"]} - set(old["flags"]) - {"UNPINNED"})
    code_like = [p for p in added + changed if not p.lower().endswith((".md", ".txt"))]
    if not (added or removed or changed):
        status = "SAME"
    elif new_caps or new_flags or code_like:
        status = "REINSPECT"
    else:
        status = "TEXT-ONLY"
    return {"status": status, "added": added, "removed": removed, "changed": changed,
            "new_capabilities": new_caps, "new_flags": new_flags, "code_changed": code_like}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("inspect")
    a.add_argument("path")
    a.add_argument("--declared", default="", help="comma list of capabilities the purpose needs")
    a.add_argument("--waive", default="", help="comma list of finding ids you have read and accept")
    a.add_argument("--rev", default=None)
    a.add_argument("--json", action="store_true")
    s = sub.add_parser("stamp")
    s.add_argument("path")
    s.add_argument("--ledger", required=True)
    s.add_argument("--source", required=True)
    s.add_argument("--rev", required=True)
    s.add_argument("--verdict", required=True)
    s.add_argument("--declared", default="")
    s.add_argument("--waive", default="")
    r = sub.add_parser("recheck")
    r.add_argument("path")
    r.add_argument("--ledger", required=True)
    args = ap.parse_args(argv)
    split = lambda s: [x for x in s.split(",") if x.strip()]
    if args.cmd == "inspect":
        rep = inspect(args.path, split(args.declared), split(args.waive), args.rev)
        print(json.dumps(rep, indent=2, ensure_ascii=False) if args.json else render(rep))
        return {"CLEAR": 0, "CLEAR-WITH-CONDITIONS": 0, "HOLD": 1, "REFUSE": 2}[rep["verdict"]]
    if args.cmd == "stamp":
        e = stamp(args.path, args.ledger, args.source, args.rev, args.verdict, split(args.declared),
                  split(args.waive))
        print(f"stamped {os.path.basename(os.path.abspath(args.path))} tree {e['tree_sha256'][:16]}... "
              f"({len(e['files'])} files, caps: {', '.join(e['capabilities']) or 'none'})")
        return 0
    res = recheck(args.path, args.ledger)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return {"SAME": 0, "TEXT-ONLY": 0}.get(res["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
