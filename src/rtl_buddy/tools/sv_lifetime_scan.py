r"""Pre-synthesis scan for SystemVerilog subroutines with static lifetime.

A `function` or `task` declared outside a class without `automatic` shares one storage location per
argument across all calls. Simulation hides this, but yosys-slang creates one net per formal, so
call sites in one combinational process alias their arguments and the netlist is wrong.

The scan is a tokenizer with a definedness-only preprocessor, not a parser. It follows `` `include ``,
`` `ifdef `` / `` `ifndef `` / `` `elsif `` / `` `else `` / `` `endif `` and `` `undefineall ``, and
reports each declaration once. Limits:

- Macro bodies are skipped at their `` `define ``, so a declaration produced by a macro is not reported.
- `-y` library directories are not scanned, and an unresolvable `` `include `` is skipped (logged at DEBUG).
- `` `if `` expressions are not evaluated; only macro definedness is.
- Scope tracking pairs keywords rather than parsing declarations, so unusual legal code can misplace an exemption.

Exempt: class methods (including out-of-body `function int C::f(...)`), `extern` / `pure virtual`
prototypes, DPI imports and exports, and subroutines inside a `module automatic` (or
`package` / `interface` / `program` `automatic`). An explicit `function static` outside a class is reported.
"""

import logging
import os
import re
from dataclasses import dataclass, field

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

_WORD = "word"
_STRING = "string"
_PUNCT = "punct"
_DIRECTIVE = "directive"

_TOKEN_RE = re.compile(
    r"""
      (?P<line_comment> // [^\n]* )
    | (?P<block_comment> /\* .*? \*/ )
    | (?P<string> " (?: \\. | [^"\\\n] )* " )
    | (?P<directive> ` [A-Za-z_] [A-Za-z0-9_$]* )
    | (?P<escaped_id> \\ \S+ )
    | (?P<word> [A-Za-z_$] [A-Za-z0-9_$]* )
    | (?P<ws> \s+ )
    | (?P<other> . )
    """,
    re.VERBOSE | re.DOTALL,
)

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

# Reaching one of these while walking outwards means the subroutine is not a class method.
_NON_CLASS_SCOPES = frozenset(
    {"module", "macromodule", "package", "interface", "program", "checker"}
)

# Containers other than `class` and `interface`, which need extra disambiguation.
_CONTAINER_SCOPES = frozenset(
    {"module", "macromodule", "package", "program", "checker"}
)

# A following `function`/`task` is a prototype or DPI import/export: no body, no storage.
_PROTOTYPE_QUALIFIERS = frozenset({"extern", "pure", "import", "export"})

# A statement boundary resets the pending-qualifier window.
_STATEMENT_RESET = frozenset({"begin"})

# Runaway guard, matching slang's maxIncludeDepth. Exceeding it raises instead of skipping the
# header, because a skipped file hides findings.
MAX_INCLUDE_DEPTH = 1024


@dataclass(frozen=True)
class LifetimeFinding:
    """One subroutine declared without an effective `automatic` lifetime."""

    path: str
    line: int
    kind: str  # "function" or "task"
    name: str

    def describe(self) -> str:
        """Return ``file:line: <kind> <name>``."""
        return f"{self.path}:{self.line}: {self.kind} {self.name}"


@dataclass
class _Scope:
    keyword: str
    automatic: bool


@dataclass
class _Token:
    kind: str
    text: str
    line: int
    path: str
    escaped: bool = False


@dataclass
class _Cond:
    """One `` `ifdef `` frame: whether the branch is live and whether an earlier branch of the chain was."""

    active: bool
    taken: bool


@dataclass
class _ScanState:
    """Preprocessor state shared across a file and everything it includes.

    `active` is the chain of files currently open; it only stops `` `include `` cycles. Each
    inclusion of a header is scanned in its own context, and :func:`_dedupe` collapses repeats.
    """

    defined: set[str] = field(default_factory=set)
    incdirs: tuple[str, ...] = ()
    active: list[str] = field(default_factory=list)
    # Command-line macros (run `defines:` plus the frontend's implicit ones), restored by `` `undefineall ``.
    seed: frozenset[str] = frozenset()
    # True if `` `undefineall `` spares the seed (slang); False if it clears it too (Yosys read_verilog).
    keep_seed: bool = True


