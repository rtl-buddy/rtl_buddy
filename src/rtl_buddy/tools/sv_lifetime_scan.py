r"""Pre-synthesis scan for SystemVerilog subroutines with static lifetime.

A `function` or `task` declared at module, interface, package, program, or
compilation-unit scope without an explicit `automatic` lifetime has *static*
lifetime: every formal argument and local variable is a single shared storage
location. Simulation hides this, because a call completes atomically inside a
process, but yosys-slang lowers the declaration literally and creates one net
per formal that every call site shares. Two call sites in one combinational
process then alias their arguments — no error, no warning, and a wrong netlist
(rtl-buddy/rtl_buddy#472).

The scan is a tokenizer with a definedness-only preprocessor, not a parser:
rtl_buddy has no pyslang dependency, so there is no elaborated AST to ask. It
follows `` `include `` directives and honours `` `ifdef `` / `` `ifndef `` /
`` `elsif `` / `` `else `` / `` `endif `` so it does not report code the
compiler never sees, but it is not a preprocessor and its limits run in both
directions:

Misses (a real hazard the scan does not report):

- Macro bodies are skipped at their `` `define ``, not at expansion, so a
  declaration produced by a macro is never reported. Its `function` keyword or
  `automatic` qualifier may also be hidden inside the macro.
- `-y` library directories are not scanned: they contribute files the
  synthesis filelist never names.
- An `` `include `` whose path cannot be resolved against the including file's
  directory or the filelist's `+incdir+` entries is logged at DEBUG and
  skipped, not failed.

`` `undefineall `` is honoured with the semantics of the frontend in use:
slang spares the command-line macros (its `undefineAll()` re-applies
`options.predefines`), while Yosys's own `read_verilog` clears those too.

The macro table follows the compilation-unit boundary the frontend actually
uses: reset per top-level source and re-seeded from the run's defines, unless
the caller passes ``single_unit=True``. Headers share their includer's table,
since `` `include `` is textual. Each inclusion of a header is scanned in its
own context -- the same header is exempt inside a class and a finding inside an
ordinary module -- and repeated declarations are collapsed afterwards.

Spurious findings (something reported that is not a hazard):

- `` `if `` expression evaluation is not implemented, and is not SystemVerilog
  anyway: only definedness is evaluated. A branch selected by a macro *value*
  rather than by its existence is not modelled.
- Scope tracking pairs keywords (`module`/`endmodule`, `class`/`endclass`, …)
  rather than parsing declarations, so pathological but legal code can confuse
  the nesting and change which declarations are exempt.

`(* ... *)` attribute instances are dropped whole. They carry arbitrary user
identifiers, some of which collide with keywords (`(* keep *)`, and legally
`(* extern *)` or an escaped `(* \extern *)`), and none of them qualify the
declaration they decorate.

Exemptions follow the language, not taste: class methods (including out-of-body
definitions such as `function int C::f(...)`) are automatic by definition,
`extern` / `pure virtual` prototypes and DPI imports/exports have no body here,
and a `module automatic` (or `package`/`interface`/`program` `automatic`)
header makes its unqualified subroutines automatic. An explicit
`function static` outside a class is reported: it is the same shared storage,
written on purpose.
"""

import logging
import os
import re
from dataclasses import dataclass, field

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

# Token kinds produced by _tokenize.
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

# Runaway guard only: a real `include cycle is already stopped by the active
# path check in _expand_file, so anything that reaches this limit is a chain
# deeper than any compiler would accept. Matched to slang's own
# PreprocessorOptions::maxIncludeDepth so the scan does not give up on
# anything slang would still preprocess. Exceeding it raises rather than
# silently dropping the header: a skipped file is a missed finding, and this
# gate exists because missed findings are invisible.
MAX_INCLUDE_DEPTH = 1024


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


@dataclass
class _Token:
    kind: str
    text: str
    line: int
    path: str
    escaped: bool = False


@dataclass
class _Cond:
    """One `` `ifdef `` frame: whether this branch is live and whether a
    previous branch of the same chain already was."""

    active: bool
    taken: bool


