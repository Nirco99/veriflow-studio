"""VeriFlow Studio - IntelliSense engine (no GUI): completion context, candidates and ranking.

Kinds (VS Code style badges):  M module · P port · W wire/signal · K keyword/snippet · G gate primitive · S system task / directive
Ranking: ports after "."  >  modules + local signals  >  gates + keywords  >  system tasks / directives.
"""
from __future__ import annotations

import re

CURSOR = "‸"          # caret marker, same as ui_components.CURSOR

KIND_RANK = {"P": 0, "M": 1, "W": 1, "G": 2, "K": 2, "S": 3}
KIND_NAMES = {"M": "module", "P": "port", "W": "signal", "K": "keyword", "G": "gate", "S": "system"}

KEYWORDS = ["module", "endmodule", "input", "output", "inout", "wire", "reg", "logic", "integer", "parameter",
            "localparam", "assign", "always", "always_comb", "always_ff", "initial", "begin", "end", "if", "else",
            "case", "casez", "endcase", "default", "for", "while", "repeat", "forever", "generate", "endgenerate",
            "genvar", "function", "endfunction", "task", "endtask", "posedge", "negedge", "or", "signed", "unsigned",
            "supply0", "supply1", "tri", "defparam"]

KEYWORD_SNIPPETS = [   # (label, insert, detail)
    ("module (template)", "module " + CURSOR + "NAME (\n    output y,\n    input  a,\n    input  b\n);\n\n\n\nendmodule", "module … endmodule"),
    ("always @(*)", "always @(*) begin\n    " + CURSOR + "\nend", "combinational block"),
    ("always @(posedge clk)", "always @(posedge clk) begin\n    " + CURSOR + "\nend", "sequential block"),
    ("initial begin … end", "initial begin\n    " + CURSOR + "\nend", "testbench stimulus block"),
    ("begin … end", "begin\n    " + CURSOR + "\nend", "block"),
    ("if … else", "if (" + CURSOR + ") begin\n\nend else begin\n\nend", "if / else"),
    ("case … endcase", "case (" + CURSOR + ")\n    default: ;\nendcase", "case statement"),
    ("for loop", "for (i = 0; i < " + CURSOR + "; i = i + 1) begin\n\nend", "for loop"),
    ("generate … endgenerate", "generate\n    " + CURSOR + "\nendgenerate", "generate block"),
    ("function … endfunction", "function " + CURSOR + "NAME;\n    input a;\n    NAME = a;\nendfunction", "function"),
    ("task … endtask", "task " + CURSOR + "NAME;\nbegin\n\nend\nendtask", "task"),
]

GATES = {   # name -> pin order hint
    "and": "(out, in1, in2, …)", "nand": "(out, in1, in2, …)", "or": "(out, in1, in2, …)", "nor": "(out, in1, in2, …)",
    "xor": "(out, in1, in2, …)", "xnor": "(out, in1, in2, …)", "not": "(out, in)", "buf": "(out, in)",
    "bufif0": "(out, in, ctrl)", "bufif1": "(out, in, ctrl)", "notif0": "(out, in, ctrl)", "notif1": "(out, in, ctrl)",
}

SYSTEM_TASKS = [   # (label, insert, detail)
    ("$display", '$display("' + CURSOR + '");', "print a line"),
    ("$monitor", '$monitor("a=%b | b=%b | y=%b", ' + CURSOR + ");", "print on every change"),
    ("$strobe", '$strobe("' + CURSOR + '");', "print at end of time step"),
    ("$finish", "$finish;" + CURSOR, "end the simulation"),
    ("$stop", "$stop;" + CURSOR, "pause (acts as $finish with vvp -n)"),
    ("$dumpfile", '$dumpfile("' + CURSOR + 'wave.vcd");', "VCD output file"),
    ("$dumpvars", "$dumpvars(0, " + CURSOR + ");", "dump signals to the VCD"),
    ("$time", "$time" + CURSOR, "current simulation time"),
    ("$random", "$random" + CURSOR, "32-bit random number"),
    ("$readmemb", '$readmemb("' + CURSOR + '", mem);', "load binary memory file"),
    ("$readmemh", '$readmemh("' + CURSOR + '", mem);', "load hex memory file"),
]