def _tokenize(text: str, path: str) -> list[_Token]:
    r"""Split SystemVerilog text into tokens tagged with their source line.

    Comments and whitespace are dropped. String contents are never inspected. Escaped identifiers
    are marked so `\begin` or `\endmodule` is not taken as a keyword.
    """
    tokens: list[_Token] = []
    line = 1
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        value = m.group()
        start_line = line
        line += value.count("\n")
        if kind in ("line_comment", "block_comment", "ws"):
            continue
        if kind == "string":
            tokens.append(_Token(_STRING, value, start_line, path))
        elif kind == "directive":
            tokens.append(_Token(_DIRECTIVE, value[1:], start_line, path))
        elif kind == "word":
            tokens.append(_Token(_WORD, value, start_line, path))
        elif kind == "escaped_id":
            # \escaped.identifier: the backslash is not part of the name, and the name is never a keyword.
            tokens.append(_Token(_WORD, value[1:], start_line, path, escaped=True))
        else:
            tokens.append(_Token(_PUNCT, value, start_line, path))
    return tokens


def _define_body_end(lines: list[str], start_index: int) -> int:
    """Return the 1-based line where the `` `define `` at `start_index` ends (a body continues past a trailing backslash)."""
    i = start_index
    while i < len(lines) and lines[i].rstrip().endswith("\\"):
        i += 1
    return i + 1


def _resolve_include(
    target: str, from_path: str, incdirs: tuple[str, ...]
) -> str | None:
    """Resolve an `` `include `` against the including file, then the incdirs."""
    if os.path.isabs(target):
        return target if os.path.isfile(target) else None
    candidates = [os.path.join(os.path.dirname(os.path.abspath(from_path)), target)]
    candidates.extend(os.path.join(d, target) for d in incdirs)
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.normpath(candidate)
    return None


def _read(path: str) -> str | None:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return None


@dataclass
class _Frame:
    """One source mid-expansion: its tokens, its lines and the walk position.

    The `` `ifdef `` chain is per frame: a conditional left open by a header does not continue into its includer.
    """

    path: str
    tokens: list[_Token]
    lines: list[str]
    depth: int
    # True if this frame pushed its realpath onto `_ScanState.active` and must pop it. Text passed
    # to `scan_text` does not; its caller owns the entry.
    opened: bool = False
    index: int = 0
    conds: list[_Cond] = field(default_factory=list)


def _new_frame(text: str, path: str, depth: int, *, opened: bool = False) -> _Frame:
    return _Frame(
        path=path,
        tokens=_tokenize(text, path),
        lines=text.splitlines(),
        depth=depth,
        opened=opened,
    )


def _open_frame(path: str, state: _ScanState, depth: int) -> _Frame | None:
    """Return the Frame for `path`, or None if it is a cycle or unreadable.

    The file stays in `state.active` until the driver pops it.
    """
    real = os.path.realpath(path)
    # Re-including a file already on the path is an include guard, not an error.
    if real in state.active:
        return None
    if depth > MAX_INCLUDE_DEPTH:
        chain = " -> ".join(state.active[-5:] + [path])
        raise FatalRtlBuddyError(
            f"`include nesting deeper than {MAX_INCLUDE_DEPTH} while scanning "
            f"for static-lifetime subroutines; last of the chain: {chain}"
        )
    text = _read(path)
    if text is None:
        return None
    state.active.append(real)
    return _new_frame(text, path, depth, opened=True)


def _expand_file(path: str, state: _ScanState, depth: int = 0) -> list[_Token]:
    """Return tokens for `path` and everything it includes, minus inactive `` `ifdef `` regions and macro bodies."""
    root = _open_frame(path, state, depth)
    if root is None:
        return []
    return _drive(root, state)


def _expand_text(
    text: str, path: str, state: _ScanState, depth: int = 0
) -> list[_Token]:
    """Tokens for already-read `text` and everything it includes."""
    return _drive(_new_frame(text, path, depth), state)