@dataclass
class _ScanState:
    """Preprocessor state shared across a file and everything it includes.

    `active` is the chain of files currently open, used only to stop an
    `` `include `` cycle. It is deliberately not a permanent "already scanned"
    set: the same header included from a class and from an ordinary module is
    exempt in the first context and a finding in the second, so every
    inclusion has to be scanned in its own context. Repeats are collapsed
    afterwards by :func:`_dedupe`, which keys on the declaration itself.
    """

    defined: set[str] = field(default_factory=set)
    incdirs: tuple[str, ...] = ()
    active: list[str] = field(default_factory=list)
    # The command-line seed (run `defines:` plus the frontend's own implicit
    # macros), kept so `` `undefineall `` can restore it -- see `keep_seed`.
    seed: frozenset[str] = frozenset()
    # Whether `` `undefineall `` spares the seed. It does under slang, whose
    # undefineAll() re-applies options.predefines; it does NOT under Yosys's
    # own read_verilog, which clears its global_defines_cache too.
    keep_seed: bool = True


def _tokenize(text: str, path: str) -> list[_Token]:
    r"""Split SystemVerilog text into tokens tagged with their source line.

    Comments and whitespace are dropped. String literals survive as one
    `_STRING` token because `import "DPI-C"` and `` `include "x.svh" `` have to
    stay recognisable, but their contents are never inspected, so a keyword
    inside a string cannot become a finding. Escaped identifiers are marked so
    a `\begin` or `\endmodule` cannot be mistaken for the keyword.
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
            # \escaped.identifier — the leading backslash is not part of the
            # name, and the name is never a keyword however it is spelled.
            tokens.append(_Token(_WORD, value[1:], start_line, path, escaped=True))
        else:
            tokens.append(_Token(_PUNCT, value, start_line, path))
    return tokens


def _define_body_end(lines: list[str], start_index: int) -> int:
    """1-based line number where the `` `define `` starting at `start_index` ends.

    A macro body continues while the line ends with a backslash.
    """
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


def _expand_file(path: str, state: _ScanState, depth: int = 0) -> list[_Token]:
    """Tokens for `path` and everything it includes, with inactive
    `` `ifdef `` regions and macro bodies removed."""
    real = os.path.realpath(path)
    # A cycle is not an error: `\`include` of a file already on the path is
    # how include guards behave, and the guarded body is empty the second time.
    if real in state.active:
        return []
    if depth > MAX_INCLUDE_DEPTH:
        chain = " -> ".join(state.active[-5:] + [path])
        raise FatalRtlBuddyError(
            f"`include nesting deeper than {MAX_INCLUDE_DEPTH} while scanning "
            f"for static-lifetime subroutines; last of the chain: {chain}"
        )
    text = _read(path)
    if text is None:
        return []
    state.active.append(real)
    try:
        return _expand_text(text, path, state, depth)
    finally:
        state.active.pop()


def _expand_text(
    text: str, path: str, state: _ScanState, depth: int = 0
) -> list[_Token]:
    lines = text.splitlines()
    tokens = _tokenize(text, path)
    out: list[_Token] = []
    conds: list[_Cond] = []
    i = 0

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
        # Conditional directives are processed even inside an inactive region,
        # so nesting stays balanced.
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
                frame = conds[-1]
                outer = all(c.active for c in conds[:-1])
                live = outer and not frame.taken and macro in state.defined
                frame.active = live
                frame.taken = frame.taken or live
            i += 2 if i + 1 < len(tokens) else 1
            continue
        if name == "else":
            if conds:
                frame = conds[-1]
                outer = all(c.active for c in conds[:-1])
                live = outer and not frame.taken
                frame.active = live
                frame.taken = frame.taken or live
            i += 1
            continue
        if name == "endif":
            if conds:
                conds.pop()
            i += 1
            continue

        if not active():
            i += 1
            continue

        if name == "define":
            # Register the macro name, then skip its whole body: a macro is
            # scanned where it expands, and rtl_buddy does not expand macros.
            if i + 1 < len(tokens):
                state.defined.add(tokens[i + 1].text)
            end_line = _define_body_end(lines, tok.line - 1)
            i += 1
            while i < len(tokens) and tokens[i].line <= end_line:
                i += 1
            continue

        if name == "undef":
            if i + 1 < len(tokens):
                state.defined.discard(tokens[i + 1].text)
            i += 2 if i + 1 < len(tokens) else 1
            continue

        if name == "undefineall":
            # Verified against both frontends with a syntax error inside the
            # guarded region: slang's Preprocessor::undefineAll() clears the
            # macro map and then re-applies options.predefines, so the -D
            # macros survive; Yosys's read_verilog clears `defines` AND
            # `global_defines_cache`, so nothing survives.
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
                    out.extend(_expand_file(resolved, state, depth + 1))
            # An `include of a macro or an angle-bracket path is left alone.
            continue

        # Any other directive (`timescale, `celldefine, a macro use) is not a
        # declaration and carries no scope.
        i += 1

    return out


def _skip_parens(tokens: list[_Token], open_index: int) -> int:
    """Index just past the `)` matching the `(` at `open_index`.

    Returns the end of the token list when the group never closes, so a
    truncated or malformed header terminates the header scan instead of
    looping.
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

    Returns `(explicit_lifetime, name, qualified)` where `explicit_lifetime`
    is ``"automatic"``, ``"static"``, or None. The name is the last identifier
    at nesting depth zero before the argument list or the terminating `;`,
    which handles `ptr_t inc(`, `bit [W-1:0] f;`, `pkg::t_e g(`, and
    `void run;` without needing a type grammar.

    Only the header's *own* `(` or `;` stops the scan, so every grouping a
    return type can bring has to be tracked: `[ ]` for packed ranges (which
    may hold a call, `bit [$clog2(W)-1:0] g(`), `{ }` for an anonymous
    `struct`/`union`/`enum` body -- whose members end in `;` or `,`, and whose
    `;` used to stop the scan mid-type and name the subroutine after its last
    member -- and two parenthesised groups skipped whole so their `(` is not
    read as the argument list: `#( )` for a parameterisation
    (`function R#(int) C::f(`) and `type( )` for a type reference
    (`function type(expr) C::f(`, LRM 6.23).

    `qualified` says an **unescaped** `::` or `.` separator was consumed, so
    the declaration is an out-of-block definition of a method declared
    elsewhere. It is deliberately not derived from the name text: an escaped
    identifier is a single name however it is spelled, so `\\C::f` is an
    ordinary subroutine called `C::f`, not `f` belonging to `C`.
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
                # Inside an anonymous struct/union body nothing is the name,
                # and its members' `;` are not the header's terminator.
                pass
            elif (
                bracket == 0
                and tok.text == "#"
                and j + 1 < len(tokens)
                and tokens[j + 1].kind == _PUNCT
                and tokens[j + 1].text == "("
            ):
                # `R#(int)` parameterises the *return type*; the real name is
                # still to come. Skip the balanced group so its `(` is not
                # read as the argument list.
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
                # `type(expr)` is a type reference used as the return type, so
                # this `(` opens the type, not the argument list. Skipping the
                # group lets the scan reach the real name -- and the `C::`
                # ahead of it, which decides whether this is an out-of-block
                # class method. `type` itself is then replaced as the name by
                # the identifier that follows.
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
                # A fresh unqualified name replaces anything the separators
                # had built up (`function pkg::t_e decode(` -> `decode`).
                qualified = False
        j += 1
    # Only trim a dangling separator this parser added; an escaped name may
    # legitimately end in one.
    if not name_escaped:
        name = name.rstrip(":.")
    return explicit, name or "<unnamed>", qualified