DIRECTIVES = [
    ("`timescale", "`timescale 1ns/1ps" + CURSOR, "time unit / precision"),
    ("`include", '`include "' + CURSOR + '"', "include a header (.vh)"),
    ("`define", "`define " + CURSOR, "text macro"),
    ("`ifdef", "`ifdef " + CURSOR + "\n\n`endif", "conditional compilation"),
    ("`ifndef", "`ifndef " + CURSOR + "\n\n`endif", "conditional compilation"),
    ("`else", "`else" + CURSOR, ""),
    ("`endif", "`endif" + CURSOR, ""),
]

IDENT = r"[A-Za-z_][\w$]*"
_DECL_RE = re.compile(r"\b(input|output|inout|wire|reg|logic|integer|genvar|parameter|localparam)\b"
                      r"((?:\s+(?:wire|reg|logic|signed|unsigned))*)\s*(\[[^\]]*\])?\s*"
                      r"((?:(?!\b(?:input|output|inout|wire|reg|logic|integer|genvar|parameter|localparam)\b)[^;)])*)")
_RESERVED = set(KEYWORDS) | set(GATES) | {"wire", "reg", "logic", "signed", "unsigned", "module", "endmodule"}


def strip_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), code, flags=re.S)
    return re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), code)


def in_comment_or_string(line_before: str) -> bool:
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line_before)
    return "//" in code or code.count('"') % 2 == 1


# --------------------------------------------------------------------------- context
def completion_context(before: str) -> dict:
    """Classify the text before the caret.

    -> {"mode": "port"|"system"|"directive"|"word"|"none", "prefix": str, "replace": int (chars to delete),
        "module": str (port mode), "used": set(port names already connected), "instance": str}"""
    line = before.rsplit("\n", 1)[-1]
    if in_comment_or_string(line):
        return {"mode": "none", "prefix": "", "replace": 0}
    m = re.search(r"`(\w*)$", line)
    if m:
        return {"mode": "directive", "prefix": "`" + m.group(1), "replace": len(m.group(0))}
    m = re.search(r"\$(\w*)$", line)
    if m:
        return {"mode": "system", "prefix": "$" + m.group(1), "replace": len(m.group(0))}
    m = re.search(r"\.\s*(\w*)$", line)
    if m:
        inst = enclosing_instance(before[:len(before) - len(m.group(0))])
        if inst:
            return {"mode": "port", "prefix": m.group(1), "replace": len(m.group(0)), **inst}
        return {"mode": "none", "prefix": "", "replace": 0}
    m = re.search(IDENT + "$", line)
    word = m.group(0) if m else ""
    if m and m.start() > 0 and line[m.start() - 1] in "$`.":
        return {"mode": "none", "prefix": "", "replace": 0}
    if word[:1].isdigit():
        return {"mode": "none", "prefix": "", "replace": 0}
    return {"mode": "word", "prefix": word, "replace": len(word)}