def _drive(root: _Frame, state: _ScanState) -> list[_Token]:
    """Expand `root` and every file it reaches, using an explicit stack.

    The walk is iterative so that :data:`MAX_INCLUDE_DEPTH` is reachable before Python's recursion limit.
    """
    out: list[_Token] = []
    stack = [root]
    try:
        while stack:
            frame = stack[-1]
            child = _advance(frame, state, out)
            if child is not None:
                stack.append(child)
                continue
            stack.pop()
            if frame.opened:
                state.active.pop()
    finally:
        # A depth overflow abandons the walk mid-chain; release the files it left open.
        for frame in stack:
            if frame.opened:
                state.active.pop()
    return out


def _advance(frame: _Frame, state: _ScanState, out: list[_Token]) -> _Frame | None:
    """Consume `frame`'s tokens into `out` until an `` `include `` to descend into, or the end.

    Returns the frame for that include, or None when `frame` is finished.
    """
    tokens = frame.tokens
    lines = frame.lines
    path = frame.path
    conds = frame.conds
    i = frame.index

    def active() -> bool:
        return all(c.active for c in conds)

    while i < len(tokens):
        tok = tokens[i]
        if tok.kind != _DIRECTIVE:
            if active():
                out.append(tok)
            i += 1
            continue

        name = tok.text
        # Conditionals are processed in inactive regions too, to keep nesting balanced.
        if name in ("ifdef", "ifndef"):
            macro = tokens[i + 1].text if i + 1 < len(tokens) else ""
            want = (
                (macro in state.defined)
                if name == "ifdef"
                else (macro not in state.defined)
            )
            live = active() and want
            conds.append(_Cond(active=live, taken=live))
            i += 2 if i + 1 < len(tokens) else 1
            continue
        if name == "elsif":
            macro = tokens[i + 1].text if i + 1 < len(tokens) else ""
            if conds:
                cond = conds[-1]
                outer = all(c.active for c in conds[:-1])
                live = outer and not cond.taken and macro in state.defined
                cond.active = live
                cond.taken = cond.taken or live
            i += 2 if i + 1 < len(tokens) else 1
            continue
        if name == "else":
            if conds:
                cond = conds[-1]
                outer = all(c.active for c in conds[:-1])
                live = outer and not cond.taken
                cond.active = live
                cond.taken = cond.taken or live
            i += 1
            continue
        if name == "endif":
            if conds:
                conds.pop()
            i += 1
            continue

        if name == "define":
            # Skip the whole macro body, also in inactive branches: replacement text may contain directives
            # (`ifdef) that would open a bogus conditional chain. The name is registered only in live
            # branches, and bodies are never scanned.
            if active() and i + 1 < len(tokens):
                state.defined.add(tokens[i + 1].text)
            end_line = _define_body_end(lines, tok.line - 1)
            i += 1
            while i < len(tokens) and tokens[i].line <= end_line:
                i += 1
            continue

        if not active():
            i += 1
            continue

        if name == "undef":
            if i + 1 < len(tokens):
                state.defined.discard(tokens[i + 1].text)
            i += 2 if i + 1 < len(tokens) else 1
            continue

        if name == "undefineall":
            # slang's undefineAll() re-applies the -D predefines; Yosys read_verilog clears them too.
            state.defined = set(state.seed) if state.keep_seed else set()
            i += 1
            continue

        if name == "include":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            i += 1
            if nxt is not None and nxt.kind == _STRING:
                i += 1
                target = nxt.text[1:-1]
                resolved = _resolve_include(target, path, state.incdirs)
                if resolved is None:
                    log_event(
                        logger,
                        logging.DEBUG,
                        "synth.lifetime_scan_include_unresolved",
                        path=path,
                        line=nxt.line,
                        include=target,
                    )
                else:
                    frame.index = i
                    child = _open_frame(resolved, state, frame.depth + 1)
                    if child is not None:
                        return child
                    continue
            # An `include of a macro or an angle-bracket path is left alone.
            continue

        # Other directives (`timescale, macro uses) carry no declaration or scope.
        i += 1

    frame.index = i
    return None