def _is_class_method(stack: list[_Scope]) -> bool:
    """Whether the innermost enclosing object scope is a class.

    A `function`/`task` scope between the declaration and the class does not
    stop the walk, and deliberately so: SystemVerilog has no nested
    subroutines to distinguish. A subroutine body admits `tf_item_declaration`
    -- data, type, parameter and `let` declarations -- and not a `function` or
    `task` declaration (LRM 1800-2017 A.2.7/A.2.8), so a subroutine can only
    be declared directly in a class, module, interface, program, package or
    generate block. slang agrees, rejecting every nested form with
    "expected statement". Restricting the walk to a direct class scope would
    therefore only change the verdict on input that does not compile.
    """
    for scope in reversed(stack):
        if scope.keyword == "class":
            return True
        if scope.keyword in _NON_CLASS_SCOPES:
            return False
    return False


def _attribute_end(tokens: list[_Token], open_index: int) -> int:
    """Index just past the `*)` closing the attribute opened at `open_index`.

    Returns `open_index` when the group is not actually an attribute (a `(`
    that closes without a `*`) or never closes, so the caller leaves it alone.
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

    An attribute carries arbitrary user identifiers -- `(* keep *)`,
    `(* ram_style = "block" *)`, and legally even `(* extern *)` -- none of
    which are declarations or qualifiers. Leaving them in let a name that
    happens to collide with a keyword reach the pending-qualifier window and
    exempt the very declaration the attribute was decorating. Removing the
    whole group is the robust rule, and it removes a balanced pair of
    parentheses so the paren depth the walker tracks is unaffected.
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
    # Stripped before the walk, not skipped during it, so the header parser
    # indexing into the same list cannot see an attribute either.
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
            # Only the presence of a string matters (the `"DPI-C"` in an
            # import/export), never its contents.
            pending.append('""')
            continue

        if tok.kind != _WORD:
            continue

        value = tok.text
        # An escaped identifier is never a keyword, however it is spelled, so
        # it must not match one through the pending window either -- a
        # `\\extern` is a name. Kept in `pending` with its backslash so the
        # window still records that something stood here.
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

        # A scope keyword inside parentheses is a port or argument type
        # (`module m (interface bus);`), never a declaration.
        if paren_depth == 0:
            if value in ("function", "task"):
                prototype = bool(_PROTOTYPE_QUALIFIERS.intersection(pending))
                explicit, name, external = _parse_subroutine_header(tokens, i)
                if prototype:
                    # No body, so no `endfunction` to pair with: do not push.
                    continue
                if _is_class_method(stack) or "virtual" in pending or external:
                    # An out-of-body `function int C::f(...)` defines a class
                    # method; the class it belongs to is elsewhere.
                    #
                    # This deliberately outranks an explicit `static`. A class
                    # method may NOT have a static lifetime -- slang rejects
                    # every arm of this condition at parse time with "class
                    # methods cannot have static lifetime" (its parser raises
                    # MethodStaticLifetime for an in-class declaration and for
                    # a `::`-scoped out-of-block one alike), so
                    # `function static int C::f(...)`, `virtual function
                    # static ...` and an in-class `function static ...` are
                    # all uncompilable. Reporting them would be a finding
                    # against code that has no netlist to corrupt, on a run
                    # the frontend already fails with a clearer message.
                    # `static function` -- the qualifier BEFORE the keyword --
                    # is a different thing, a class-static method, and is
                    # legal; it never reaches `explicit`, which only reads the
                    # lifetime slot after the keyword.
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
    """Text of the next `_WORD` token, or None if the next token is not one."""
    if index + 1 < len(tokens):
        nxt = tokens[index + 1]
        if nxt.kind == _WORD and not nxt.escaped:
            return nxt.text
    return None


def _dedupe(findings: list[LifetimeFinding]) -> list[LifetimeFinding]:
    """Collapse repeats of the same declaration, keeping the first.

    A header included from several modules is scanned once per inclusion, so
    that each is judged in its own context, but the declaration inside it is
    one declaration and is reported once.
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

    Returns no findings when the file cannot be read. Pass `state` to share a
    macro table across several sources.
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

    `single_unit` must match how the frontend actually reads the sources.
    yosys-slang compiles each file as its own compilation unit unless
    ``--single-unit`` is passed, so by default the macro table is reset per
    top-level source and re-seeded from `defines` — carrying a `` `define ``
    from one file into the next would suppress an `` `ifndef ``-guarded
    declaration that the compiler does see. With `single_unit` the table is
    shared, matching the one-compilation-unit read.

    Headers always share their includer's macro table, because `` `include ``
    is textual.

    `undefineall_keeps_predefines` selects the `` `undefineall `` semantics of
    the frontend in use: True for slang, whose `undefineAll()` re-applies
    `options.predefines`, and False for Yosys's own `read_verilog`, which
    clears its `global_defines_cache` as well. It defaults to the slang
    behaviour, that being the frontend whose miscompilation this gate exists
    for.
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
