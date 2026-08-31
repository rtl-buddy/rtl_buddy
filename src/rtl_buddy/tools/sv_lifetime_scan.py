"""Pre-synthesis scan for SystemVerilog subroutines with static lifetime.

A `function` or `task` declared at module, interface, package, program, or
compilation-unit scope without an explicit `automatic` lifetime has *static*
lifetime: every formal argument and local variable is a single shared storage
location. Simulation hides this, because a call completes atomically inside a
process, but yosys-slang lowers the declaration literally and creates one net
per formal that every call site shares. Two call sites in one combinational
process then alias their arguments — no error, no warning, and a wrong netlist
(rtl-buddy/rtl_buddy#472).

The scan is a tokenizer, not a parser: rtl_buddy has no pyslang dependency, so
there is no elaborated AST to ask. It is deliberately conservative and its
limits are worth stating:

- It does not run the preprocessor. A declaration produced by a macro is
  reported at the macro's *definition* line, and one whose `function` keyword
  or `automatic` qualifier is itself hidden inside a macro is missed entirely.
- `include` files are scanned only when the synthesis filelist names them as
  sources; `-y` library directories are not walked (their contents are not in
  the filelist).
- Scope tracking pairs keywords (`module`/`endmodule`, `class`/`endclass`, …)
  rather than parsing declarations, so pathological but legal code can confuse
  the nesting. A confused scope changes which declarations are *exempt*, never
  whether the file parses.

Exemptions follow the language, not taste: class methods are automatic by
definition, `extern` / `pure virtual` prototypes and DPI imports/exports have
no body here, and a `module automatic` (or `package`/`interface`/`program`
`automatic`) header makes its unqualified subroutines automatic. An explicit
`function static` outside a class is reported: it is the same shared storage,
written on purpose.
"""

import re
from dataclasses import dataclass

# Token kinds produced by _tokenize.
_WORD = "word"
_STRING = "string"
_PUNCT = "punct"

_TOKEN_RE = re.compile(
    r"""
      (?P<line_comment> // [^\n]* )
    | (?P<block_comment> /\* .*? \*/ )
    | (?P<string> " (?: \\. | [^"\\\n] )* " )
    | (?P<escaped_id> \\ \S+ )
    | (?P<word> [A-Za-z_$] [A-Za-z0-9_$]* )
    | (?P<ws> \s+ )
    | (?P<other> . )
    """,
    re.VERBOSE | re.DOTALL,
)

# Scope-opening keyword -> the keyword that closes it.
_SCOPE_ENDERS = {
    "module": "endmodule",
    "macromodule": "endmodule",
    "package": "endpackage",
    "interface": "endinterface",
    "program": "endprogram",
    "checker": "endchecker",
    "class": "endclass",
    "function": "endfunction",
    "task": "endtask",
}

_END_KEYWORDS = frozenset(_SCOPE_ENDERS.values())

# Scopes that are declaration containers rather than object types; reaching one
# while walking outwards means the subroutine is not a class method.
_NON_CLASS_SCOPES = frozenset(
    {"module", "macromodule", "package", "interface", "program", "checker"}
)

# Declaration containers other than `class` and `interface`, which need extra
# disambiguation and are handled separately. All of these accept a
# `[lifetime]` qualifier in their header except `checker`, where the lookahead
# simply never matches.
_CONTAINER_SCOPES = frozenset(
    {"module", "macromodule", "package", "program", "checker"}
)

# Qualifiers that mean "this `function`/`task` keyword introduces a prototype or
# an imported/exported subroutine", so there is no body and no storage here.
_PROTOTYPE_QUALIFIERS = frozenset({"extern", "pure", "import", "export"})

# A statement boundary resets the pending-qualifier window.
_STATEMENT_RESET = frozenset({"begin"})


@dataclass(frozen=True)
class LifetimeFinding:
    """One subroutine declared without an effective `automatic` lifetime."""

    path: str
    line: int
    kind: str  # "function" or "task"
    name: str

    def describe(self) -> str:
        """`file:line: function <name>`, the form the issue asked for."""
        return f"{self.path}:{self.line}: {self.kind} {self.name}"


@dataclass
class _Scope:
    keyword: str
    automatic: bool


def _tokenize(text: str) -> list[tuple[str, str, int]]:
    """Split SystemVerilog text into `(kind, text, line)` tokens.

    Comments and whitespace are dropped. String literals survive as a single
    `_STRING` token because `import "DPI-C"` has to stay recognisable, but
    their contents are never inspected, so a keyword inside a string cannot
    become a finding.
    """
    tokens: list[tuple[str, str, int]] = []
    line = 1
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        value = m.group()
        start_line = line
        line += value.count("\n")
        if kind in ("line_comment", "block_comment", "ws"):
            continue
        if kind == "string":
            tokens.append((_STRING, value, start_line))
        elif kind == "word":
            tokens.append((_WORD, value, start_line))
        elif kind == "escaped_id":
            # \escaped.identifier — the leading backslash is not part of the name.
            tokens.append((_WORD, value[1:], start_line))
        else:
            tokens.append((_PUNCT, value, start_line))
    return tokens


def _next_word(tokens: list[tuple[str, str, int]], index: int) -> str | None:
    """Text of the next `_WORD` token, or None if the next token is not one."""
    if index + 1 < len(tokens):
        kind, value, _ = tokens[index + 1]
        if kind == _WORD:
            return value
    return None