def _skip_parens(tokens: list[_Token], open_index: int) -> int:
    """Return the index just past the `)` matching the `(` at `open_index`.

    Returns the end of the token list if the group never closes, so a malformed header terminates the scan.
    """
    depth = 0
    j = open_index
    while j < len(tokens):
        tok = tokens[j]
        if tok.kind == _PUNCT:
            if tok.text == "(":
                depth += 1
            elif tok.text == ")":
                depth -= 1
                if depth == 0:
                    return j + 1
        j += 1
    return j


def _parse_subroutine_header(
    tokens: list[_Token], index: int
) -> tuple[str | None, str, bool]:
    """Read a `function`/`task` header starting at `index`.

    Returns `(explicit_lifetime, name, qualified)`. `explicit_lifetime` is "automatic", "static"
    or None. The name is the last identifier at depth zero before the argument list or `;`.
    `qualified` is True when an unescaped `::` or `.` was consumed, meaning an out-of-block
    definition of a method declared elsewhere; an escaped identifier is one name however it is spelled.

    `[ ]`, `{ }`, `#( )` and `type( )` groups in the return type are skipped whole so their
    `(` or `;` is not taken as the end of the header.
    """
    explicit: str | None = None
    name = ""
    bracket = 0
    brace = 0
    qualify = False
    qualified = False
    name_escaped = False
    j = index + 1
    while j < len(tokens):
        tok = tokens[j]
        if tok.kind == _PUNCT:
            if tok.text == "[":
                bracket += 1
            elif tok.text == "]":
                bracket = max(0, bracket - 1)
            elif tok.text == "{":
                brace += 1
            elif tok.text == "}":
                brace = max(0, brace - 1)
            elif brace:
                # Inside an anonymous struct/union body nothing is the name and `;` does not end the header.
                pass
            elif (
                bracket == 0
                and tok.text == "#"
                and j + 1 < len(tokens)
                and tokens[j + 1].kind == _PUNCT
                and tokens[j + 1].text == "("
            ):
                # `R#(int)` parameterises the return type; skip the group so its `(` is not the argument list.
                j = _skip_parens(tokens, j + 1)
                continue
            elif (
                bracket == 0
                and brace == 0
                and tok.text == "("
                and j > index + 1
                and tokens[j - 1].kind == _WORD
                and not tokens[j - 1].escaped
                and tokens[j - 1].text == "type"
            ):
                # `type(expr)` is the return type; skip it to reach the real name and any `C::` before it.
                j = _skip_parens(tokens, j)
                continue
            elif bracket == 0 and tok.text in ("(", ";"):
                break
            elif bracket == 0 and tok.text == "." and name:
                qualify = True
                qualified = True
                name += "."
            elif (
                bracket == 0
                and tok.text == ":"
                and name
                and j + 1 < len(tokens)
                and tokens[j + 1].kind == _PUNCT
                and tokens[j + 1].text == ":"
            ):
                # `::` arrives as two punct tokens; consume both.
                qualify = True
                qualified = True
                name += "::"
                j += 1
        elif tok.kind == _WORD and bracket == 0 and brace == 0:
            if (
                not tok.escaped
                and tok.text in ("automatic", "static")
                and explicit is None
                and not name
            ):
                explicit = tok.text
            elif qualify:
                name += tok.text
                qualify = False
                name_escaped = tok.escaped
            else:
                name = tok.text
                name_escaped = tok.escaped
                # A fresh unqualified name replaces what the separators built (`pkg::t_e decode(` -> `decode`).
                qualified = False
        j += 1
    # Trim only a separator this parser added; an escaped name may end in one.
    if not name_escaped:
        name = name.rstrip(":.")
    return explicit, name or "<unnamed>", qualified


def _is_class_method(stack: list[_Scope]) -> bool:
    """Return whether the innermost enclosing object scope is a class.

    Enclosing function/task scopes do not stop the walk. SystemVerilog has no nested subroutines
    (LRM 1800-2017 A.2.7/A.2.8), so this only affects input that does not compile.
    """
    for scope in reversed(stack):
        if scope.keyword == "class":
            return True
        if scope.keyword in _NON_CLASS_SCOPES:
            return False
    return False