def enclosing_instance(before: str) -> dict | None:
    """If the caret sits inside `Type [#(...)] name ( ... ` return {"module", "instance", "used"}."""
    code = strip_comments(before)
    depth, i = 0, len(code) - 1
    while i >= 0:
        ch = code[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                break
            depth -= 1
        elif ch == ";" and depth == 0:
            return None
        i -= 1
    if i < 0:
        return None
    head = code[:i]
    m = re.search(r"(" + IDENT + r")\s*(?:#\s*\((?:[^()]|\([^()]*\))*\)\s*)?(" + IDENT + r")\s*(?:\[[^\]]*\]\s*)?$", head)
    if not m or m.group(1) in _RESERVED or m.group(1) in ("module", "macromodule", "function", "task"):
        return None
    inside = code[i + 1:]
    used = set(re.findall(r"\.\s*(" + IDENT + r")\s*\(", inside))
    return {"module": m.group(1), "instance": m.group(2), "used": used}


# --------------------------------------------------------------------------- local declarations
def local_signals(text: str) -> list[dict]:
    """Ports / wires / regs / parameters declared in the buffer (regex based: works while the code is incomplete)."""
    out, seen = [], set()
    code = strip_comments(text)
    for m in _DECL_RE.finditer(code):
        kw, rng, names = m.group(1), (m.group(3) or "").replace(" ", ""), m.group(4)
        for piece in names.split(","):
            n = re.match(r"\s*(" + IDENT + ")", piece)
            if not n or n.group(1) in _RESERVED or n.group(1) in seen:
                continue
            seen.add(n.group(1))
            out.append({"name": n.group(1), "detail": f"{kw} {rng}".strip()})
    return out


# --------------------------------------------------------------------------- module helpers
def port_width(p: dict) -> int:
    return int(p.get("bits") or 1) if str(p.get("bits") or "1").isdigit() else 0


def port_detail(p: dict) -> str:
    w = port_width(p)
    return f"{p.get('direction', '?')} {f'{w}-bit' if w else (p.get('range_text') or 'bus')}"


def signature(info: dict) -> str:
    """'input [3:0] a, input [1:0] sel -> output y'"""
    ports = info.get("ports") or []
    ins = [f"{p['direction']} {(p.get('range_text') or '') + ' ' if p.get('range_text') else ''}{p['name']}"
           for p in ports if p.get("direction") != "output"]
    outs = [f"output {(p.get('range_text') or '') + ' ' if p.get('range_text') else ''}{p['name']}"
            for p in ports if p.get("direction") == "output"]
    return ", ".join(ins) + (" -> " + ", ".join(outs) if outs else "")


def next_instance_name(module: str, text: str, prefix: str | None = None) -> str:
    base = prefix or f"u_{module.lower()}_"
    used = [int(n) for n in re.findall(r"\b" + re.escape(base) + r"(\d+)\b", text)]
    return f"{base}{max(used, default=0) + 1}"


def instantiation(info: dict, text: str = "") -> str:
    """Full instance with every port listed; caret inside the first port's parentheses."""
    name = info["module_name"]
    inst = next_instance_name(name, text)
    ports = info.get("ports") or []
    if not ports:
        return f"{name} {inst} ();{CURSOR}"
    one = f"{name} {inst} (" + ", ".join(f".{p['name']}({CURSOR if i == 0 else ''})" for i, p in enumerate(ports)) + ");"
    if len(one) <= 90:
        return one
    w = max(len(p["name"]) for p in ports)
    body = ",\n".join(f"    .{p['name'].ljust(w)} ({CURSOR if i == 0 else ''})" for i, p in enumerate(ports))
    return f"{name} {inst} (\n{body}\n);"


# --------------------------------------------------------------------------- candidates
def build_items(ctx: dict, text: str, known: list[dict] | dict, own_modules: set[str] | None = None) -> list[dict]:
    """All candidates for this context (unfiltered). known = global module index entries (workspace + packages)."""
    known_list = list(known.values()) if isinstance(known, dict) else list(known or [])
    own = own_modules or set()
    mode = ctx.get("mode")
    items: list[dict] = []
    if mode == "port":
        mod = next((k for k in known_list if k.get("module_name") == ctx.get("module")), None)
        if not mod:
            return []
        for p in mod.get("ports") or []:
            items.append({"label": f".{p['name']}()", "match": p["name"], "kind": "P", "insert": f".{p['name']}({CURSOR})",
                          "detail": f"[{port_detail(p)}]", "done": p["name"] in ctx.get("used", set()),
                          "doc": f"{mod['module_name']} · {port_detail(p)} {p['name']}"})
        items.sort(key=lambda i: i["done"])          # unconnected ports first, in declaration order
        return items
    if mode == "system":
        return [{"label": l, "match": l, "kind": "S", "insert": ins, "detail": d, "doc": d} for l, ins, d in SYSTEM_TASKS]
    if mode == "directive":
        return [{"label": l, "match": l, "kind": "S", "insert": ins, "detail": d, "doc": d} for l, ins, d in DIRECTIVES]
    if mode != "word":
        return []
    seen: set[str] = set()
    for k in sorted(known_list, key=lambda k: k.get("module_name", "").lower()):
        name = k.get("module_name")
        if not name or name in seen or name in own:
            continue
        seen.add(name)
        pkg = k.get("package_name") or ""
        sig = signature(k)
        doc = f"{name} ({sig})" + (f" · {pkg}" if pkg else "")
        items.append({"label": f"{name}", "match": name, "kind": "M", "insert": instantiation(k, text), "snippet": True,
                      "detail": "Full Instantiation", "doc": doc, "variant": "inst"})
        items.append({"label": f"{name}", "match": name, "kind": "M", "insert": name, "detail": "Module Name",
                      "doc": doc, "variant": "name"})
    for s in local_signals(text):
        if s["name"] not in seen:
            seen.add(s["name"])
            items.append({"label": s["name"], "match": s["name"], "kind": "W", "insert": s["name"], "detail": s["detail"],
                          "doc": f"{s['detail']} {s['name']} (this file)"})
    for g, hint in GATES.items():
        n = next_instance_name(g, text, prefix="g")
        items.append({"label": g, "match": g, "kind": "G", "insert": f"{g} {n} ({CURSOR});", "snippet": True,
                      "detail": hint, "doc": f"{g} {hint} - the output pin comes first"})
    for label, ins, d in KEYWORD_SNIPPETS:
        items.append({"label": label, "match": label.split()[0], "kind": "K", "insert": ins, "snippet": True,
                      "detail": d, "doc": ins.replace(CURSOR, "").split("\n")[0] + (" …" if "\n" in ins else "")})
    for kw in KEYWORDS:
        if kw not in GATES:
            items.append({"label": kw, "match": kw, "kind": "K", "insert": kw, "detail": "keyword", "doc": f"keyword {kw}"})
    return items


def _initials(name: str) -> str:
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)
    return "".join(p[0] for p in parts).lower()


