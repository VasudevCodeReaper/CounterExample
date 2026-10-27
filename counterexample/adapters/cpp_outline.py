"""Lightweight C/C++ source outliner (no libclang required).

Finds top-level / namespace-level functions and class methods with their
byte spans, parameter types and return types.  Used by the C++ runner (to
locate the entrypoint and infer generators) and by ``extract_git_oracle``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "do", "else", "try",
    "alignof", "decltype", "static_assert", "new", "delete", "throw", "case", "default",
}

_FUNC_HEADER = re.compile(
    r"^(?P<prefix>.*?)(?P<name>~?[A-Za-z_]\w*(?:\s*::\s*~?[A-Za-z_]\w*)*)\s*"
    r"\((?P<params>.*)\)\s*(?P<quals>(?:const|noexcept(?:\([^)]*\))?|override|final|volatile|&&|&|\s|->\s*[\w:<>,\s\*&]+)*)$",
    re.S,
)
_CLASS_HEADER = re.compile(r"(?:template\s*<.*>\s*)?\b(class|struct|union)\s+(?P<name>[A-Za-z_]\w*)[^{;]*$", re.S)
_NAMESPACE_HEADER = re.compile(r"\bnamespace\s*(?P<name>[A-Za-z_]\w*)?\s*$")
_ACCESS = re.compile(r"^\s*(?:public|private|protected)\s*:\s*", re.S)
_ACCESS_AT = re.compile(r"(?:public|private|protected)\s*:(?!:)")


@dataclass
class CppDef:
    name: str
    qualified: str
    kind: str  # function | method | class | namespace
    start: int
    end: int
    ret: str = ""
    params: list[str] = field(default_factory=list)
    param_names: list[str] = field(default_factory=list)
    cls: str | None = None
    access: str = "public"


def blank_comments_and_strings(code: str) -> str:
    """Return ``code`` with comments, string/char literals and preprocessor
    lines replaced by spaces (newlines kept) so offsets stay aligned."""
    out = list(code)
    i, n = 0, len(code)
    line_start = True
    while i < n:
        c = code[i]
        if line_start and c == "#":
            j = i
            while j < n and code[j] != "\n":
                if code[j] == "\\" and j + 1 < n and code[j + 1] == "\n":
                    j += 2
                    continue
                j += 1
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if c == "\n":
            line_start = True
            i += 1
            continue
        if not c.isspace():
            line_start = False
        if code.startswith("//", i):
            j = code.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if code.startswith("/*", i):
            j = code.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if c == "R" and code.startswith('R"', i):
            m = re.match(r'R"([^(\s]*)\(', code[i:])
            if m:
                end = code.find(")" + m.group(1) + '"', i)
                j = n if end < 0 else end + len(m.group(1)) + 2
                for k in range(i + 1, j):
                    if out[k] != "\n":
                        out[k] = " "
                i = j
                continue
        if c in ('"', "'"):
            j = i + 1
            while j < n and code[j] != c:
                if code[j] == "\\":
                    j += 1
                elif code[j] == "\n":
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                out[k] = " "
            i = j + 1
            continue
        i += 1
    return "".join(out)


def split_params(params: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in params:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    if out == ["void"]:
        return []
    return out


def param_type_and_name(param: str) -> tuple[str, str]:
    param = re.sub(r"=.*$", "", param, flags=re.S).strip()
    m = re.match(r"^(?P<type>.*?[\s\*&>])(?P<name>[A-Za-z_]\w*)\s*(?P<arr>(?:\[[^\]]*\])*)$", param, re.S)
    if m and m.group("type").strip() and m.group("type").strip() not in ("const", "unsigned", "signed", "long", "short"):
        t = m.group("type").strip()
        if m.group("arr"):
            t += "*"
        return re.sub(r"\s+", " ", t), m.group("name")
    return re.sub(r"\s+", " ", param), ""


def outline(code: str) -> list[CppDef]:
    text = blank_comments_and_strings(code)
    defs: list[CppDef] = []
    # scope stack entries: (kind, name, def or None, access)
    stack: list[list] = [["namespace", "", None, "public"]]
    last_delim = 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == ";":
            last_delim = i + 1
        elif c == "{":
            header = text[last_delim:i].strip()
            top = stack[-1]
            if top[0] == "class":
                m_acc = _ACCESS.match(header)
                while m_acc:
                    top[3] = re.match(r"\s*(\w+)", m_acc.group(0)).group(1)
                    header = header[m_acc.end():]
                    m_acc = _ACCESS.match(header)
            entry: list = ["other", "", None, "public"]
            if top[0] in ("namespace", "class"):
                mc = _CLASS_HEADER.search(header)
                mn = _NAMESPACE_HEADER.search(header)
                mf = _FUNC_HEADER.match(header) if "(" in header else None
                if mn and not mf:
                    entry = ["namespace", mn.group("name") or "", None, "public"]
                elif mc and not mf:
                    kind = mc.group(1)
                    access = "private" if kind == "class" else "public"
                    d = CppDef(mc.group("name"), _qualify(stack, mc.group("name")), "class", _header_start(text, last_delim), -1)
                    defs.append(d)
                    entry = ["class", mc.group("name"), d, access]
                elif mf:
                    name = re.sub(r"\s+", "", mf.group("name"))
                    base = name.split("::")[-1]
                    prefix = mf.group("prefix").strip()
                    if base not in _KEYWORDS and not prefix.endswith(("=", "return", "(", ",")):
                        params = split_params(mf.group("params"))
                        typed = [param_type_and_name(p) for p in params]
                        in_class = top[0] == "class"
                        ret = re.sub(r"^(?:template\s*<.*?>\s*)?", "", prefix, flags=re.S)
                        ret = re.sub(r"\b(?:static|inline|virtual|constexpr|explicit|friend|extern)\b", " ", ret)
                        ret = re.sub(r"\s+", " ", ret).strip()
                        arrow = re.search(r"->\s*(.+)$", mf.group("quals") or "", re.S)
                        if ret == "auto" and arrow:
                            ret = arrow.group(1).strip()
                        cls = top[1] if in_class else (name.split("::")[-2] if "::" in name else None)
                        d = CppDef(
                            base,
                            _qualify(stack, name),
                            "method" if cls else "function",
                            _header_start(text, last_delim),
                            -1,
                            ret=ret,
                            params=[t for t, _ in typed],
                            param_names=[nm for _, nm in typed],
                            cls=cls,
                            access=top[3] if in_class else "public",
                        )
                        defs.append(d)
                        entry = ["function", base, d, "public"]
            stack.append(entry)
            last_delim = i + 1
        elif c == "}":
            if len(stack) > 1:
                done = stack.pop()
                if done[2] is not None:
                    end = i + 1
                    if done[0] == "class":
                        # include the trailing ';' of a class definition
                        m = re.match(r"[^;{}]*;", text[end:])
                        if m:
                            end += m.end()
                    done[2].end = end
            last_delim = i + 1
        i += 1
    return [d for d in defs if d.end > 0]


def _header_start(text: str, pos: int) -> int:
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        m = _ACCESS_AT.match(text, pos)
        if not m:
            return pos
        pos = m.end()


def _qualify(stack: list[list], name: str) -> str:
    parts = [s[1] for s in stack[1:] if s[0] in ("namespace", "class") and s[1]]
    return "::".join(parts + [name]) if parts else name


def include_lines(code: str) -> list[str]:
    return [ln for ln in code.splitlines() if re.match(r"\s*#\s*(include|define|pragma)\b", ln) or re.match(r"\s*using\s+namespace\b", ln)]


_DECL_NAMES = (
    re.compile(r"\busing\s+([A-Za-z_]\w*)\s*="),
    re.compile(r"\btypedef\b.*?\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*;\s*$", re.S),
    re.compile(r"\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])*\s*(?:=|\{|;)"),
)


def toplevel_statements(code: str) -> list[tuple[int, int, set[str]]]:
    """Namespace-level declarations ending in ``;`` (constants, typedefs,
    aliases, globals, prototypes) with the names they introduce."""
    text = blank_comments_and_strings(code)
    out: list[tuple[int, int, set[str]]] = []
    depth = 0
    last = 0
    for i, c in enumerate(text):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                last = i + 1
        elif c == ";" and depth == 0:
            seg = text[last : i + 1]
            start = last + (len(seg) - len(seg.lstrip()))
            body = seg.strip()
            if body and body != ";" and not body.startswith(("using namespace", "return")):
                names: set[str] = set()
                for rx in _DECL_NAMES:
                    m = rx.search(body)
                    if m:
                        names.add(m.group(1))
                        break
                out.append((start, i + 1, names))
            last = i + 1
    return out


def identifiers(code: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_]\w*", blank_comments_and_strings(code)))