def _attribute_end(tokens: list[_Token], open_index: int) -> int:
    """Return the index just past the `*)` closing the attribute opened at `open_index`.

    Returns `open_index` if the group is not an attribute or never closes.
    """
    depth = 0
    j = open_index
    while j < len(tokens):
        tok = tokens[j]
        if tok.kind == _PUNCT:
            if tok.text == "(":
                depth += 1
            elif tok.text == ")":
                depth -= 1
                if depth == 0:
                    prev = tokens[j - 1]
                    if prev.kind == _PUNCT and prev.text == "*":
                        return j + 1
                    return open_index
        j += 1
    return open_index


def _strip_attributes(tokens: list[_Token]) -> list[_Token]:
    """Drop `(* ... *)` attribute instances from the stream.

    Attributes carry arbitrary identifiers, including keywords (`(* extern *)`), which would
    otherwise reach the pending-qualifier window and exempt the declaration they decorate.
    """
    out: list[_Token] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if (
            tok.kind == _PUNCT
            and tok.text == "("
            and i + 1 < len(tokens)
            and tokens[i + 1].kind == _PUNCT
            and tokens[i + 1].text == "*"
        ):
            end = _attribute_end(tokens, i)
            if end > i:
                i = end
                continue
        out.append(tok)
        i += 1
    return out


def _walk(tokens: list[_Token]) -> list[LifetimeFinding]:
    # Stripped before the walk so the header parser indexing the same list cannot see them either.
    tokens = _strip_attributes(tokens)
    findings: list[LifetimeFinding] = []
    stack: list[_Scope] = []
    pending: list[str] = []
    paren_depth = 0

    for i, tok in enumerate(tokens):
        if tok.kind == _PUNCT:
            if tok.text == "(":
                paren_depth += 1
            elif tok.text == ")":
                paren_depth = max(0, paren_depth - 1)
            elif tok.text == ";":
                pending.clear()
            continue

        if tok.kind == _STRING:
            # Only the presence of a string matters (the "DPI-C" in an import/export).
            pending.append('""')
            continue

        if tok.kind != _WORD:
            continue

        value = tok.text
        # An escaped identifier is never a keyword, so it must not match through the pending window;
        # it stays there with its backslash to record that something stood here.
        if tok.escaped:
            pending.append("\\" + value)
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

        # A scope keyword inside parentheses is a port or argument type (`module m (interface bus);`).
        if paren_depth == 0:
            if value in ("function", "task"):
                prototype = bool(_PROTOTYPE_QUALIFIERS.intersection(pending))
                explicit, name, external = _parse_subroutine_header(tokens, i)
                if prototype:
                    # No body, so no `endfunction` to pair with: do not push.
                    continue
                if _is_class_method(stack) or "virtual" in pending or external:
                    # An out-of-body `function int C::f(...)` defines a class method. This outranks an explicit
                    # `static`: slang rejects static class methods, so reporting them would flag code that
                    # does not compile. `static function` (qualifier before the keyword) is a legal
                    # class-static method and never reaches `explicit`.
                    automatic = True
                elif explicit == "automatic":
                    automatic = True
                elif explicit == "static":
                    automatic = False
                else:
                    automatic = stack[-1].automatic if stack else False
                if not automatic:
                    findings.append(
                        LifetimeFinding(
                            path=tok.path, line=tok.line, kind=value, name=name
                        )
                    )
                stack.append(_Scope(keyword=value, automatic=automatic))
                pending.clear()
                continue

            # `typedef class C;` and `extern module m(...);` have no end keyword; opening a scope would swallow what follows.
            declares_scope = "typedef" not in pending and "extern" not in pending

            if value == "class" and declares_scope:
                # Class methods are automatic by definition.
                stack.append(_Scope(keyword="class", automatic=True))
                pending.clear()
                continue

            if value == "interface" and declares_scope:
                # `virtual interface bus_if h;` declares a variable; `interface class C;` is opened by the `class` that follows.
                nxt = _next_word(tokens, i)
                if "virtual" not in pending and nxt != "class":
                    stack.append(
                        _Scope(keyword="interface", automatic=nxt == "automatic")
                    )
                    pending.clear()
                    continue

            if value in _CONTAINER_SCOPES and declares_scope:
                stack.append(
                    _Scope(
                        keyword=value, automatic=_next_word(tokens, i) == "automatic"
                    )
                )
                pending.clear()
                continue

        pending.append(value)

    return findings