def _subsequence(pre: str, s: str) -> bool:
    it = iter(s)
    return all(c in it for c in pre)


def match_score(prefix: str, word: str) -> int | None:
    """Lower is better; None = no match. exact-case prefix < prefix < initials (fa -> FullAdder) < substring < subsequence."""
    if not prefix:
        return 5
    p, w = prefix.lower(), word.lower()
    if word.startswith(prefix):
        return 0
    if w.startswith(p):
        return 1
    if len(p) >= 2 and _initials(word).startswith(p):
        return 2
    if p in w:
        return 3
    if len(p) >= 2 and _subsequence(p, w):
        return 4
    return None


def rank(items: list[dict], prefix: str, limit: int = 60) -> list[dict]:
    out = []
    for idx, it in enumerate(items):
        s = match_score(prefix.lstrip("$`"), it["match"].lstrip("$`"))
        if s is None:
            continue
        if it["kind"] == "P":          # ports keep declaration order (unconnected first)
            key = (0, s, it.get("done", False), idx)
        else:
            key = (0 if s <= 1 else 1, KIND_RANK[it["kind"]], s, len(it["match"]), it["match"].lower(),
                   0 if it.get("variant") == "inst" else 1, idx)
        out.append((key, it))
    out.sort(key=lambda t: t[0])
    res = [it for _k, it in out[:limit]]
    if prefix and len(res) == 1 and res[0]["insert"] == prefix:     # already typed exactly: nothing to offer
        return []
    return res