def _parse_subroutine_header(
    tokens: list[tuple[str, str, int]], index: int
) -> tuple[str | None, str]:
    """Read a `function`/`task` header starting at `index`.

    Returns `(explicit_lifetime, name)` where `explicit_lifetime` is
    ``"automatic"``, ``"static"``, or None. The name is the last identifier at
    bracket depth zero before the argument list or the terminating `;`, which
    handles `ptr_t inc(`, `bit [W-1:0] f;`, `pkg::t_e g(`, and `void run;`
    without needing a type grammar.
    """
    explicit: str | None = None
    name = ""
    bracket = 0
    j = index + 1
    while j < len(tokens):
        kind, value, _ = tokens[j]
        if kind == _PUNCT:
            if value == "[":
                bracket += 1
            elif value == "]":
                bracket = max(0, bracket - 1)
            elif bracket == 0 and value in ("(", ";"):
                break
        elif kind == _WORD and bracket == 0:
            if value in ("automatic", "static") and explicit is None and not name:
                explicit = value
            else:
                name = value
        j += 1
    return explicit, name or "<unnamed>"


def _is_class_method(stack: list[_Scope]) -> bool:
    """Whether the innermost enclosing object scope is a class."""
    for scope in reversed(stack):
        if scope.keyword == "class":
            return True
        if scope.keyword in _NON_CLASS_SCOPES:
            return False
    return False


def scan_text(text: str, path: str) -> list[LifetimeFinding]:
    """Scan one source's text and return its static-lifetime findings."""
    tokens = _tokenize(text)
    findings: list[LifetimeFinding] = []
    stack: list[_Scope] = []
    pending: list[str] = []
    paren_depth = 0

    for i, (kind, value, line) in enumerate(tokens):
        if kind == _PUNCT:
            if value == "(":
                paren_depth += 1
            elif value == ")":
                paren_depth = max(0, paren_depth - 1)
            elif value == ";":
                pending.clear()
            continue

        if kind == _STRING:
            # Only the presence of a string matters (the `"DPI-C"` in an
            # import/export), never its contents.
            pending.append('""')
            continue

        if value in _END_KEYWORDS:
            for depth in range(len(stack) - 1, -1, -1):
                if _SCOPE_ENDERS[stack[depth].keyword] == value:
                    del stack[depth:]
                    break
            pending.clear()
            continue

        if value in _STATEMENT_RESET or value.startswith("end"):
            pending.clear()
            continue

        # A scope keyword inside parentheses is a port or argument type
        # (`module m (interface bus);`), never a declaration.
        if paren_depth == 0:
            if value in ("function", "task"):
                prototype = bool(_PROTOTYPE_QUALIFIERS.intersection(pending))
                explicit, name = _parse_subroutine_header(tokens, i)
                if prototype:
                    # No body, so no `endfunction` to pair with: do not push.
                    continue
                if _is_class_method(stack) or "virtual" in pending:
                    automatic = True
                elif explicit == "automatic":
                    automatic = True
                elif explicit == "static":
                    automatic = False
                else:
                    automatic = stack[-1].automatic if stack else False
                if not automatic:
                    findings.append(
                        LifetimeFinding(path=path, line=line, kind=value, name=name)
                    )
                stack.append(_Scope(keyword=value, automatic=automatic))
                pending.clear()
                continue

            # `typedef class C;` is a forward declaration with no `endclass`,
            # and `extern module m(...);` is a prototype: opening a scope for
            # either would swallow everything that follows.
            declares_scope = "typedef" not in pending and "extern" not in pending

            if value == "class" and declares_scope:
                # Class methods are automatic by definition.
                stack.append(_Scope(keyword="class", automatic=True))
                pending.clear()
                continue

            if value == "interface" and declares_scope:
                # `virtual interface bus_if h;` declares a variable, and
                # `interface class C;` is opened by the `class` that follows.
                if "virtual" not in pending and _next_word(tokens, i) != "class":
                    stack.append(
                        _Scope(
                            keyword="interface",
                            automatic=_next_word(tokens, i) == "automatic",
                        )
                    )
                    pending.clear()
                    continue

            if value in _CONTAINER_SCOPES and declares_scope:
                stack.append(
                    _Scope(
                        keyword=value,
                        automatic=_next_word(tokens, i) == "automatic",
                    )
                )
                pending.clear()
                continue

        pending.append(value)

    return findings


def scan_file(path: str) -> list[LifetimeFinding]:
    """Scan one file. Returns no findings when the file cannot be read."""
    try:
        with open(path, "r", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    return scan_text(text, path)


def scan_files(paths: list[str]) -> list[LifetimeFinding]:
    """Scan sources in order and return the concatenated findings."""
    findings: list[LifetimeFinding] = []
    for path in paths:
        findings.extend(scan_file(path))
    return findings


def describe_findings(findings: list[LifetimeFinding], limit: int = 10) -> str:
    """One-line summary naming each `file:line: kind name`, truncated at `limit`."""
    shown = [f.describe() for f in findings[:limit]]
    text = "; ".join(shown)
    remaining = len(findings) - len(shown)
    if remaining > 0:
        text += f"; and {remaining} more"
    return text