def _next_word(tokens: list[_Token], index: int) -> str | None:
    """Return the text of the next `_WORD` token, or None."""
    if index + 1 < len(tokens):
        nxt = tokens[index + 1]
        if nxt.kind == _WORD and not nxt.escaped:
            return nxt.text
    return None


def _dedupe(findings: list[LifetimeFinding]) -> list[LifetimeFinding]:
    """Collapse repeats of the same declaration, keeping the first.

    A header is scanned once per inclusion but reported once.
    """
    seen: set[tuple[str, int, str, str]] = set()
    out: list[LifetimeFinding] = []
    for f in findings:
        key = (f.path, f.line, f.kind, f.name)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _new_state(
    defines: dict | None,
    incdirs: tuple[str, ...] | list[str],
    keep_seed: bool = True,
) -> _ScanState:
    seed = frozenset(str(k) for k in (defines or {}))
    return _ScanState(
        defined=set(seed),
        incdirs=tuple(incdirs),
        seed=seed,
        keep_seed=keep_seed,
    )


def scan_text(
    text: str,
    path: str,
    *,
    incdirs: tuple[str, ...] | list[str] = (),
    defines: dict | None = None,
    undefineall_keeps_predefines: bool = True,
) -> list[LifetimeFinding]:
    """Scan one source's text and return its static-lifetime findings."""
    state = _new_state(defines, incdirs, undefineall_keeps_predefines)
    state.active.append(os.path.realpath(path))
    return _dedupe(_walk(_expand_text(text, path, state)))


def scan_file(
    path: str,
    *,
    incdirs: tuple[str, ...] | list[str] = (),
    defines: dict | None = None,
    undefineall_keeps_predefines: bool = True,
    state: _ScanState | None = None,
) -> list[LifetimeFinding]:
    """Scan one file and everything it includes.

    Returns no findings if the file cannot be read. Pass `state` to share a macro table across sources.
    """
    if state is None:
        state = _new_state(defines, incdirs, undefineall_keeps_predefines)
    return _dedupe(_walk(_expand_file(path, state)))


def scan_files(
    paths: list[str],
    *,
    incdirs: tuple[str, ...] | list[str] = (),
    defines: dict | None = None,
    single_unit: bool = False,
    undefineall_keeps_predefines: bool = True,
) -> list[LifetimeFinding]:
    """Scan sources in order and return their combined findings.

    `single_unit` must match how the frontend reads the sources. By default each top-level source
    starts from a fresh macro table seeded from `defines`, as yosys-slang compiles each file as its
    own unit unless `--single-unit` is passed; with `single_unit` the table is shared. Headers
    always share their includer's table.

    `undefineall_keeps_predefines` selects `` `undefineall `` semantics: True for slang (the
    default), False for Yosys's read_verilog, which also clears the predefines.
    """

    def _fresh() -> _ScanState:
        return _new_state(defines, incdirs, undefineall_keeps_predefines)

    shared = _fresh() if single_unit else None
    findings: list[LifetimeFinding] = []
    for path in paths:
        state = shared if shared is not None else _fresh()
        findings.extend(_walk(_expand_file(path, state)))
    return _dedupe(findings)


def describe_findings(findings: list[LifetimeFinding], limit: int = 10) -> str:
    """One-line summary naming each `file:line: kind name`, truncated at `limit`."""
    shown = [f.describe() for f in findings[:limit]]
    text = "; ".join(shown)
    remaining = len(findings) - len(shown)
    if remaining > 0:
        text += f"; and {remaining} more"
    return text
