#!/usr/bin/env python3
"""Translate a preprocessed Souffle program (as dumped by `ctadl dump-analysis`)
into a FlowLog program.

The translator covers the Souffle features used by CTADL.  Anything it does not
understand is reported as an error, so that unsupported features stay visible.

Rewrites performed (see README.md for the rationale of each one):
  * nested `.comp` declarations are hoisted to top level (FlowLog cannot resolve
    a nested component from a nested `.init`);
  * a relation name in a component's clauses denotes what Souffle resolves it
    to (the innermost enclosing instance that declares it or reaches it
    through an `.init`, else the program), as in FlowLog; a name that denotes
    different relations in different instances, or an enclosing instance's
    relation with a choice-domain or a type parameter, is rejected
    (resolve_decl);
  * as in Souffle, an atom or directive of an undefined relation and an atom
    of the wrong arity are errors, also in clauses the translation drops
    (check_relations);
  * record types are flattened into one column per field;
  * `.functor` declarations become `.extern fn`, `@F(..)` becomes `F(..)`;
  * variadic `cat` becomes nested binary `cat`, and `to_string` of a symbol
    becomes the symbol, as Souffle computes it (FlowLog's takes numbers only);
  * `range(lo, hi)` generators read a precomputed `FlowlogRange(hi, i)` table
    when 0 <= lo and hi < 1024, so the clause fires in a single iteration as
    in Souffle; other bounds fall back to a recursive helper relation;
  * `choice-domain` is handled by a per-relation policy (drop / min lattice /
    "min-choice" encoding through an interned int64 key; min-choice unless the
    policy file says otherwise); inside recursion,
    reads of a min-choice relation go through `choice_claimN` UDFs so that the
    loop sees a single witness per key.  The key's priority bits follow the
    clause order Souffle evaluates, i.e. source order as permuted by its AST
    transformers (see souffle_order_key), so both engines keep the first
    candidate from the same clause;
  * positive uses of `inline` relations are expanded into the clause bodies,
    as Souffle does (a materialised relation would delay its consumers by one
    iteration inside recursion); relations whose clauses are given per
    component instance, or that are used negated or inside an aggregate, are
    materialised, and unused ones are dropped;
  * negations over computed values read a `NegPre*` helper relation that
    materialises the positive part first, expression arguments of aggregated
    atoms are bound inside the aggregate body, and facts with computed
    arguments are anchored on a one-row `FlowlogUnit` relation;
  * `.output`/`.input` directives inside components are recorded in the
    schema manifest once per instance (`inst.R`). FlowLog writes one
    `.output` per relation, so each further file output of a top-level
    relation writes a `FlowlogOutputN` copy of it (split_outputs);
  * `true`/`false`, negated comparisons, self equalities, expression arguments
    of body atoms, keyword-named variables and placeholder-only aggregates are
    normalised into forms FlowLog accepts;
  * builtins whose FlowLog counterparts differ from Souffle's call ports with
    Souffle's semantics in souffle2flowlog.rs (SOUFFLE_UDFS).  The partial ones
    (`to_number`, `/`, `%`) are total there, and Souffle's stops on them are
    reproduced by check rules where that is sound (add_partial_checks);
  * integers are kept on Souffle's 64 bits: constant expressions are computed
    as Souffle does, with wrap-around (fold_constants), and the expressions
    FlowLog would type as int32 are pinned with `as(_, int64)` or
    `as(_, uint64)` (anchor_widths);
  * in rules that involve unsigned values, a variable equated to a constant
    is replaced by the constant as Souffle does, so each copy takes the type
    of its own context (alias_constants), and a `/` or `%` of literals only
    is computed in the type of its context (type_context_partials);
  * a `.plan` keeps the join order of Souffle's version 0 (a delta on the
    rule's first recursive atom), as FlowLog joins every version of a rule
    in one order, where each clause of the translated rule reads the atoms
    the plan orders (flowlog_plan); the other versions are dropped;
  * `.pragma` and relation representation qualifiers are dropped
    (`.limitsize`, which truncates a relation, is rejected).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, replace
from typing import Any


class TranslateError(Exception):
    pass


# ============================================================================
# Lexer
# ============================================================================

DIRECTIVES = {
    ".decl",
    ".type",
    ".input",
    ".output",
    ".printsize",
    ".limitsize",
    ".comp",
    ".init",
    ".override",
    ".pragma",
    ".functor",
    ".plan",
    ".include",
    ".once",
    ".number_type",
    ".symbol_type",
}
PUNCT2 = [":-", "<:", "!=", "<=", ">="]
PUNCT1 = "()[]{},;.:=<>!+-*/%^@|&"


@dataclass
class Tok:
    kind: str  # id | num | str | dir | p | eof
    val: Any
    line: int

    def __repr__(self) -> str:
        return f"{self.kind}:{self.val!r}@{self.line}"


_ID_RE = re.compile(r"[A-Za-z_?][A-Za-z0-9_?]*(?:\.[A-Za-z_?][A-Za-z0-9_?]*)*")
_NUM_RE = re.compile(r"0x[0-9a-fA-F]+|0b[01]+|\d+\.\d+|\d+[uU]?")


def lex(src: str) -> list[Tok]:
    toks: list[Tok] = []
    i, n, line = 0, len(src), 1
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r\f":
            i += 1
            continue
        if c == "#" and (i == 0 or src[i - 1] == "\n"):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise TranslateError(f"line {line}: unterminated comment")
            line += src.count("\n", i, j)
            i = j + 2
            continue
        if c == '"':
            j = i + 1
            buf: list[str] = []
            while True:
                if j >= n:
                    raise TranslateError(f"line {line}: unterminated string")
                ch = src[j]
                if ch == '"':
                    break
                if ch == "\\":
                    nx = src[j + 1] if j + 1 < n else ""
                    # Souffle's escapes; it rejects any other
                    esc = {
                        '"': '"',
                        "'": "'",
                        "\\": "\\",
                        "a": "\a",
                        "b": "\b",
                        "f": "\f",
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                        "v": "\v",
                    }
                    if nx not in esc:
                        raise TranslateError(
                            f"line {line}: unknown escape sequence \\{nx}"
                        )
                    buf.append(esc[nx])
                    j += 2
                    continue
                if ch == "\n":
                    line += 1
                buf.append(ch)
                j += 1
            toks.append(Tok("str", "".join(buf), line))
            i = j + 1
            continue
        if c == "." and i + 1 < n and src[i + 1].isalpha():
            m = re.match(r"\.[A-Za-z_]+", src[i:])
            assert m
            if m.group(0) in DIRECTIVES:
                toks.append(Tok("dir", m.group(0), line))
                i += len(m.group(0))
                continue
        if c.isalpha() or c in "_?":
            if src.startswith("choice-domain", i):
                toks.append(Tok("id", "choice-domain", line))
                i += len("choice-domain")
                continue
            m = _ID_RE.match(src, i)
            assert m
            toks.append(Tok("id", m.group(0), line))
            i = m.end()
            continue
        if c.isdigit():
            m = _NUM_RE.match(src, i)
            assert m
            s = m.group(0)
            if s.startswith("0x"):
                v: Any = int(s, 16)
            elif s.startswith("0b"):
                v = int(s[2:], 2)
            elif "." in s:
                v = float(s)
            else:
                v = int(s.rstrip("uU"))
            toks.append(Tok("num", v, line))
            i = m.end()
            continue
        two = src[i : i + 2]
        if two in PUNCT2:
            toks.append(Tok("p", two, line))
            i += 2
            continue
        if c in PUNCT1:
            toks.append(Tok("p", c, line))
            i += 1
            continue
        if c == "$":
            raise TranslateError(
                f"line {line}: algebraic data type constructors are not supported"
            )
        raise TranslateError(f"line {line}: unexpected character {c!r}")
    toks.append(Tok("eof", None, line))
    return toks


# ============================================================================
# AST
# ============================================================================


@dataclass
class Var:
    name: str


@dataclass
class Wild:
    pass


@dataclass
class Num:
    val: Any


@dataclass
class Str:
    val: str


@dataclass
class Nil:
    pass


@dataclass
class Rec:
    elems: list


@dataclass
class Bin:
    op: str
    l: Any
    r: Any


@dataclass
class Un:
    op: str
    e: Any


@dataclass
class Call:
    name: str
    args: list
    functor: bool = False


@dataclass
class Agg:
    kind: str  # count | sum | min | max | mean
    target: Any  # None for count
    body: list  # list of conjunctions (disjunction)


@dataclass
class Cast:
    e: Any
    ty: str


# body literals
@dataclass
class Atom:
    name: str
    args: list


@dataclass
class Neg:
    atom: Atom


@dataclass
class Cmp:
    op: str
    l: Any
    r: Any


@dataclass
class Pred:
    name: str  # match | contains
    args: list
    neg: bool = False


@dataclass
class Group:
    disj: list  # list of conjunctions


@dataclass
class BoolLit:
    val: bool


# items
@dataclass
class TypeDecl:
    name: str
    kind: str  # '=' or '<:'
    rhs: Any  # str (alias), ('record', [(f, t)]), ('union', [t])


@dataclass
class RelDecl:
    name: str
    attrs: list  # [(name, type)]
    quals: list
    choice: list  # list of list of attr names


@dataclass
class IODecl:
    kind: str
    name: str
    params: list  # [(key, value)]


@dataclass
class Comp:
    name: str
    tparams: list
    supers: list
    items: list


@dataclass
class Init:
    name: str
    comp: str
    targs: list


@dataclass
class Override:
    name: str


@dataclass
class Pragma:
    args: list


@dataclass
class Functor:
    name: str
    params: list
    ret: str
    stateful: bool


@dataclass
class Plan:
    """A Souffle `.plan`: a join order (1-based positions among the positive
    atoms) per rule version, and the names of the positive atoms it orders
    (note_plan_atoms; None if the rule's clauses differ in them)."""

    orders: dict
    atoms: tuple | None


@dataclass
class Rule:
    heads: list
    body: Any  # None for facts, else list of conjunctions
    line: int = 0
    # min-choice clauses only: which of the clauses Souffle makes of the rule
    # this is (DNF conjunct, inline expansion path), and the transformer
    # flags that place it in Souffle's clause order (see souffle_order_key)
    sub: Any = None
    sx: Any = None
    # the rule's Souffle `.plan` (a Plan), which finish_rule turns into
    # FlowLog's join order (a tuple of positions) or drops
    plan: Any = None


@dataclass
class Raw:
    text: str


# ============================================================================
# Parser
# ============================================================================

BUILTIN_FUNCS = {
    "cat",
    "ord",
    "strlen",
    "substr",
    "to_string",
    "to_number",
    "to_unsigned",
    "to_float",
    "range",
    "min",
    "max",
    "lnot",
    "bnot",
    "autoinc",
}
AGG_KINDS = {"count", "sum", "min", "max", "mean"}
CMP_OPS = {"=", "!=", "<", "<=", ">", ">="}
BIN_PREC = {
    "lor": 1,
    "lxor": 2,
    "land": 3,
    "bor": 4,
    "bxor": 5,
    "band": 6,
    "bshl": 7,
    "bshr": 7,
    "bshru": 7,
    "+": 10,
    "-": 10,
    "*": 20,
    "/": 20,
    "%": 20,
    "^": 30,
}
RIGHT_ASSOC = {"^"}


class ParseFail(Exception):
    pass


class Parser:
    def __init__(self, toks: list[Tok]):
        self.toks = toks
        self.i = 0

    # -- token helpers
    def peek(self, k: int = 0) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def at(self, kind: str, val: Any = None, k: int = 0) -> bool:
        t = self.peek(k)
        return t.kind == kind and (val is None or t.val == val)

    def at_p(self, val: str, k: int = 0) -> bool:
        return self.at("p", val, k)

    def expect(self, kind: str, val: Any = None) -> Tok:
        t = self.next()
        if t.kind != kind or (val is not None and t.val != val):
            raise ParseFail(f"line {t.line}: expected {val or kind}, got {t.val!r}")
        return t

    def expect_p(self, val: str) -> Tok:
        return self.expect("p", val)

    # -- program
    def parse_program(self) -> list:
        items = self.parse_items(top=True)
        self.expect("eof")
        return items

    def parse_items(self, top: bool) -> list:
        items: list = []
        while True:
            t = self.peek()
            if t.kind == "eof" or (t.kind == "p" and t.val == "}"):
                return items
            if t.kind == "dir":
                d = t.val
                if d == ".decl":
                    items.extend(self.parse_decl())
                elif d == ".type":
                    items.append(self.parse_type())
                elif d in (".input", ".output", ".printsize", ".limitsize"):
                    items.extend(self.parse_io())
                elif d == ".comp":
                    items.append(self.parse_comp())
                elif d == ".init":
                    items.append(self.parse_init())
                elif d == ".override":
                    self.next()
                    items.append(Override(self.expect("id").val))
                elif d == ".pragma":
                    self.next()
                    args = []
                    while self.at("str"):
                        args.append(self.next().val)
                    items.append(Pragma(args))
                elif d == ".functor":
                    items.append(self.parse_functor())
                elif d == ".plan":
                    orders = self.parse_plan()
                    if (
                        items
                        and isinstance(items[-1], Rule)
                        and items[-1].body is not None
                    ):
                        items[-1] = replace(items[-1], plan=Plan(orders, None))
                else:
                    raise TranslateError(f"line {t.line}: unsupported directive {d}")
            else:
                items.append(self.parse_rule())

    def parse_decl(self) -> list:
        self.expect("dir", ".decl")
        names = [self.expect("id").val]
        while self.at_p(","):
            self.next()
            names.append(self.expect("id").val)
        self.expect_p("(")
        attrs = []
        if not self.at_p(")"):
            while True:
                an = self.expect("id").val
                self.expect_p(":")
                at = self.expect("id").val
                attrs.append((an, at))
                if self.at_p(","):
                    self.next()
                    continue
                break
        self.expect_p(")")
        quals = []
        while self.at("id") and self.peek().val in (
            "inline",
            "no_inline",
            "magic",
            "no_magic",
            "brie",
            "btree",
            "btree_delete",
            "eqrel",
            "overridable",
            "input",
            "output",
            "printsize",
            "override",
        ):
            quals.append(self.next().val)
        choice = []
        if self.at("id", "choice-domain"):
            self.next()
            while True:
                if self.at_p("("):
                    self.next()
                    grp = [self.expect("id").val]
                    while self.at_p(","):
                        self.next()
                        grp.append(self.expect("id").val)
                    self.expect_p(")")
                else:
                    grp = [self.expect("id").val]
                choice.append(grp)
                if self.at_p(",") and (
                    self.at_p("(", 1)
                    or (
                        self.at("id", None, 1)
                        and not self.at_p("(", 2)
                        and not self.at_p(".", 2)
                    )
                ):
                    self.next()
                    continue
                break
        if "eqrel" in quals:
            raise TranslateError(f"eqrel relation {names} is not supported")
        decls: list = [
            RelDecl(nm, list(attrs), list(quals), [list(c) for c in choice])
            for nm in names
        ]
        # the deprecated qualifiers stand for directives with default parameters
        return decls + [
            IODecl("." + q, nm, [])
            for q in ("input", "output", "printsize")
            if q in quals
            for nm in names
        ]

    def parse_type(self) -> TypeDecl:
        self.expect("dir", ".type")
        name = self.expect("id").val
        t = self.next()
        if t.kind != "p" or t.val not in ("=", "<:"):
            raise ParseFail(f"line {t.line}: bad .type")
        if self.at_p("["):
            self.next()
            fields = []
            if not self.at_p("]"):
                while True:
                    fn = self.expect("id").val
                    self.expect_p(":")
                    ft = self.expect("id").val
                    fields.append((fn, ft))
                    if self.at_p(","):
                        self.next()
                        continue
                    break
            self.expect_p("]")
            return TypeDecl(name, t.val, ("record", fields))
        first = self.expect("id").val
        if self.at_p("{"):
            raise TranslateError(
                f".type {name}: algebraic data types are not supported"
            )
        if self.at_p("|"):
            alts = [first]
            while self.at_p("|"):
                self.next()
                alts.append(self.expect("id").val)
            return TypeDecl(name, t.val, ("union", alts))
        return TypeDecl(name, t.val, first)

    def parse_io(self) -> list:
        kind = self.next().val
        names = [self.expect("id").val]
        while self.at_p(","):
            self.next()
            names.append(self.expect("id").val)
        params = []
        if self.at_p("("):
            self.next()
            if not self.at_p(")"):
                while True:
                    k = self.expect("id").val
                    self.expect_p("=")
                    v = self.next()
                    if v.kind not in ("str", "id", "num"):
                        raise ParseFail(f"line {v.line}: bad IO parameter")
                    params.append((k, str(v.val)))
                    if self.at_p(","):
                        self.next()
                        continue
                    break
            self.expect_p(")")
        return [IODecl(kind, nm, list(params)) for nm in names]

    def parse_comp(self) -> Comp:
        self.expect("dir", ".comp")
        name = self.expect("id").val
        tparams = []
        if self.at_p("<"):
            self.next()
            tparams.append(self.expect("id").val)
            while self.at_p(","):
                self.next()
                tparams.append(self.expect("id").val)
            self.expect_p(">")
        supers = []
        if self.at_p(":"):
            self.next()
            while True:
                sname = self.expect("id").val
                sargs = []
                if self.at_p("<"):
                    self.next()
                    sargs.append(self.expect("id").val)
                    while self.at_p(","):
                        self.next()
                        sargs.append(self.expect("id").val)
                    self.expect_p(">")
                supers.append((sname, sargs))
                if self.at_p(","):
                    self.next()
                    continue
                break
        self.expect_p("{")
        items = self.parse_items(top=False)
        self.expect_p("}")
        return Comp(name, tparams, supers, items)

    def parse_init(self) -> Init:
        self.expect("dir", ".init")
        name = self.expect("id").val
        self.expect_p("=")
        comp = self.expect("id").val
        targs = []
        if self.at_p("<"):
            self.next()
            targs.append(self.expect("id").val)
            while self.at_p(","):
                self.next()
                targs.append(self.expect("id").val)
            self.expect_p(">")
        return Init(name, comp, targs)

    def parse_functor(self) -> Functor:
        self.expect("dir", ".functor")
        name = self.expect("id").val
        self.expect_p("(")
        params = []
        if not self.at_p(")"):
            while True:
                pn = self.expect("id").val
                self.expect_p(":")
                pt = self.expect("id").val
                params.append((pn, pt))
                if self.at_p(","):
                    self.next()
                    continue
                break
        self.expect_p(")")
        self.expect_p(":")
        ret = self.expect("id").val
        stateful = False
        if self.at("id", "stateful"):
            self.next()
            stateful = True
        return Functor(name, params, ret, stateful)

    def parse_plan(self) -> dict:
        self.expect("dir", ".plan")
        orders: dict = {}
        while True:
            version = int(self.expect("num").val)
            self.expect_p(":")
            self.expect_p("(")
            order = []
            if not self.at_p(")"):
                order.append(int(self.expect("num").val))
                while self.at_p(","):
                    self.next()
                    order.append(int(self.expect("num").val))
            self.expect_p(")")
            orders[version] = tuple(order)
            if self.at_p(",") and self.at("num", None, 1):
                self.next()
                continue
            return orders

    # -- rules
    def parse_rule(self) -> Rule:
        line = self.peek().line
        heads = [self.parse_head_atom()]
        while self.at_p(","):
            self.next()
            heads.append(self.parse_head_atom())
        body = None
        if self.at_p(":-"):
            self.next()
            body = self.parse_disj()
        self.expect_p(".")
        return Rule(heads, body, line)

    def parse_head_atom(self) -> Atom:
        t = self.expect("id")
        self.expect_p("(")
        args = self.parse_args(")")
        self.expect_p(")")
        return Atom(t.val, args)

    def parse_args(self, close: str) -> list:
        args = []
        if self.at_p(close):
            return args
        args.append(self.parse_expr())
        while self.at_p(","):
            self.next()
            args.append(self.parse_expr())
        return args

    def parse_disj(self) -> list:
        disj = [self.parse_conj()]
        while self.at_p(";"):
            self.next()
            disj.append(self.parse_conj())
        return disj

    def parse_conj(self) -> list:
        conj = [self.parse_lit()]
        while self.at_p(","):
            self.next()
            conj.append(self.parse_lit())
        return conj

    def parse_lit(self) -> Any:
        t = self.peek()
        if t.kind == "p" and t.val == "!":
            self.next()
            if self.at_p("("):
                save = self.i
                try:
                    inner = self.parse_constraint()
                    return negate(inner)
                except ParseFail:
                    self.i = save
                self.next()
                d = self.parse_disj()
                self.expect_p(")")
                if len(d) == 1 and len(d[0]) == 1:
                    return negate(d[0][0])
                raise TranslateError(
                    f"line {t.line}: negation of a compound body is not supported"
                )
            lit = self.parse_lit()
            return negate(lit)
        if t.kind == "id" and t.val in ("true", "false") and not self.at_p("(", 1):
            self.next()
            return BoolLit(t.val == "true")
        if t.kind == "p" and t.val == "(":
            save = self.i
            try:
                return self.parse_constraint()
            except ParseFail:
                self.i = save
            self.next()
            d = self.parse_disj()
            self.expect_p(")")
            return Group(d)
        if t.kind == "id" and self.at_p("(", 1):
            if t.val in ("match", "contains"):
                self.next()
                self.expect_p("(")
                args = self.parse_args(")")
                self.expect_p(")")
                return Pred(t.val, args)
            # Souffle keywords cannot name a relation: `as(x, T) > 0` and
            # `sum(x) : {...} > 0` start a constraint
            if t.val not in BUILTIN_FUNCS and t.val not in AGG_KINDS and t.val != "as":
                self.next()
                self.expect_p("(")
                args = self.parse_args(")")
                self.expect_p(")")
                return Atom(t.val, args)
        return self.parse_constraint()

    def parse_constraint(self) -> Cmp:
        l = self.parse_expr()
        t = self.peek()
        if t.kind != "p" or t.val not in CMP_OPS:
            raise ParseFail(f"line {t.line}: expected comparison, got {t.val!r}")
        self.next()
        r = self.parse_expr()
        return Cmp(t.val, l, r)

    # -- expressions
    def parse_expr(self, min_prec: int = 0) -> Any:
        lhs = self.parse_unary()
        while True:
            t = self.peek()
            op = None
            if t.kind == "p" and t.val in BIN_PREC:
                op = t.val
            elif t.kind == "id" and t.val in BIN_PREC:
                op = t.val
            if op is None:
                return lhs
            prec = BIN_PREC[op]
            if prec < min_prec:
                return lhs
            self.next()
            nxt = prec if op in RIGHT_ASSOC else prec + 1
            rhs = self.parse_expr(nxt)
            lhs = Bin(op, lhs, rhs)

    def parse_unary(self) -> Any:
        # Souffle's unary -, bnot and lnot bind looser than ^: -2^2 is -(2^2)
        t = self.peek()
        if t.kind == "p" and t.val == "-":
            self.next()
            e = self.parse_expr(BIN_PREC["^"])
            if isinstance(e, Num):
                return Num(-e.val)
            return Un("-", e)
        if t.kind == "id" and t.val in ("bnot", "lnot") and not self.at_p("(", 1):
            self.next()
            return Un(t.val, self.parse_expr(BIN_PREC["^"]))
        return self.parse_primary()

    def parse_primary(self) -> Any:
        t = self.next()
        if t.kind == "num":
            return Num(t.val)
        if t.kind == "str":
            return Str(t.val)
        if t.kind == "p" and t.val == "(":
            e = self.parse_expr()
            self.expect_p(")")
            return e
        if t.kind == "p" and t.val == "[":
            elems = self.parse_args("]")
            self.expect_p("]")
            return Rec(elems)
        if t.kind == "p" and t.val == "@":
            name = self.expect("id").val
            self.expect_p("(")
            args = self.parse_args(")")
            self.expect_p(")")
            return Call(name, args, functor=True)
        if t.kind == "id":
            v = t.val
            if v == "_":
                return Wild()
            if v == "nil":
                return Nil()
            if v in AGG_KINDS and self._is_aggregate(v):
                return self.parse_aggregate(v)
            if v == "as" and self.at_p("("):
                self.next()
                e = self.parse_expr()
                self.expect_p(",")
                ty = self.expect("id").val
                self.expect_p(")")
                return Cast(e, ty)
            if self.at_p("("):
                if v not in BUILTIN_FUNCS:
                    raise ParseFail(f"line {t.line}: {v}(...) is not an expression")
                self.next()
                args = self.parse_args(")")
                self.expect_p(")")
                return Call(v, args)
            return Var(v)
        raise ParseFail(f"line {t.line}: unexpected {t.val!r} in expression")

    def _is_aggregate(self, kind: str) -> bool:
        if kind == "count":
            return self.at_p(":")
        if not self.at_p("("):
            return True
        # min(a, b) functional form vs `min (x) : {...}`
        depth, k = 0, 0
        while True:
            t = self.peek(k)
            if t.kind == "eof":
                return False
            if t.kind == "p" and t.val == "(":
                depth += 1
            elif t.kind == "p" and t.val == ")":
                depth -= 1
                if depth == 0:
                    return self.at_p(":", k + 1)
            k += 1

    def parse_aggregate(self, kind: str) -> Agg:
        target = None
        if kind != "count":
            target = self.parse_expr()
        self.expect_p(":")
        if self.at_p("{"):
            self.next()
            body = self.parse_disj()
            self.expect_p("}")
        else:
            t = self.expect("id")
            self.expect_p("(")
            args = self.parse_args(")")
            self.expect_p(")")
            body = [[Atom(t.val, args)]]
        return Agg(kind, target, body)


NEG_CMP = {"=": "!=", "!=": "=", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}


def negate(lit: Any) -> Any:
    if isinstance(lit, Atom):
        return Neg(lit)
    if isinstance(lit, Neg):
        return lit.atom
    if isinstance(lit, Cmp):
        return Cmp(NEG_CMP[lit.op], lit.l, lit.r)
    if isinstance(lit, Pred):
        return Pred(lit.name, lit.args, not lit.neg)
    if isinstance(lit, BoolLit):
        return BoolLit(not lit.val)
    if isinstance(lit, Group) and len(lit.disj) == 1 and len(lit.disj[0]) == 1:
        return negate(lit.disj[0][0])
    raise TranslateError(f"cannot negate {lit}")


# ============================================================================
# Generic traversal helpers
# ============================================================================


def expr_children(e: Any) -> list:
    if isinstance(e, Bin):
        return [e.l, e.r]
    if isinstance(e, Un):
        return [e.e]
    if isinstance(e, Call):
        return list(e.args)
    if isinstance(e, Rec):
        return list(e.elems)
    if isinstance(e, Cast):
        return [e.e]
    return []


def expr_vars(e: Any, out: set, include_agg: bool = True) -> set:
    if isinstance(e, Var):
        out.add(e.name)
    elif isinstance(e, Agg):
        if include_agg:
            if e.target is not None:
                expr_vars(e.target, out)
            for conj in e.body:
                for lit in conj:
                    lit_vars(lit, out)
    else:
        for c in expr_children(e):
            expr_vars(c, out, include_agg)
    return out


def lit_vars(lit: Any, out: set, include_agg: bool = True) -> set:
    if isinstance(lit, Atom):
        for a in lit.args:
            expr_vars(a, out, include_agg)
    elif isinstance(lit, Neg):
        lit_vars(lit.atom, out, include_agg)
    elif isinstance(lit, Cmp):
        expr_vars(lit.l, out, include_agg)
        expr_vars(lit.r, out, include_agg)
    elif isinstance(lit, Pred):
        for a in lit.args:
            expr_vars(a, out, include_agg)
    elif isinstance(lit, Group):
        for conj in lit.disj:
            for l2 in conj:
                lit_vars(l2, out, include_agg)
    return out


def map_expr(e: Any, f) -> Any:
    """Bottom-up rewrite of an expression (aggregate bodies are not entered)."""
    if isinstance(e, Bin):
        e = Bin(e.op, map_expr(e.l, f), map_expr(e.r, f))
    elif isinstance(e, Un):
        e = Un(e.op, map_expr(e.e, f))
    elif isinstance(e, Call):
        e = Call(e.name, [map_expr(a, f) for a in e.args], e.functor)
    elif isinstance(e, Rec):
        e = Rec([map_expr(a, f) for a in e.elems])
    elif isinstance(e, Cast):
        e = Cast(map_expr(e.e, f), e.ty)
    elif isinstance(e, Agg):
        e = Agg(e.kind, None if e.target is None else map_expr(e.target, f), e.body)
    return f(e)


def map_lit_top(lit: Any, g) -> Any:
    """Apply `g` to each top-level expression of a literal."""
    if isinstance(lit, Atom):
        return Atom(lit.name, [g(a) for a in lit.args])
    if isinstance(lit, Neg):
        return Neg(map_lit_top(lit.atom, g))
    if isinstance(lit, Cmp):
        return Cmp(lit.op, g(lit.l), g(lit.r))
    if isinstance(lit, Pred):
        return Pred(lit.name, [g(a) for a in lit.args], lit.neg)
    if isinstance(lit, Group):
        return Group([[map_lit_top(l2, g) for l2 in conj] for conj in lit.disj])
    return lit


def map_lit_exprs(lit: Any, f) -> Any:
    return map_lit_top(lit, lambda e: map_expr(e, f))


def deep_map_expr(e: Any, f) -> Any:
    """Bottom-up rewrite of an expression, aggregate bodies (at any depth)
    included."""

    def g(x):
        if isinstance(x, Agg):
            x = Agg(
                x.kind, x.target, [[deep_map_lit(l2, f) for l2 in c] for c in x.body]
            )
        return f(x)

    return map_expr(e, g)


def deep_map_lit(lit: Any, f) -> Any:
    return map_lit_top(lit, lambda e: deep_map_expr(e, f))


def subst_expr(e: Any, sub: dict) -> Any:
    """Replace the variables named in `sub` everywhere in an expression."""
    return deep_map_expr(
        e, lambda x: sub[x.name] if isinstance(x, Var) and x.name in sub else x
    )


def subst_lit(lit: Any, sub: dict) -> Any:
    return deep_map_lit(
        lit, lambda x: sub[x.name] if isinstance(x, Var) and x.name in sub else x
    )


def map_outer_aggs(e: Any, f) -> Any:
    """Replace each outermost aggregate `a` of an expression by `f(a)`."""
    if isinstance(e, Agg):
        return f(e)
    if isinstance(e, Bin):
        return Bin(e.op, map_outer_aggs(e.l, f), map_outer_aggs(e.r, f))
    if isinstance(e, Un):
        return Un(e.op, map_outer_aggs(e.e, f))
    if isinstance(e, Call):
        return Call(e.name, [map_outer_aggs(a, f) for a in e.args], e.functor)
    if isinstance(e, Rec):
        return Rec([map_outer_aggs(a, f) for a in e.elems])
    if isinstance(e, Cast):
        return Cast(map_outer_aggs(e.e, f), e.ty)
    return e


def has_agg(x: Any) -> bool:
    """Whether an expression or literal contains an aggregate."""
    found: list = []

    def probe(e):
        if isinstance(e, Agg):
            found.append(e)
        return e

    if isinstance(x, (Atom, Neg, Cmp, Pred, Group)):
        map_lit_exprs(x, probe)
    else:
        map_expr(x, probe)
    return bool(found)


def has_source(conj: list) -> bool:
    """Whether FlowLog sees a conjunction read a relation: a positive atom,
    an aggregate, or a disjunction each of whose branches does."""
    for lit in conj:
        if isinstance(lit, Atom) or isinstance(lit, (Cmp, Pred)) and has_agg(lit):
            return True
        if isinstance(lit, Group) and lit.disj and all(has_source(c) for c in lit.disj):
            return True
    return False


def iter_rules(items: list, scope: tuple = ()):
    for it in items:
        if isinstance(it, Rule):
            yield scope, it
        elif isinstance(it, Comp):
            yield from iter_rules(it.items, scope + (it.name,))


def rule_vars(r: Rule) -> set:
    """Every variable a rule spells, aggregate internals included."""
    out: set = set()
    for h in r.heads:
        lit_vars(h, out)
    for conj in r.body or []:
        for lit in conj:
            lit_vars(lit, out)
    return out


CMP_FNS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "=": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def fold_cmp(op: str, l: Any, r: Any) -> Any:
    """`l op r`, or its truth value when both sides are integer literals."""
    if (
        isinstance(l, Num)
        and isinstance(r, Num)
        and isinstance(l.val, int)
        and isinstance(r.val, int)
    ):
        return CMP_FNS[op](l.val, r.val)
    return Cmp(op, l, r)


# Souffle's integer types (64-bit domain) as FlowLog primitives, and their ranges.
INT_RANGES = {"int64": (-(1 << 63), (1 << 63) - 1), "uint64": (0, (1 << 64) - 1)}
INT32_RANGE = (-(1 << 31), (1 << 31) - 1)
# The operators FlowLog evaluates itself (the others call SOUFFLE_UDFS); it
# writes the shifts as calls of runtime functions.
INT_OPS = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "band": lambda a, b: a & b,
    "bor": lambda a, b: a | b,
    "bxor": lambda a, b: a ^ b,
    "bshl": lambda a, b: a << (b & 63),
    "bshr": lambda a, b: a >> (b & 63),
    "bshru": lambda a, b: (a & ((1 << 64) - 1)) >> (b & 63),
}
SHIFT_OPS = ("bshl", "bshr", "bshru")
# The operators Souffle types by their operands (so a constant expression of
# them takes its type from where it is used), and those of them (and the
# comparisons) whose result differs on `number` and `unsigned` operands of
# the same bits; the others wrap alike.
CONST_OPS = {"+", "-", "*", "/", "%", "^", "band", "bor", "bxor"} | set(SHIFT_OPS)
SIGNED_OPS = {"/", "%", "^", "<", "<=", ">", ">="} | set(SHIFT_OPS)
# Souffle builtins whose parameters have fixed types.
TYPED_PARAM_BUILTINS = {"cat", "strlen", "substr", "to_number"}


def wrap_int(v: int, prim: str) -> int:
    """v modulo 2**64, as a value of the primitive int64 or uint64."""
    v &= (1 << 64) - 1
    return v - (1 << 64) if prim == "int64" and v >> 63 else v


class EmptyAggregate(Exception):
    """A min/max aggregate over a body that is always false: no value."""


def eval_const(e: Any, prim: str, env: dict) -> int | None:
    """The value Souffle gives an integer constant expression of type `prim`
    (int64 or uint64): literals, variables valued in `env` and FlowLog's
    native operators, computed on 64 bits with wrap-around.  Shift counts are
    taken modulo 64; `bshr` is arithmetic on int64, `bshru` always logical.
    None if `e` is not such an expression, or a leaf is out of range."""
    if isinstance(e, Bin) and e.op in INT_OPS:
        a = eval_const(e.l, prim, env)
        b = eval_const(e.r, prim, env) if a is not None else None
        return None if b is None else wrap_int(INT_OPS[e.op](a, b), prim)
    if isinstance(e, Var):
        v = env.get(e.name)
    elif isinstance(e, Num) and isinstance(e.val, int):
        v = e.val
    else:
        return None
    lo, hi = INT_RANGES[prim]
    return v if v is not None and lo <= v <= hi else None


def arith_leaves(e: Any) -> list:
    """The operands of an arithmetic expression, through its operators."""
    if isinstance(e, Bin):
        return arith_leaves(e.l) + arith_leaves(e.r)
    return [e]


def map_arith_leaves(e: Any, f) -> Any:
    if isinstance(e, Bin):
        return Bin(e.op, map_arith_leaves(e.l, f), map_arith_leaves(e.r, f))
    return f(e)


def grounded_vars(conj: list) -> set:
    """Variables bound by a conjunction: positive atom arguments, plus
    equalities whose other side is already bound (to a fixpoint)."""
    g: set = set()
    for lit in conj:
        if isinstance(lit, Atom):
            for a in lit.args:
                if isinstance(a, Var):
                    g.add(a.name)
        elif (
            isinstance(lit, Cmp)
            and lit.op == "="
            and isinstance(lit.l, Var)
            and isinstance(lit.r, Agg)
        ):
            g.add(lit.l.name)
    changed = True
    while changed:
        changed = False
        for lit in conj:
            if isinstance(lit, Cmp) and lit.op == "=":
                for x, e in ((lit.l, lit.r), (lit.r, lit.l)):
                    if (
                        isinstance(x, Var)
                        and x.name not in g
                        and expr_vars(e, set()) <= g
                    ):
                        g.add(x.name)
                        changed = True
    return g


def all_aggs_in_lit(lit: Any, out: list) -> list:
    def visit(e):
        if isinstance(e, Agg):
            out.append(e)
        return e

    map_lit_exprs(lit, visit)
    return out


def body_atoms(body: list):
    """Yield every (positive or negative) atom reachable from a body,
    including atoms inside groups and aggregate bodies."""
    for conj in body:
        for lit in conj:
            yield from lit_atoms(lit)


def lit_atoms(lit: Any):
    if isinstance(lit, Atom):
        yield lit
    elif isinstance(lit, Neg):
        yield lit.atom
    elif isinstance(lit, Group):
        yield from body_atoms(lit.disj)
    for agg in all_aggs_in_lit(lit, []):
        yield from body_atoms(agg.body)


def rename_atoms(lit: Any, names: dict) -> Any:
    """Respell the atoms of a literal (aggregate bodies are not entered)."""
    if isinstance(lit, Atom):
        return Atom(names.get(lit.name, lit.name), lit.args)
    if isinstance(lit, Neg):
        return Neg(rename_atoms(lit.atom, names))
    if isinstance(lit, Group):
        return Group([[rename_atoms(l2, names) for l2 in c] for c in lit.disj])
    return lit


def prune_conj(conj: list) -> list | None:
    """Drop the constant-true (in)equalities of a conjunction; None when one
    is constant-false or a variable is equated to two different constants
    (or equated to and distinguished from the same one)."""
    out: list = []
    eq: dict = {}
    ne: dict = {}
    for lit in conj:
        if isinstance(lit, Cmp) and lit.op in ("=", "!="):
            l, r = lit.l, lit.r
            if (
                isinstance(l, (Str, Num))
                and isinstance(r, (Str, Num))
                and type(l) is type(r)
            ):
                if (l.val == r.val) == (lit.op == "="):
                    continue
                return None
            if isinstance(r, Var) and isinstance(l, (Str, Num)):
                l, r = r, l
            if isinstance(l, Var) and isinstance(r, (Str, Num)):
                key = (type(r).__name__, r.val)
                if lit.op == "=":
                    if eq.get(l.name, key) != key or key in ne.get(l.name, ()):
                        return None
                    eq[l.name] = key
                else:
                    if eq.get(l.name) == key:
                        return None
                    ne.setdefault(l.name, set()).add(key)
        out.append(lit)
    return out


def tarjan_scc(edges: dict) -> dict:
    """Map every node of a dependency graph to its strongly connected
    component id (iterative Tarjan)."""
    index: dict = {}
    low: dict = {}
    comp: dict = {}
    stack: list = []
    on_stack: set = set()
    counter = 0
    nodes = set(edges) | {d for ds in edges.values() for d in ds}
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(edges.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            pushed = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(edges.get(w, ()))))
                    pushed = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if pushed:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp[w] = v
                    if w == v:
                        break
    return comp


# ============================================================================
# Souffle's clause order (min-choice priorities)
#
# Souffle keeps, per choice-domain key, the first tuple it derives: its
# non-recursive clauses run before the loop and its recursive ones in every
# iteration, each group in the order of the relation's clause list.  That list
# starts in source order (a rule's body split into its DNF conjuncts, the
# clauses of component instances appended after the top-level ones) and is
# then permuted by the AST transformers: each one that rewrites a clause
# removes it and appends the new version, so the final order sorts by
# (rewritten by the last transformer, ..., by the first, source position).
# The helpers below predict the rewrites of Souffle 2.5's pipeline that occur
# in CTADL-like programs: ResolveAliases, inlining, constant constraint
# folding, RemoveEmptyRelations and PartitionBodyLiterals.
# ============================================================================

SX_GENERATORS = ("range",)


def souffle_dnf(disj: list) -> list:
    """The clause bodies Souffle's parser makes of a rule body (RuleBody in
    parser/ParserUtils.cpp): the disjunctive normal form with disjuncts left
    to right and nested groups multiplied out in order, repeated literals
    dropped and conjunctions subsumed by another one removed."""
    out: list = []
    for conj in disj:
        acc: list = [[]]
        for lit in conj:
            alts = souffle_dnf(lit.disj) if isinstance(lit, Group) else [[lit]]
            nxt: list = []
            for a in acc:
                for b in alts:
                    cur = list(a)
                    for x in b:
                        if x not in cur:
                            cur.append(x)
                    dnf_insert(nxt, cur)
            acc = nxt
        for c in acc:
            dnf_insert(out, c)
    return out


def dnf_insert(dnf: list, conj: list) -> None:
    if any(all(x in conj for x in c) for c in dnf):
        return
    dnf[:] = [c for c in dnf if not all(x in c for x in conj)]
    dnf.append(conj)


def flowlog_clauses(body: list) -> list:
    """The clause bodies FlowLog makes of a rule body: nested disjunctions
    multiplied out in order (unlike souffle_dnf, nothing is dropped)."""
    out: list = []
    for conj in body:
        acc: list = [[]]
        for lit in conj:
            alts = flowlog_clauses(lit.disj) if isinstance(lit, Group) else [[lit]]
            acc = [a + b for a in acc for b in alts]
        out.extend(acc)
    return out


def plan_atoms(clauses: list) -> tuple | None:
    """The names of the positive atoms a `.plan` orders, if every clause has
    the same ones, else None. A min-choice read `R_choice` stands for `R`."""
    seqs = {
        tuple(
            lit.name[: -len("_choice")] if lit.name.endswith("_choice") else lit.name
            for lit in c
            if isinstance(lit, Atom)
        )
        for c in clauses
    }
    return seqs.pop() if len(seqs) == 1 else None


def sx_materialized(a: Agg) -> bool:
    """Souffle's needsMaterializedRelation (MaterializeAggregationQueries):
    an aggregate whose body has several atoms, a nested aggregate, a
    multi-result functor or a variable repeated in its atom ranges over the
    distinct bindings of its named variables (a subclause relation whose head
    omits wildcards); any other ranges over the tuples of its single atom."""
    lits = [lit for conj in a.body for lit in conj]
    atoms = [lit for lit in lits if isinstance(lit, Atom)]
    if (
        len(atoms) > 1
        or any(has_agg(lit) for lit in lits)
        or (a.target is not None and has_agg(a.target))
    ):
        return True
    ranges: list = []
    names: list = []

    def probe_range(e):
        if isinstance(e, Call) and e.name == "range":
            ranges.append(e)
        return e

    def probe_var(e):
        if isinstance(e, Var):
            names.append(e.name)
        return e

    for lit in lits:
        map_lit_exprs(lit, probe_range)
    if a.target is not None:
        map_expr(a.target, probe_range)
    if ranges:
        return True
    for atom in atoms:
        map_lit_exprs(atom, probe_var)
    return len(names) != len(set(names))


def sx_const(e: Any) -> bool:
    return isinstance(e, (Num, Str, Nil))


def sx_records(e: Any, out: list) -> list:
    if isinstance(e, Rec):
        out.append(e)
    for c in expr_children(e):
        sx_records(c, out)
    return out


def sx_lit_exprs(lit: Any) -> list:
    if isinstance(lit, Atom):
        return list(lit.args)
    if isinstance(lit, Neg):
        return list(lit.atom.args)
    if isinstance(lit, (Cmp,)):
        return [lit.l, lit.r]
    if isinstance(lit, Pred):
        return list(lit.args)
    return []


def sx_equations(conj: list, out: list) -> list:
    """Every `=` of a clause body, aggregate bodies included."""
    for lit in conj:
        if isinstance(lit, Cmp) and lit.op == "=":
            out.append((lit.l, lit.r))
        for agg in all_aggs_in_lit(lit, []):
            for c in agg.body:
                sx_equations(c, out)
    return out


def sx_eq_maps(l: Any, r: Any, grounded: set) -> bool:
    """Whether ResolveAliases derives a substitution from `l = r`."""
    if l == r:
        return False
    if isinstance(l, Rec) and isinstance(r, Rec):
        return any(sx_eq_maps(a, b, grounded) for a, b in zip(l.elems, r.elems))
    if not isinstance(l, Var):
        l, r = r, l
    if not isinstance(l, Var):
        return False
    if isinstance(r, Var):
        return True
    if isinstance(r, Agg) or (
        isinstance(r, Call) and not r.functor and r.name in SX_GENERATORS
    ):
        return False
    if l.name in expr_vars(r, set()):
        return False
    return isinstance(r, Rec) or l.name not in grounded


def sx_resolve_aliases_changes(conj: list) -> bool:
    """Whether Souffle's ResolveAliases rewrites a clause with this body:
    it substitutes variables defined by equations (unless bound by a body
    atom), drops `t = t` and moves functors and casts out of body atoms."""
    grounded: set = set()
    for lit in conj:
        if isinstance(lit, Atom):
            for a in lit.args:
                if isinstance(a, Var):
                    grounded.add(a.name)
                for rec in sx_records(a, []):
                    grounded.update(x.name for x in rec.elems if isinstance(x, Var))
    for lit in conj:
        if isinstance(lit, Atom):
            for a in lit.args:
                if isinstance(a, (Bin, Un, Cast)) or (
                    isinstance(a, Call) and (a.functor or a.name not in SX_GENERATORS)
                ):
                    return True
                if any(
                    isinstance(x, (Bin, Un, Call))
                    for rec in sx_records(a, [])
                    for x in rec.elems
                ):
                    return True
        if isinstance(lit, Cmp) and lit.op == "=" and lit.l == lit.r:
            return True
    return any(sx_eq_maps(l, r, grounded) for l, r in sx_equations(conj, []))


def sx_record_constraint(conj: list) -> bool:
    """A `[..] = [..]` / `[..] != [..]` between record literals anywhere in
    the clause: FoldAnonymousRecords rewrites it."""
    for lit in conj:
        if (
            isinstance(lit, Cmp)
            and lit.op in ("=", "!=")
            and isinstance(lit.l, Rec)
            and isinstance(lit.r, Rec)
        ):
            return True
        if any(
            sx_record_constraint(c)
            for agg in all_aggs_in_lit(lit, [])
            for c in agg.body
        ):
            return True
    return False


def sx_const_constraint(conj: list) -> bool:
    """A `c1 = c2` / `c1 != c2` over constants: SimplifyConstantBinary-
    Constraints and RemoveBooleanConstraints rewrite the clause."""
    return any(
        isinstance(lit, Cmp)
        and lit.op in ("=", "!=")
        and sx_const(lit.l)
        and sx_const(lit.r)
        for lit in conj
    )


def sx_partitioned(head: Atom, conj: list) -> bool:
    """Whether PartitionBodyLiterals splits off body variables that are not
    connected to the head (every `_` is a distinct variable by then)."""
    parent: dict = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    fresh = iter(range(1 << 30))

    def names(lit: Any) -> list:
        out: list = []

        def ev(e: Any) -> None:
            if isinstance(e, Var):
                out.append(e.name)
            elif isinstance(e, Wild):
                out.append(f"_<{next(fresh)}>")
            elif isinstance(e, Agg):
                if e.target is not None:
                    ev(e.target)
                for c in e.body:
                    for l2 in c:
                        for x in sx_lit_exprs(l2):
                            ev(x)
            else:
                for c in expr_children(e):
                    ev(c)

        for e in sx_lit_exprs(lit):
            ev(e)
        return out

    body_vars: list = []
    for lit in [head] + conj:
        vs = names(lit)
        for v in vs:
            parent.setdefault(v, v)
        for v in vs[1:]:
            parent[find(v)] = find(vs[0])
        if lit is not head:
            body_vars += vs
    head_vars = names(head)
    if not head_vars:
        return bool(body_vars)
    root = find(head_vars[0])
    return any(find(v) != root for v in body_vars)


# ============================================================================
# Partial functions (see Translator.add_partial_checks)
# ============================================================================


def is_partial_call(e: Any) -> bool:
    """A call Souffle stops the program on for some arguments."""
    return (isinstance(e, Call) and not e.functor and e.name == "to_number") or (
        isinstance(e, Bin) and e.op in ("/", "%")
    )


def collect_exprs(x: Any, pred, out: list) -> list:
    """The distinct subexpressions of an expression or literal (aggregate
    bodies included) satisfying `pred`."""

    def probe(e):
        if pred(e) and e not in out:
            out.append(e)
        return e

    if isinstance(x, (Atom, Neg, Cmp, Pred, Group)):
        deep_map_lit(x, probe)
    else:
        deep_map_expr(x, probe)
    return out


def contains_term(x: Any, t: Any) -> bool:
    return bool(collect_exprs(x, lambda e: e == t, []))


def literal_div_fails(c: Bin) -> bool | None:
    """Whether the `/` or `%` of literals c fails in Souffle (a zero divisor,
    or INT64_MIN / -1 on number), when that does not depend on whether its
    context types it number or unsigned; else None."""

    def neg(x):
        if isinstance(x, Un) and x.op == "-":
            return Bin("-", Num(0), x.e)
        if isinstance(x, Num) and isinstance(x.val, int) and x.val < 0:
            return Bin("-", Num(0), Num(-x.val))
        return x

    l, r = map_expr(c.l, neg), map_expr(c.r, neg)
    fails = set()
    for prim in INT_RANGES:
        a, b = eval_const(l, prim, {}), eval_const(r, prim, {})
        if a is None or b is None:
            return None
        fails.add(
            b == 0 or (prim == "int64" and a == INT_RANGES["int64"][0] and b == -1)
        )
    return fails.pop() if len(fails) == 1 else None


def literal_expr(e: Any) -> bool:
    """A numeric literal, possibly computed: it takes its type from context."""
    if isinstance(e, Num):
        return True
    if isinstance(e, Un):
        return literal_expr(e.e)
    if isinstance(e, Bin):
        return literal_expr(e.l) and literal_expr(e.r)
    return False


def with_children(e: Any, kids: list) -> Any:
    if isinstance(e, Bin):
        return Bin(e.op, kids[0], kids[1])
    if isinstance(e, Un):
        return Un(e.op, kids[0])
    if isinstance(e, Call):
        return Call(e.name, kids, e.functor)
    if isinstance(e, Rec):
        return Rec(kids)
    if isinstance(e, Cast):
        return Cast(kids[0], e.ty)
    return e


def replace_terms(e: Any, pairs: list) -> Any:
    """Top-down: the first (term, replacement) pair whose term equals a node
    replaces it."""
    for t, v in pairs:
        if e == t:
            return v
    kids = expr_children(e)
    return with_children(e, [replace_terms(k, pairs) for k in kids]) if kids else e


def sx_negative_literals(e: Any) -> Any:
    """Souffle's parser reads `-5` as the constant -5."""
    if isinstance(e, Un) and e.op == "-" and isinstance(e.e, Num):
        return Num(-e.e.val)
    return e


def sx_resolve_aliases(head: Atom, body: list, tmp_names) -> tuple:
    """One run of Souffle 2.5's ResolveAliasesTransformer on a clause without
    aggregates, records or generators: unify the body's `=` equations (last
    first) into a substitution of the variables a body atom does not bind,
    apply it to the clause, drop `t = t` and move the functors and casts of
    positive atoms out into `v = t` equations over new variables (named by
    `tmp_names`)."""
    base: set = set()
    for lit in body:
        if isinstance(lit, Atom):
            base.update(a.name for a in lit.args if isinstance(a, Var))
    eqs = [(lit.l, lit.r) for lit in body if isinstance(lit, Cmp) and lit.op == "="]
    sub: dict = {}

    def new_mapping(v: str, t: Any) -> None:
        one = {v: t}
        eqs[:] = [(subst_expr(a, one), subst_expr(b, one)) for a, b in eqs]
        for k in sub:
            sub[k] = subst_expr(sub[k], one)
        sub.setdefault(v, t)

    while eqs:
        l, r = eqs.pop()
        if l == r or (not isinstance(l, Var) and not isinstance(r, Var)):
            continue
        if isinstance(l, Var) and isinstance(r, Var):
            new_mapping(l.name, r)
        elif not isinstance(l, Var):
            eqs.append((r, l))
        elif l.name not in expr_vars(r, set()) and l.name not in base:
            new_mapping(l.name, r)
    head = subst_lit(head, sub)
    body = [
        lit
        for lit in (subst_lit(x, sub) for x in body)
        if not (isinstance(lit, Cmp) and lit.op == "=" and lit.l == lit.r)
    ]
    terms: list = []
    for lit in body:
        if isinstance(lit, Atom):
            for a in lit.args:
                if isinstance(a, (Bin, Un, Call, Cast)) and a not in terms:
                    terms.append(a)
    pairs = [(t, Var(next(tmp_names))) for t in terms]
    body = [
        (
            Atom(lit.name, [replace_terms(a, pairs) for a in lit.args])
            if isinstance(lit, Atom)
            else lit
        )
        for lit in body
    ]
    return head, body + [Cmp("=", v, t) for t, v in pairs]


def var_occurrences(lit: Any) -> list:
    """The variable names of a literal, once per occurrence."""
    out: list = []

    def probe(e):
        if isinstance(e, Var):
            out.append(e.name)
        return e

    deep_map_lit(lit, probe)
    return out


def is_ord(e: Any) -> bool:
    return isinstance(e, Call) and not e.functor and e.name == "ord"


def sx_text(e: Any) -> str:
    """An expression as text, for messages."""

    def g(x):
        if isinstance(x, Cast):
            return Call("as", [x.e, Var(x.ty)])
        if isinstance(x, Un):
            return Call(x.op, [x.e])
        if isinstance(x, Bin) and x.op not in FL_PREC:
            return Call(x.op, [x.l, x.r])
        return x

    try:
        return pe(deep_map_expr(e, g))
    except TranslateError:
        return "an expression"


@dataclass
class SxClause:
    """A clause of the instantiated program (see Translator.sx_clauses)."""

    head: Atom
    body: list
    line: int
    top: bool  # from a top-level rule
    deps: set  # the relations the body reads, at any depth
    sim: Any = None  # (head, body) as Souffle evaluates it, when modelled
    kept: bool = False  # surely kept by Souffle's pipeline (Translator.sx_analysis)


def sx_simulate(head: Atom, body: list, tmp_names) -> tuple:
    """The clause as Souffle evaluates it, for a clause sx_resolve_aliases
    handles: ResolveAliases runs four times in the pipeline, and
    MinimiseProgram drops repeated literals."""
    head = map_lit_exprs(head, sx_negative_literals)
    body = [map_lit_exprs(lit, sx_negative_literals) for lit in body]
    for _ in range(4):
        new = sx_resolve_aliases(head, body, tmp_names)
        if new == (head, body):
            break
        head, body = new
    out: list = []
    for lit in body:
        if lit not in out:
            out.append(lit)
    return head, out


# ============================================================================
# Translator
# ============================================================================

FLOWLOG_KEYWORDS = {
    "as",
    "fn",
    "overridable",
    "static",
    "append",
    "mutable",
    "True",
    "False",
    "count",
    "sum",
    "min",
    "max",
    "mean",
}
_FL_IDENT = re.compile(r"^_?[A-Za-z]+[A-Za-z0-9_]*$")

# Souffle's primitive types for a 64-bit-domain build, which CTADL requires
# (-DSOUFFLE_DOMAIN_64BIT=ON). `float` is rejected: CTADL has none, and its
# formatting has not been checked against Souffle's.
PRIM_TYPES = {"symbol": "symbol", "number": "int64", "unsigned": "uint64"}
SOUFFLE_KIND = {v: k for k, v in PRIM_TYPES.items()}  # for schema.json
NUMBER = PRIM_TYPES["number"]
NUMERIC_PRIMS = {"int64", "uint64", "int32"}

# Choice-domain policies decoded through deterministic min-choice keys.
MINCHOICE_POLICIES = ("minchoice", "choice")
# The policy of a choice-domain relation the policy file does not list.
DEFAULT_CHOICE_POLICY = "minchoice"
# Arities of the choice_claimN UDFs in souffle2flowlog.rs.
MAX_CLAIM_KEYS = 6
# Choice-key priorities: non-recursive clauses 0.., recursive ones REC_PRIO..;
# must match PRIO_BITS in souffle2flowlog.rs (2 * REC_PRIO == 1 << PRIO_BITS).
REC_PRIO = 256
# One-row relation giving computed facts a body.
UNIT_REL = "FlowlogUnit"
# Relation names the translation introduces (besides `R_choice`, checked
# per choice relation).
RESERVED_REL = re.compile(r"^(Flowlog\w*|Range\d+(_req)?|NegPre\d+|ChoiceInstance)$")
# `FlowlogRange(hi, i)` holds 0 <= i < hi for every hi < RANGE_TABLE.
RANGE_REL = "FlowlogRange"
RANGE_TABLE = 1024
RANGE_DIGITS = 32  # RANGE_TABLE == RANGE_DIGITS ** 2
# Souffle builtins whose FlowLog counterparts differ (strlen/substr count
# chars, to_number parses decimal only, a negative exponent gives 0, `/` and
# `%` panic on a zero divisor, all but the operators are int32) map to the
# souffle2flowlog.rs functions with Souffle's semantics: name, parameters, result.
# `u/` and `u%` are the operators on `unsigned`.
SOUFFLE_UDFS = {
    "strlen": ("souffle_strlen", "s: string", "int64"),
    "substr": ("souffle_substr", "s: string, i: int64, n: int64", "string"),
    "to_number": ("souffle_to_number", "s: string", "int64"),
    "^": ("souffle_pow", "a: int64, b: int64", "int64"),
    "ord": ("souffle_ord", "s: string", "int64"),
    "/": ("souffle_div", "a: int64, b: int64", "int64"),
    "%": ("souffle_mod", "a: int64, b: int64", "int64"),
    "u/": ("souffle_udiv", "a: uint64, b: uint64", "uint64"),
    "u%": ("souffle_umod", "a: uint64, b: uint64", "uint64"),
}
# The partial ones: Souffle stops the program on some arguments, where the
# ports return 0; `<port>_error`, of the same parameters, returns "" or
# Souffle's complaint (see Translator.add_partial_checks).
PARTIAL_UDFS = ("to_number", "/", "%", "u/", "u%")
PARTIAL_BY_PORT = {SOUFFLE_UDFS[k][0]: k for k in PARTIAL_UDFS}
# Parameter types of the ports and of their `_error` companions, by name.
PORT_PARAMS = {
    n + sfx: [p.split(":")[1].strip() for p in ps.split(",")]
    for n, ps, _ in SOUFFLE_UDFS.values()
    for sfx in ("", "_error")
}
UDF_RESULT = {"string": "symbol", "int64": "number", "uint64": "unsigned"}
# The check rules' relation and pseudo-function (`m = flowlog_error_of(c)`,
# rewritten to the `_error` companion of the partial call c), and the
# relation whose rule stops the run.
ERROR_REL = "FlowlogError"
ERROR_OF = "flowlog_error_of"
ABORT_REL = "FlowlogAbort"
# Pragmas that change which clauses Souffle evaluates in ways the checks do
# not model.
SX_PIPELINE_PRAGMAS = ("magic-transform", "disable-transformers", "provenance")


def fl_name(name: str) -> str:
    """A FlowLog-legal spelling for a variable or attribute name."""
    if name in FLOWLOG_KEYWORDS:
        return name + "_k"
    if _FL_IDENT.match(name):
        return name
    s = re.sub(r"[^A-Za-z0-9_]", "_", name).lstrip("_")
    s = "v_" + s if not s or not s[0].isalpha() else s
    return s


def fl_string(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            # FlowLog reads Rust string literals, which lack \a \b \f \v
            out.append(f"\\u{{{ord(ch):x}}}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


class Translator:
    def __init__(self, items: list, policy: dict):
        self.items = items
        self.policy = policy
        self.choice_policy: dict = policy.get("choice", {})
        self.types: dict[str, TypeDecl] = {}
        self.functors: dict[str, Functor] = {}
        self.decls: dict[str, RelDecl] = {}  # by base name (first wins)
        self.global_decls: dict[str, RelDecl] = {}
        self.comp_decls: dict[str, dict[str, RelDecl]] = {}
        self.comp_inits: dict[str, dict[str, str]] = {}
        self.global_inits: dict[str, str] = {}
        self.counter = 0
        self.range_counter = 0
        self.warnings: list[str] = []
        self.dropped_rels: set[str] = set()
        self.read_rels: set[str] | None = (
            None  # set by run(); None: every relation counts as read
        )
        self.schema: dict[str, Any] = {}
        self.decl_comp: dict[int, str] = {}  # id(decl) -> declaring component
        self.instances: dict[str, list] = {}  # component -> instance paths
        self.rel_ids: dict[int, int] = {}  # id(decl) -> claim relation id
        self.ci_comps: set[str] = set()  # components needing ChoiceInstance
        self.claim_arities: set[int] = set()
        self.souffle_udfs: set[str] = set()  # keys of SOUFFLE_UDFS in use
        self.error_udfs: set[str] = (
            set()
        )  # keys of PARTIAL_UDFS whose `_error` is in use
        self.check_vars: list[str] = []  # see check_var
        self.need_choice_number = False
        self.tparams: set[str] = set()  # component type parameters
        self.comp_tparams: dict[str, list] = {}  # component -> its type parameters
        self.init_targs: dict[tuple, list] = (
            {}
        )  # (component or None, instance) -> type arguments
        self.need_unit = False
        self.need_range = False
        self.output_copies = 0  # see split_outputs
        # every variable name the program spells, and those generated so far:
        # generated names (fresh, flattened record fields) avoid them all
        self.taken: set[str] = set()
        for _, r in iter_rules(items):
            self.taken |= rule_vars(r)
        self.flat_names: dict[tuple, str] = {}

    def fresh(self, prefix: str) -> str:
        while True:
            self.counter += 1
            name = f"{prefix}{self.counter}"
            if name not in self.taken:
                self.taken.add(name)
                return name

    def flat_var(self, name: str, field: str) -> str:
        """The variable for field `field` of record variable `name`."""
        v = self.flat_names.get((name, field))
        if v is None:
            v = f"{name}_{field}"
            while v in self.taken:
                v += "_"
            self.taken.add(v)
            self.flat_names[(name, field)] = v
        return v

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    # ------------------------------------------------------------------ types
    def collect(self, items: list, comp: str | None = None) -> None:
        for it in items:
            if isinstance(it, RelDecl) and RESERVED_REL.match(it.name):
                raise TranslateError(
                    f"relation name {it.name} is reserved by the translation"
                )
            if isinstance(it, TypeDecl):
                self.types[it.name] = it
            elif isinstance(it, Functor):
                self.functors[it.name] = it
            elif isinstance(it, RelDecl):
                self.decls.setdefault(it.name, it)
                if comp is None:
                    self.global_decls[it.name] = it
                else:
                    self.comp_decls.setdefault(comp, {})[it.name] = it
                    self.decl_comp[id(it)] = comp
            elif isinstance(it, Init):
                if comp is None:
                    self.global_inits[it.name] = it.comp
                else:
                    self.comp_inits.setdefault(comp, {})[it.name] = it.comp
                self.init_targs[(comp, it.name)] = it.targs
            elif isinstance(it, Comp):
                self.comp_decls.setdefault(it.name, {})
                self.tparams.update(it.tparams)
                self.comp_tparams[it.name] = it.tparams
                self.collect(it.items, it.name)

    def base_type(self, t: str) -> Any:
        seen = set()
        while t in self.types and t not in seen:
            seen.add(t)
            td = self.types[t]
            if isinstance(td.rhs, tuple):
                if td.rhs[0] == "record":
                    return ("record", t, td.rhs[1])
                bases = {self.base_type(a) for a in td.rhs[1]}  # a union
                if len(bases) != 1:
                    raise TranslateError(f"union type {t} mixes base types")
                return bases.pop()
            t = td.rhs
        if t not in PRIM_TYPES and t != "float" and t not in self.tparams:
            raise TranslateError(f"unknown type {t}")
        return self.fl_type(t)

    @staticmethod
    def fl_type(t: str) -> str:
        """The FlowLog spelling of a type name (primitives mapped)."""
        if t == "float":
            raise TranslateError("type float is not supported")
        return PRIM_TYPES.get(t, t)

    def is_record_type(self, t: str) -> bool:
        b = self.base_type(t)
        return isinstance(b, tuple) and b[0] == "record"

    def record_fields(self, t: str) -> list:
        b = self.base_type(t)
        assert isinstance(b, tuple)
        return b[2]

    def flat_attrs(self, attrs: list, stack: tuple = ()) -> list:
        out = []
        for an, at in attrs:
            if self.is_record_type(at):
                name = self.base_type(at)[1]
                if name in stack:
                    raise TranslateError(
                        f"recursive record type {name} is not supported"
                    )
                for fn_, ft in self.flat_attrs(self.record_fields(at), stack + (name,)):
                    out.append((f"{an}_{fn_}", ft))
            else:
                out.append((an, at))
        return out

    def record_width(self, t: str) -> int:
        return len(self.flat_attrs([("x", t)]))

    def record_format(self, t: str) -> str:
        """How Souffle writes a record of type t, with `{}` for each flattened
        field: `[[{}, {}], {}]`."""
        return (
            "["
            + ", ".join(
                self.record_format(ft) if self.is_record_type(ft) else "{}"
                for _, ft in self.record_fields(t)
            )
            + "]"
        )

    def choice_keys(self, d: RelDecl) -> set:
        """The flattened attribute names of d's choice-domain key: a record
        attribute contributes all of its fields."""
        keys = d.choice[0]
        return {
            fa
            for an, at in d.attrs
            if an in keys
            for fa, _ in self.flat_attrs([(an, at)])
        }

    # ------------------------------------------------------------- top level
    def run(self) -> list:
        items = self.hoist_comps(self.items)
        self.collect(items)
        clash = sorted(self.tparams & set(self.types))
        if clash:
            # the types are resolved globally, so the parameter would read as the type
            raise TranslateError(
                f"component type parameter {clash[0]} has the name of a type"
            )
        items = self.split_outputs(items)
        self.instances = self.comp_instances()
        self.check_relations(items)
        self.read_rels = self.collect_read_rels(items)
        items = self.drop_unused_inline(items)
        items = self.split_choice_rules(items)
        items = self.add_partial_checks(items)
        items = self.inline_relations(items)
        self.note_plan_atoms(items)
        self.compute_choice_prio(items)
        items = self.transform_items(items, scope=())
        items = self.dedupe_io(items)
        items = self.add_choice_instances(items)
        if self.need_unit:
            items += [
                Raw(f".decl {UNIT_REL}(u: int32)"),
                Rule([Atom(UNIT_REL, [Num(0)])], None),
            ]
        if self.need_range:
            items += self.range_table()
        if self.error_udfs:
            # FlowlogError is a relation, so the messages are consolidated
            # (rows a negation retracts cancel out) before souffle_raise sees them
            items += [
                Raw(f".decl {ABORT_REL}(n: int64)"),
                Raw(f".output {ABORT_REL}"),
                Raw(f"{ABORT_REL}(n) :- {ERROR_REL}(m), n = souffle_raise(m)."),
            ]
        externs: list = []
        if any(self.is_minchoice(n) for n, d in self.decls.items() if d.choice):
            externs = [
                Raw(".extern fn choice_key1(prio: int32, a: string) -> int64"),
                Raw(
                    ".extern fn choice_key2(prio: int32, a: string, b: string) -> int64"
                ),
                Raw(
                    ".extern fn choice_key3(prio: int32, a: string, b: string, c: string) -> int64"
                ),
                Raw(
                    ".extern fn choice_key4(prio: int32, a: string, b: string, c: string, d: string) -> int64"
                ),
                Raw(".extern fn choice_field(ck: int64, i: int32) -> string"),
            ]
        if self.need_choice_number:
            externs.append(
                Raw(".extern fn choice_number(ck: int64, i: int32) -> int64")
            )
        for n in sorted(self.claim_arities):
            ks = ", ".join(f"k{i}: string" for i in range(1, n + 1))
            externs.append(
                Raw(
                    f".extern fn choice_claim{n}(inst: int32, rel: int32, ck: int64, {ks}) -> int64"
                )
            )
        for k in sorted(self.souffle_udfs):
            name, params, ret = SOUFFLE_UDFS[k]
            externs.append(Raw(f".extern fn {name}({params}) -> {ret}"))
        for k in sorted(self.error_udfs):
            name, params, _ = SOUFFLE_UDFS[k]
            externs.append(Raw(f".extern fn {name}_error({params}) -> string"))
        if self.error_udfs:
            externs.append(Raw(".extern fn souffle_raise(msg: string) -> int64"))
        return externs + items

    def add_choice_instances(self, items: list) -> list:
        """Number the instances of components whose choice claims need one."""
        if not self.ci_comps:
            return items
        out: list = []
        for it in items:
            if isinstance(it, Comp) and it.name in self.ci_comps:
                it = Comp(
                    it.name,
                    it.tparams,
                    it.supers,
                    [Raw(".decl ChoiceInstance(t: int32)")] + it.items,
                )
            out.append(it)
        n = 0
        for comp in sorted(self.ci_comps):
            for path in self.instances.get(comp, []):
                n += 1
                out.append(Rule([Atom(f"{path}.ChoiceInstance", [Num(n)])], None))
        return out

    # ---------------------------------------------------- partial functions
    def add_partial_checks(self, items: list) -> list:
        """Souffle stops the program when to_number reads a non-number or an
        integer `/` or `%` divides by zero or overflows; their ports return 0
        instead, because FlowLog evaluates expressions Souffle does not (a
        rule's functions before its constant tests, a negation's projection
        on the rows it removes), where stopping would be wrong.  For a
        partial call c in the head of a clause Souffle surely evaluates as
        written (sx_simulate, sx_analysis), the check rule

            FlowlogError(m) :- B, m = flowlog_error_of(c), m != "".

        where B is the clause's body without the literals containing c,
        derives Souffle's message for each binding of B on which c fails,
        and FlowlogAbort stops the run with it.  If Souffle completes, no
        call it evaluated failed, so its relations are the translation's and
        it evaluated c on every binding of B: no check fires.  So when one
        fires, Souffle stops too.  A call elsewhere (only in the body, in a
        recursive, choice-domain or component clause, in one Souffle may
        rewrite or drop, or with a B that reads a choice-domain or `ord`) is
        not checked: Souffle may stop on it where the translation completes,
        and a warning lists it.  So does a checked call that is in the body
        too, as Souffle may evaluate it there on a row B rejects (it tests a
        variable repeated in an atom after the rule's constraints), and a
        call of literals only (as `(0-1)/2`) that fails or not depending on
        its type: the check computes it on number, Souffle in the type of
        its context (type_context_partials)."""
        if not any(
            collect_exprs(lit, is_partial_call, [])
            for _, r in iter_rules(items)
            for lit in r.heads + [l for c in r.body or [] for l in c]
        ):
            return items

        def pragmas(its: list):
            for it in its:
                if isinstance(it, Pragma):
                    yield it
                elif isinstance(it, Comp):
                    yield from pragmas(it.items)

        bad = [
            p.args[0]
            for p in pragmas(items)
            if p.args and p.args[0] in SX_PIPELINE_PRAGMAS
        ]
        if bad:
            self.warn(
                f".pragma {bad[0]}: partial calls are not checked (Souffle may stop on them "
                "where the translation completes)"
            )
            return items
        clauses = self.sx_clauses(items)
        an = self.sx_analysis(items, clauses)
        checks: list = []
        checked: dict = {}  # (line, call) -> checked in every clause
        for cl in clauses:
            calls: list = []
            for lit in (
                [cl.sim[0]] + cl.sim[1] if cl.sim is not None else [cl.head] + cl.body
            ):
                collect_exprs(lit, is_partial_call, calls)
            in_head = (
                collect_exprs(cl.sim[0], is_partial_call, [])
                if cl.sim is not None
                else []
            )
            for c in calls:
                rule = self.partial_check(cl, c, an) if c in in_head else None
                key = (cl.line, sx_text(c))
                whole = rule is not None and not any(
                    contains_term(lit, c) for lit in cl.sim[1]
                )
                checked[key] = checked.get(key, True) and whole
                if rule is not None and not any(
                    r.heads == rule.heads and r.body == rule.body for r in checks
                ):
                    checks.append(rule)
        left = sorted(k for k, ok in checked.items() if not ok)
        if left:
            self.warn(
                f"{len(left)} of {len(checked)} partial calls are not fully checked (Souffle may stop on "
                "them where the translation completes): "
                + "; ".join(f"line {n}: {t}" for n, t in left)
            )
        if not checks:
            return items
        d = RelDecl(ERROR_REL, [("msg", "symbol")], [], [])
        self.decls[ERROR_REL] = self.global_decls[ERROR_REL] = d
        return items + [d] + checks

    def partial_check(self, cl: SxClause, c: Any, an: dict) -> Rule | None:
        """The check rule of partial call c in the head of clause cl (see
        add_partial_checks), or None when it is not sound."""
        head, body = cl.sim
        d = self.resolve_decl(head.name)
        if not (
            cl.top
            and cl.kept
            and d is not None
            and not d.choice
            and "inline" not in d.quals
            and head.name in an["live"]
            and head.name in an["irreducible"]
            and head.name not in an["recursive"]
        ):
            return None
        if any(isinstance(lit, Atom) and contains_term(lit, c) for lit in body):
            return None
        # the check computes a call of literals on number, the clause in the
        # type of its context
        if literal_expr(c) and literal_div_fails(c) is None:
            return None
        rest = [lit for lit in body if not contains_term(lit, c)]
        if any(a.name in an["tainted"] for a in body_atoms([rest])) or collect_exprs(
            c, is_ord, []
        ):
            return None
        if any(
            collect_exprs(lit, lambda e: is_ord(e) or is_partial_call(e), [])
            for lit in rest
        ):
            return None
        vs = expr_vars(c, set())
        for lit in rest:
            lit_vars(lit, vs)
        if not vs <= grounded_vars(rest):
            return None
        m = Var(self.check_var(0))
        lits = rest + [Cmp("=", m, Call(ERROR_OF, [c])), Cmp("!=", m, Str(""))]
        occ: dict = {}
        for lit in lits:
            for v in var_occurrences(lit):
                occ[v] = occ.get(v, 0) + 1
        in_atoms = {
            v for lit in lits if isinstance(lit, Atom) for v in var_occurrences(lit)
        }
        sub: dict = {}
        k = 0
        for v, n in occ.items():
            if n == 1 and v in in_atoms:
                sub[v] = Wild()
            elif v.startswith(" "):
                k += 1
                sub[v] = Var(self.check_var(k))
        lits = [subst_lit(lit, sub) for lit in lits]
        return Rule([Atom(ERROR_REL, [m])], [lits], cl.line)

    def check_var(self, i: int) -> str:
        """The i-th variable name the check rules introduce: `msg` (for the
        message), then `tmp`, `tmp_1`, ... (for Souffle's temporaries), each
        one no rule spells."""
        while len(self.check_vars) <= i:
            self.check_vars.append(
                self.fresh_var("msg" if not self.check_vars else "tmp")
            )
        return self.check_vars[i]

    def fresh_var(self, base: str) -> str:
        """A variable name no rule spells (`fresh` names stay as they were)."""
        name, i = base, 0
        while name in self.taken:
            i += 1
            name = f"{base}_{i}"
        self.taken.add(name)
        return name

    def sx_resolver(self, comp: str | None, path: str | None):
        """The relation names of component `comp`'s clauses in its instance
        at `path`, as Souffle's instantiation spells them (resolve_at; top
        level: unchanged)."""
        if comp is None or path is None:
            return lambda n: n
        return lambda n: self.resolve_at(n, path)[1]

    def sx_clauses(self, items: list) -> list:
        """The clauses Souffle evaluates: one per head, DNF conjunct
        (souffle_dnf) and component instance, relation names resolved to
        instance paths, with the clause's rewritten form (sx_simulate) when
        it has no aggregate, record, generator or inline relation."""
        n = 0

        def tmp_names():
            nonlocal n
            while True:
                n += 1
                yield f" _tmp_{n}"  # no program spells it, as in Souffle

        tmp = tmp_names()

        def odd(e):
            return (
                isinstance(e, (Agg, Rec, Nil))
                or isinstance(e, Call)
                and e.name in SX_GENERATORS
            )

        def modelled(cl: SxClause) -> bool:
            for lit in [cl.head] + cl.body:
                if isinstance(lit, Group) or collect_exprs(lit, odd, []):
                    return False
                a = lit.atom if isinstance(lit, Neg) else lit
                if isinstance(a, Atom):
                    d = self.resolve_decl(a.name)
                    if d is None or "inline" in d.quals:
                        return False
            return True

        out: list = []
        for scope, r in iter_rules(items):
            comp = scope[-1] if scope else None
            paths = [None] if comp is None else self.instances.get(comp, [])
            conjs = [[]] if r.body is None else souffle_dnf(r.body)
            for path in paths:
                res = self.sx_resolver(comp, path)
                for conj in conjs:
                    if BoolLit(False) in conj:
                        continue
                    conj = [lit for lit in conj if not isinstance(lit, BoolLit)]
                    names = {a.name: res(a.name) for a in body_atoms([conj])}
                    conj = [rename_atoms(lit, names) for lit in conj]
                    for h in r.heads:
                        cl = SxClause(
                            Atom(res(h.name), h.args),
                            conj,
                            r.line,
                            comp is None,
                            set(names.values()),
                        )
                        if modelled(cl):
                            cl.sim = sx_simulate(cl.head, cl.body, tmp)
                        out.append(cl)
        return out

    def sx_analysis(self, items: list, clauses: list) -> dict:
        """What Souffle 2.5's pipeline surely leaves of the program, from the
        modelled clauses (the others count as possibly dropped):

        - a clause is kept when it is not contradictory, has no constraint
          between constants left to fold, is not `R(..) :- .., R(..), ..`
          (MinimiseProgram; in R's cycle, any relation in place of the body's
          R may be a copy RemoveRelationCopies renames to R) and reads
          positively only relations that are inputs or have a kept clause
          (RemoveEmptyRelations);
        - `live`: relations RemoveRedundantRelations keeps, the outputs and
          what kept clauses of live relations read (negations only of
          relations with a kept clause);
        - `irreducible`: relations ReduceExistentials leaves as they are,
          the IO ones and those a live kept clause reads with an argument
          that is not `_` after ReplaceSingletonVariables (if the clause may
          be recursive, only when its head is irreducible); a relation that
          may be a copy (RemoveRelationCopies) passes its uses on, and its
          clause marks nothing;
        - `recursive`: relations in a cycle;
        - `tainted`: relations that may differ from Souffle's, a
          choice-domain or what reads one or `ord`."""
        io: dict = {}

        def walk(its: list, comp: str | None, paths: list) -> None:
            for it in its:
                if isinstance(it, IODecl):
                    for p in paths:
                        io.setdefault(self.sx_resolver(comp, p)(it.name), set()).add(
                            it.kind
                        )
                elif isinstance(it, Comp):
                    walk(it.items, it.name, self.instances.get(it.name, []))

        walk(items, None, [None])
        inputs = {n for n, k in io.items() if ".input" in k}
        outputs = {n for n, k in io.items() if k & {".output", ".printsize"}}
        is_io = inputs | outputs

        def positive(body: list) -> list:
            return [lit for lit in body if isinstance(lit, Atom)]

        edges: dict = {}
        for cl in clauses:
            edges.setdefault(cl.head.name, set()).update(cl.deps)
        scc = tarjan_scc(edges)
        rec = {scc[h] for h, ds in edges.items() for d in ds if scc[d] == scc[h]}

        def kept0(cl: SxClause) -> bool:
            if cl.sim is None:
                return False
            head, body = cl.sim
            pruned = prune_conj(body)
            # MinimiseProgram drops `R(..) :- .., R(..), ..`, also once
            # RemoveRelationCopies renames a copy of R in the body to R (the
            # copy is then in R's cycle)
            return (
                pruned is not None
                and not any(
                    isinstance(l, Cmp) and sx_const(l.l) and sx_const(l.r)
                    for l in pruned
                )
                and not any(
                    isinstance(l, Atom)
                    and l.args == head.args
                    and scc.get(l.name) == scc[head.name]
                    for l in body
                )
            )

        k0 = [kept0(cl) for cl in clauses]
        nonempty = set(inputs)
        grew = True
        while grew:
            grew = False
            for cl, ok in zip(clauses, k0):
                if (
                    ok
                    and cl.head.name not in nonempty
                    and all(a.name in nonempty for a in positive(cl.sim[1]))
                ):
                    nonempty.add(cl.head.name)
                    grew = True
        for cl, ok in zip(clauses, k0):
            cl.kept = ok and all(a.name in nonempty for a in positive(cl.sim[1]))

        def may_recurse(cl: SxClause) -> bool:
            # its head may be in its body once copies are replaced
            h = cl.head.name
            return (scc[h] in rec and any(scc[d] == scc[h] for d in cl.deps)) or (
                h not in is_io
                and any(d not in is_io and scc[d] in rec for d in cl.deps)
            )

        passes: dict = {}  # relation -> relations its irreducibility makes irreducible
        copies: set = (
            set()
        )  # clauses RemoveRelationCopies may drop, renaming their head
        for i, cl in enumerate(clauses):
            if cl.sim is None or cl.head.name in is_io:
                continue
            head, body = cl.sim
            d = self.resolve_decl(head.name)
            names = {a.name for a in head.args if isinstance(a, Var)}
            if (
                d is not None
                and not d.choice
                and len(body) == 1
                and isinstance(body[0], Atom)
                and body[0].name != head.name
                and body[0].args == head.args
                and len(names) == len(head.args)
            ):
                passes.setdefault(head.name, set()).add(body[0].name)
                copies.add(i)

        def named_atoms(cl: SxClause) -> list:
            # the atoms ReduceExistentials sees with an argument other than `_`
            head, body = cl.sim
            body = [
                l
                for l in body
                if not (isinstance(l, Neg) and l.atom.name not in nonempty)
            ]
            occ: dict = {}
            for lit in [head] + body:
                for v in var_occurrences(lit):
                    occ[v] = occ.get(v, 0) + 1
            out = []
            for lit in body:
                a = lit.atom if isinstance(lit, Neg) else lit
                if isinstance(a, Atom) and any(
                    not isinstance(x, (Var, Wild))
                    or isinstance(x, Var)
                    and occ[x.name] > 1
                    for x in a.args
                ):
                    out.append(a)
            return out

        def closure(seeds: set) -> set:
            seen, stack = set(seeds), list(seeds)
            while stack:
                for y in passes.get(stack.pop(), ()):
                    if y not in seen:
                        seen.add(y)
                        stack.append(y)
            return seen

        live = set(outputs)
        marks = set(is_io)
        done: set = set()
        while True:
            irreducible = closure(marks)
            progress = False
            for i, cl in enumerate(clauses):
                h = cl.head.name
                if i in done or not cl.kept or h not in live:
                    continue
                recursive = may_recurse(cl)
                if recursive and h not in irreducible:
                    continue
                done.add(i)
                progress = True
                for lit in cl.sim[1]:
                    if (
                        isinstance(lit, Atom)
                        or isinstance(lit, Neg)
                        and lit.atom.name in nonempty
                    ):
                        live.add(lit.name if isinstance(lit, Atom) else lit.atom.name)
                if i in copies:
                    continue  # its body relation may take the head's place: passes
                for a in named_atoms(cl):
                    if a.name != h:
                        if recursive:
                            passes.setdefault(h, set()).add(a.name)
                        else:
                            marks.add(a.name)
            if not progress:
                break
        tainted = {
            n
            for n in set(edges) | {d for ds in edges.values() for d in ds}
            if (d := self.resolve_decl(n)) is not None and d.choice
        }
        tainted |= {
            cl.head.name
            for cl in clauses
            if any(collect_exprs(lit, is_ord, []) for lit in [cl.head] + cl.body)
        }
        grew = True
        while grew:
            grew = False
            for cl in clauses:
                if cl.head.name not in tainted and cl.deps & tainted:
                    tainted.add(cl.head.name)
                    grew = True
        return {
            "live": live,
            "irreducible": irreducible,
            "tainted": tainted,
            "recursive": {n for n in edges if scc[n] in rec},
        }

    # ------------------------------------------------------- comp hoisting
    def hoist_comps(self, items: list) -> list:
        """Move nested components to the top level as `Outer_Inner`; an
        `.init` inside a component sees the components nested in it and in
        its enclosing components, nearest first.  Inheritance and
        `.override` are rejected: resolve_decl does not follow them."""
        out: list = []
        for it in items:
            if isinstance(it, Comp):
                hoisted, comp = self._hoist(it, it.name, {})
                out.extend(hoisted)
                out.append(comp)
            else:
                out.append(it)
        names = [it.name for it in out if isinstance(it, Comp)]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise TranslateError(
                f"component name {dup[0]} is ambiguous once nested components are hoisted"
            )
        return out

    def _hoist(self, comp: Comp, prefix: str, outer: dict):
        if comp.supers:
            raise TranslateError(
                f"component inheritance ({comp.name} : "
                f"{', '.join(s for s, _ in comp.supers)}) is not supported"
            )
        nested = {
            it.name: f"{prefix}_{it.name}" for it in comp.items if isinstance(it, Comp)
        }
        if nested and comp.tparams:
            raise TranslateError(
                f"component {comp.name} has type parameters and nested components "
                "(not supported)"
            )
        renames = {**outer, **nested}
        hoisted: list = []
        new_items: list = []
        for it in comp.items:
            if isinstance(it, Comp):
                sub_hoisted, sub = self._hoist(it, nested[it.name], renames)
                hoisted.extend(sub_hoisted)
                hoisted.append(
                    Comp(nested[it.name], sub.tparams, sub.supers, sub.items)
                )
            elif isinstance(it, Override):
                raise TranslateError(
                    f".override {it.name} in component {comp.name} is not supported"
                )
            elif isinstance(it, Init) and it.comp in renames:
                new_items.append(Init(it.name, renames[it.comp], it.targs))
            else:
                new_items.append(it)
        return hoisted, Comp(comp.name, comp.tparams, comp.supers, new_items)

    # --------------------------------------------------- inline relations
    def split_choice_rules(self, items: list) -> list:
        """Split every rule deriving a min-choice relation into the clauses
        Souffle makes of it (one per DNF conjunct), so that each gets its own
        priority, and record the flags of the rewrites Souffle applies before
        inlining (see souffle_order_key).  A clause's `sub` is (rule number,
        DNF index): unique, even for several rules or facts on one line."""
        seq = 0

        def fix(its: list, scope: tuple) -> list:
            nonlocal seq
            out: list = []
            for it in its:
                if isinstance(it, Comp):
                    it = Comp(
                        it.name,
                        it.tparams,
                        it.supers,
                        fix(it.items, scope + (it.name,)),
                    )
                elif isinstance(it, Rule) and any(
                    d is not None and d.choice and self.is_minchoice(d.name)
                    for d in (self.resolve_decl(h.name, scope) for h in it.heads)
                ):
                    seq += 1
                    if it.body is None:
                        out.append(
                            Rule(it.heads, None, it.line, (seq, 0), None, it.plan)
                        )
                        continue
                    for i, conj in enumerate(souffle_dnf(it.body)):
                        sx = {
                            "farc": sx_record_constraint(conj),
                            "ra1": sx_resolve_aliases_changes(conj),
                            "rbc1": any(isinstance(lit, BoolLit) for lit in conj),
                            "scbc": sx_const_constraint(conj),
                        }
                        out.append(
                            Rule(it.heads, [conj], it.line, (seq, i), sx, it.plan)
                        )
                    continue
                out.append(it)
            return out

        return fix(items, ())

    def collect_read_rels(self, items: list) -> set:
        """Base names of the relations whose contents can reach a result:
        those a rule body reads, and those an .output or .printsize names.
        Base names over-approximate across components, which is safe."""
        read = {
            a.name.split(".")[-1]
            for _, r in iter_rules(items)
            if r.body is not None
            for a in body_atoms(r.body)
        }

        def scan(its: list) -> None:
            for it in its:
                if isinstance(it, IODecl) and it.kind in (".output", ".printsize"):
                    read.add(it.name.split(".")[-1])
                elif isinstance(it, Comp):
                    scan(it.items)

        scan(items)
        return read

    def drop_unused_inline(self, items: list) -> list:
        used: set[str] = set()
        for _, r in iter_rules(items):
            if r.body is not None:
                for a in body_atoms(r.body):
                    used.add(a.name.split(".")[-1])
        # names are base names, so a name is dropped only if every relation
        # declared with it (in any component) is inline
        inline, plain = set(), set()

        def scan(its: list) -> None:
            for it in its:
                if isinstance(it, RelDecl):
                    (inline if "inline" in it.quals else plain).add(it.name)
                elif isinstance(it, Comp):
                    scan(it.items)

        scan(items)
        drop = inline - plain - used
        for n in sorted(drop):
            self.warn(f"inline relation {n} is never used; dropped")
        self.dropped_rels |= drop

        def filt(its: list) -> list:
            res = []
            for it in its:
                if isinstance(it, RelDecl) and it.name in drop:
                    continue
                if isinstance(it, Rule) and any(
                    h.name.split(".")[-1] in drop for h in it.heads
                ):
                    heads = [h for h in it.heads if h.name.split(".")[-1] not in drop]
                    if not heads:
                        continue
                    it = replace(it, heads=heads)
                if isinstance(it, Comp):
                    it = Comp(it.name, it.tparams, it.supers, filt(it.items))
                res.append(it)
            return res

        return filt(items)

    def inline_relations(self, items: list) -> list:
        """Souffle substitutes the clauses of an `inline` relation into every
        clause using it, so reading it costs no iteration: inside a recursive
        stratum the consumer fires in the same iteration as the literals the
        relation stands for.  A materialised relation would delay every
        consumer by one round, which changes the witness a choice-domain keeps
        first.  Positive uses are therefore expanded in place, one conjunction
        per clause disjunct (clause variables renamed apart, head arguments
        unified with the call's, conjunctions with contradictory constant
        constraints pruned).  A relation whose clauses are given per instance
        from outside its component, or that is used negated, stays
        materialised; one used inside an aggregate is an error, as in
        Souffle."""
        scopes: dict[int, tuple] = {}
        decls: dict[int, RelDecl] = {}

        def walk(its: list, scope: tuple) -> None:
            for it in its:
                if isinstance(it, RelDecl) and "inline" in it.quals:
                    scopes[id(it)] = scope
                    decls[id(it)] = it
                elif isinstance(it, Comp):
                    walk(it.items, scope + (it.name,))

        walk(items, ())
        if not decls:
            return items
        defs: dict[int, list] = {k: [] for k in decls}
        bad: dict[int, str] = {}
        for scope, r in iter_rules(items):
            for h in r.heads:
                d = self.resolve_decl(h.name, scope)
                if d is None or id(d) not in decls:
                    continue
                k = id(d)
                if len(r.heads) != 1:
                    bad.setdefault(k, "a clause has several heads")
                elif scope != scopes[k] or h.name != d.name:
                    bad.setdefault(k, "its clauses are given per component instance")
                elif any(all_aggs_in_lit(lit, []) for c in r.body or [] for lit in c):
                    bad.setdefault(k, "a clause has an aggregate")
                defs[k].append(r)
        for scope, r in iter_rules(items):
            for kind, a in self.atom_uses(r.body or []):
                d = self.resolve_decl(a.name, scope)
                if d is None or id(d) not in decls:
                    continue
                if kind == "inside an aggregate":
                    raise TranslateError(
                        f"inline relation {a.name} is used inside an aggregate "
                        "(Souffle rejects this too)"
                    )
                if kind != "positive":
                    bad.setdefault(id(d), f"it is used {kind}")
                elif (
                    self.inline_names(scopes[id(d)], defs[id(d)], a.name, scope) is None
                ):
                    bad.setdefault(
                        id(d), "its clauses resolve differently at a use site"
                    )
        for k in sorted(bad, key=lambda k: decls[k].name):
            self.warn(
                f"inline relation {decls[k].name} is materialised ({bad[k]}); inside "
                "recursion its consumers see it one iteration later than in Souffle"
            )
        ok = {k for k in decls if k not in bad}
        if not ok:
            return items
        ctx = (ok, scopes, defs)

        def fix(its: list, scope: tuple) -> list:
            out: list = []
            for it in its:
                if isinstance(it, RelDecl) and id(it) in ok:
                    continue
                if isinstance(it, Comp):
                    it = Comp(
                        it.name,
                        it.tparams,
                        it.supers,
                        fix(it.items, scope + (it.name,)),
                    )
                elif isinstance(it, Rule):
                    ds = [self.resolve_decl(h.name, scope) for h in it.heads]
                    if any(d is not None and id(d) in ok for d in ds):
                        continue  # a clause of an inlined relation
                    if it.body is not None and it.sub is not None:
                        exp = self.expand_inline_paths(it.body[0], scope, ctx, 0)
                        if exp is not None:
                            if not exp:
                                self.warn(
                                    f"line {it.line}: rule unsatisfiable after inlining; dropped"
                                )
                            for conj, path, rounds, ceq in exp:
                                sx = dict(it.sx, k=rounds, scbc=it.sx["scbc"] or ceq)
                                out.append(
                                    Rule(
                                        it.heads,
                                        [conj],
                                        it.line,
                                        it.sub + (path,),
                                        sx,
                                        it.plan,
                                    )
                                )
                            continue
                    elif it.body is not None:
                        body = self.expand_inline(it.body, scope, ctx, 0)
                        if body is not it.body:
                            if not body:
                                self.warn(
                                    f"line {it.line}: rule unsatisfiable after inlining; dropped"
                                )
                                continue
                            it = Rule(it.heads, body, it.line, plan=it.plan)
                out.append(it)
            return out

        return fix(items, ())

    def atom_uses(self, body: list):
        """(kind, atom) for every atom of a body; kind is `positive`,
        `negated` or `inside an aggregate`."""
        for conj in body:
            for lit in conj:
                if isinstance(lit, Atom):
                    yield "positive", lit
                elif isinstance(lit, Neg):
                    yield "negated", lit.atom
                elif isinstance(lit, Group):
                    yield from self.atom_uses(lit.disj)
                for agg in all_aggs_in_lit(lit, []):
                    for a in body_atoms(agg.body):
                        yield "inside an aggregate", a

    def inline_names(
        self, dscope: tuple, rules: list, use_name: str, uscope: tuple
    ) -> dict | None:
        """How the atoms of an inline relation's clauses (declared in scope
        `dscope`) are spelled at a use `use_name` in scope `uscope`: names
        local to the declaring component take the use's instance prefix.
        None when some name would resolve to a different relation there."""
        base = use_name.split(".")[-1]
        prefix = use_name[: len(use_name) - len(base)]
        comp = dscope[-1] if dscope else None
        local: set = set()
        if comp is not None:
            local = set(self.comp_decls.get(comp, {})) | set(
                self.comp_inits.get(comp, {})
            )
        names: dict = {}
        for r in rules:
            for a in body_atoms(r.body or []):
                n = a.name
                if n in names:
                    continue
                new = prefix + n if n.split(".")[0] in local else n
                if self.resolve_decl(new, uscope) is not self.resolve_decl(n, dscope):
                    return None
                names[n] = new
        return names

    def expand_inline(self, body: list, scope: tuple, ctx: tuple, depth: int) -> list:
        """`body` with every positive inline atom expanded (the same object
        when nothing changed)."""
        changed = False
        out: list = []
        for conj in body:
            res = self.expand_inline_conj(conj, scope, ctx, depth)
            if res is None:
                out.append(conj)
            else:
                changed = True
                out.extend(res)
        return out if changed else body

    def expand_inline_conj(
        self, conj: list, scope: tuple, ctx: tuple, depth: int
    ) -> list | None:
        ok, scopes, defs = ctx
        if depth > 64:
            raise TranslateError(
                "inline relations nest too deeply (a recursive inline relation?)"
            )
        for k, lit in enumerate(conj):
            if isinstance(lit, Atom):
                d = self.resolve_decl(lit.name, scope)
                if d is None or id(d) not in ok:
                    continue
                names = self.inline_names(scopes[id(d)], defs[id(d)], lit.name, scope)
                if names is None:
                    raise TranslateError(f"cannot inline {lit.name} here")
                res: list = []
                for alt in self.inline_atom(lit, defs[id(d)], names):
                    c = prune_conj(conj[:k] + alt + conj[k + 1 :])
                    if c is not None:
                        sub = self.expand_inline_conj(c, scope, ctx, depth + 1)
                        res.extend([c] if sub is None else sub)
                return res
            if isinstance(lit, Group):
                disj = self.expand_inline(lit.disj, scope, ctx, depth + 1)
                if disj is lit.disj:
                    continue
                if not disj:
                    return []
                c = conj[:k] + [Group(disj)] + conj[k + 1 :]
                sub = self.expand_inline_conj(c, scope, ctx, depth + 1)
                return [c] if sub is None else sub
        return None

    def expand_inline_paths(
        self, conj: list, scope: tuple, ctx: tuple, depth: int
    ) -> list | None:
        """Expand the inline atoms of one Souffle clause (a DNF conjunct of a
        min-choice rule) the way InlineRelationsTransformer does: every round
        replaces each clause by one version per clause of the relation of its
        first inline atom.  Returns (conjunction, path, rounds, const_eq) per
        resulting clause, where `path` lists the inline clause chosen at each
        round and `const_eq` tells whether unification left a constraint
        between constants; None when the clause has no inline atom."""
        ok, scopes, defs = ctx
        if depth > 64:
            raise TranslateError(
                "inline relations nest too deeply (a recursive inline relation?)"
            )
        for k, lit in enumerate(conj):
            if not isinstance(lit, Atom):
                continue
            d = self.resolve_decl(lit.name, scope)
            if d is None or id(d) not in ok:
                continue
            names = self.inline_names(scopes[id(d)], defs[id(d)], lit.name, scope)
            if names is None:
                raise TranslateError(f"cannot inline {lit.name} here")
            res: list = []
            for j, alt in enumerate(
                self.inline_atom(lit, defs[id(d)], names, dnf=True)
            ):
                raw = conj[:k] + alt + conj[k + 1 :]
                ceq = sx_const_constraint(raw)
                c = prune_conj(raw)
                if c is None:
                    continue
                sub = self.expand_inline_paths(c, scope, ctx, depth + 1)
                if sub is None:
                    res.append((c, (j,), 1, ceq))
                else:
                    res.extend((c2, (j,) + p, n + 1, ceq or e) for c2, p, n, e in sub)
            return res
        return None

    def inline_atom(self, a: Atom, rules: list, names: dict, dnf: bool = False) -> list:
        """The conjunctions a positive atom of an inline relation stands for.
        With `dnf`, one per clause Souffle makes of the inline relation, in
        its order at inlining time (clauses rewritten by
        RemoveBooleanConstraints moved last)."""
        clauses: list = []
        for r in rules:
            if r.body is None:
                clauses.append((False, r, []))
            elif dnf:
                clauses += [
                    (any(isinstance(x, BoolLit) for x in c), r, c)
                    for c in souffle_dnf(r.body)
                ]
            else:
                clauses += [(False, r, c) for c in r.body]
        if dnf:
            clauses.sort(key=lambda x: x[0])
        alts: list = []
        for _, r, c in clauses:
            h = r.heads[0]
            if len(h.args) != len(a.args):
                raise TranslateError(f"{a.name}: arity differs from its inline clauses")
            sub: dict = {}
            eqs: list = []
            for hp, arg in zip(h.args, a.args):
                if isinstance(hp, Var) and hp.name not in sub:
                    sub[hp.name] = (
                        Var(self.fresh("inl_")) if isinstance(arg, Wild) else arg
                    )
                elif not isinstance(hp, Wild) and not isinstance(arg, Wild):
                    eqs.append((hp, arg))
            local: set = set()
            for lit in c:
                lit_vars(lit, local)
            for v in sorted(local - set(sub)):
                sub[v] = Var(self.fresh(f"inl_{v.lstrip('_')}_"))

            def f(e: Any) -> Any:
                return sub.get(e.name, e) if isinstance(e, Var) else e

            pre = [Cmp("=", map_expr(hp, f), arg) for hp, arg in eqs]
            alts.append(pre + [rename_atoms(map_lit_exprs(lit, f), names) for lit in c])
        return alts

    # ------------------------------------------------- min-choice priority
    def compute_choice_prio(self, items: list) -> None:
        """Souffle evaluates a stratum's non-recursive clauses first, then
        iterates its recursive clauses, each group in the relation's clause
        order after the AST transformers (see souffle_order_key); a
        choice-domain keeps the first tuple derived for each key.  Record per
        (rule line, Souffle clause, head decl) a priority mirroring that
        order: the clause's rank among the head's non-recursive clauses, or
        REC_PRIO plus its rank among the recursive ones (both capped below
        REC_PRIO)."""
        self.choice_prio: dict = {}
        edges: dict[int, set] = {}
        rules = []
        for scope, r in iter_rules(items):
            hs = [
                (h, d)
                for h in r.heads
                for d in [self.resolve_decl(h.name, scope)]
                if d is not None
            ]
            if not any(d.choice for _, d in hs):
                pos: list = []
            else:
                pos = [
                    d
                    for d in (
                        self.resolve_decl(a.name, scope)
                        for a in self.positive_atoms(r.body)
                    )
                    if d is not None
                ]
            deps = [
                d
                for d in (
                    self.resolve_decl(a.name, scope) for a in body_atoms(r.body or [])
                )
                if d is not None
            ]
            for _, d in hs:
                edges.setdefault(id(d), set()).update(id(x) for x in deps)
            rules.append((scope, r, hs, pos))
        scc = tarjan_scc(edges)
        self.scc = scc
        self.rec_sccs = {
            scc[n] for n, ds in edges.items() for d in ds if scc.get(d) == scc[n]
        }
        empty = self.souffle_empty_passes(items)
        groups: dict = {}
        for idx, (scope, r, hs, pos) in enumerate(rules):
            for h, d in hs:
                if not d.choice:
                    continue
                rec = any(scc.get(id(x)) == scc.get(id(d)) for x in pos)
                key = self.souffle_order_key(r, h, scope, empty) + (idx,)
                groups.setdefault((id(d), rec), []).append((key, r))
        for (k, rec), lst in groups.items():
            lst.sort(key=lambda x: x[0])
            for n, (_, r) in enumerate(lst):
                self.choice_prio[(r.line, r.sub, k)] = (REC_PRIO if rec else 0) + min(
                    n, REC_PRIO - 1
                )
                if n >= REC_PRIO:
                    self.warn(
                        f"line {r.line}: more than {REC_PRIO} clauses derive a min-choice relation; "
                        "the later ones share a priority"
                    )

    def souffle_order_key(self, r: Rule, h: Atom, scope: tuple, empty: dict) -> tuple:
        """Sort key of a clause in Souffle's final clause order of its head
        relation.  Every transformer that rewrites a clause re-appends it, so
        the order sorts by whether the latest rewrite touched the clause, then
        the one before, ..., then the source position: RemoveEmptyRelations
        (dropping negations of relations found empty, passes 5..2),
        PartitionBodyLiterals, RemoveEmptyRelations pass 1, constant
        constraint folding, inlining (by the number of rounds it took),
        RemoveBooleanConstraints, ResolveAliases, FoldAnonymousRecords, and
        component clauses after the top-level ones."""
        if r.sub is None or r.body is None:
            return (False,) * 12 + (bool(scope),)
        sx = r.sx or {}
        conj = r.body[0]

        def decl_of(a: Atom) -> RelDecl | None:
            return self.resolve_decl(a.name, scope)

        deleted = any(
            isinstance(l, Atom) and (d := decl_of(l)) is not None and id(d) in empty
            for l in conj
        )
        neg_pass = {
            empty.get(id(d))
            for l in conj
            if isinstance(l, Neg) and (d := decl_of(l.atom)) is not None
        }
        kept = [
            l
            for l in conj
            if not (
                isinstance(l, Neg)
                and (d := decl_of(l.atom)) is not None
                and empty.get(id(d)) == 1
            )
        ]
        rounds = sx.get("k", 0)
        for lit in conj:
            atoms = (
                [lit]
                if isinstance(lit, Atom)
                else [lit.atom] if isinstance(lit, Neg) else []
            )
            for agg in all_aggs_in_lit(lit, []):
                atoms += list(body_atoms(agg.body))
            rounds += sum(
                1
                for a in atoms
                if (d := decl_of(a)) is not None and "inline" in d.quals
            )
        return (
            deleted,
            5 in neg_pass,
            4 in neg_pass,
            3 in neg_pass,
            2 in neg_pass,
            sx_partitioned(h, kept),
            1 in neg_pass,
            bool(sx.get("scbc")),
            rounds,
            bool(sx.get("rbc1")),
            bool(sx.get("ra1")),
            bool(sx.get("farc")),
            bool(scope),
        )

    def souffle_empty_passes(self, items: list) -> dict:
        """id(decl) -> the RemoveEmptyRelations pass (1-5) of Souffle's
        pipeline that finds the relation empty: no clauses and not an input;
        deleting the clauses using an empty relation positively can empty
        further relations for the next pass.  (Within a pass Souffle handles
        the empty relations in hash-set order, so clauses negating different
        empty relations may be re-appended in another order than modelled;
        CTADL has no such clauses deriving a min-choice relation.)"""
        inputs: set = set()
        decls: dict = {}

        def walk(its: list, scope: tuple) -> None:
            for it in its:
                if isinstance(it, IODecl) and it.kind == ".input":
                    d = self.resolve_decl(it.name, scope)
                    if d is not None:
                        inputs.add(id(d))
                elif isinstance(it, RelDecl):
                    decls[id(it)] = it
                elif isinstance(it, Comp):
                    walk(it.items, scope + (it.name,))

        walk(items, ())
        clauses: list = []
        for scope, r in iter_rules(items):
            hs = {
                id(d)
                for d in (self.resolve_decl(h.name, scope) for h in r.heads)
                if d is not None
            }
            if r.body is None:
                clauses.append((hs, set()))
                continue
            body = self.simplify_bools(r.body)
            for conj in souffle_dnf(body) if body is not None else []:
                clauses.append(
                    (
                        hs,
                        {
                            id(d)
                            for d in (
                                self.resolve_decl(l.name, scope)
                                for l in conj
                                if isinstance(l, Atom)
                            )
                            if d is not None
                        },
                    )
                )
        empty: dict = {}
        alive = clauses
        for p in range(1, 6):
            derived: set = set()
            for hs, _ in alive:
                derived |= hs
            for k in decls:
                if k not in inputs and k not in derived and k not in empty:
                    empty[k] = p
            alive = [c for c in alive if not (c[1] & empty.keys())]
        return empty

    def choice_pol(self, name: str):
        return self.choice_policy.get(name, DEFAULT_CHOICE_POLICY)

    def is_minchoice(self, name: str) -> bool:
        return self.choice_pol(name) in MINCHOICE_POLICIES

    def loop_choice(self, d: RelDecl | None) -> bool:
        """A min-choice relation derived inside a recursive stratum.

        FlowLog evaluates a static loop with presence semantics: the lattice
        merge of `R_choice` emits each key's first-round minimum and then every
        improvement, but never retracts a superseded row.  Reading R inside
        the loop through the lattice would therefore see several witnesses
        per key, and each could derive further tuples.  Instead, every
        in-loop read claims a witness: `choice_claimN` keeps, per relation
        instance and key, the first candidate any read presents and returns
        it ever after.  All reads of a key agree, so the loop computes over a
        single witness per key, as Souffle's choice-domain does; R itself is
        decoded after the loop through the same claims."""
        return (
            d is not None
            and bool(d.choice)
            and self.is_minchoice(d.name)
            and self.scc.get(id(d)) in self.rec_sccs
        )

    def head_sccs(self, heads: list, scope: tuple) -> set:
        return {
            self.scc.get(id(d))
            for d in (self.resolve_decl(h.name, scope) for h in heads)
            if d is not None
        }

    def rel_id(self, d: RelDecl) -> int:
        return self.rel_ids.setdefault(id(d), len(self.rel_ids) + 1)

    def comp_instances(self) -> dict:
        """Instance paths of every component, nested ones dotted."""
        out: dict = {}

        def walk(comp: str, path: str, depth: int) -> None:
            if depth > 32:
                raise TranslateError(f"component instantiation too deep at {path}")
            out.setdefault(comp, []).append(path)
            for sub, c2 in self.comp_inits.get(comp, {}).items():
                walk(c2, f"{path}.{sub}", depth + 1)

        for name, comp in self.global_inits.items():
            walk(comp, name, 0)
        return out

    def claim_tag(self, d: RelDecl, prefix: str) -> tuple:
        """The instance number a claim of `d` is made under, and the body
        atoms that bind it. A relation of a component instantiated more than
        once reads its instance number from a `ChoiceInstance` fact."""
        comp = self.decl_comp.get(id(d))
        if comp is None or len(self.instances.get(comp, [])) <= 1:
            return Num(0), []
        self.ci_comps.add(comp)
        v = Var(self.fresh("chinst"))
        return v, [Atom(f"{prefix}ChoiceInstance", [v])]

    def claim_call(self, d: RelDecl, ck: Any, keys: list) -> Any:
        """`keys` pairs each choice-key argument with its column type."""
        strs = [
            Call("to_string", [e]) if self.base_type(t) in NUMERIC_PRIMS else e
            for e, t in keys
        ]
        n = len(strs)
        if not 1 <= n <= MAX_CLAIM_KEYS:
            raise TranslateError(
                f"{d.name}: choice-domain of {n} columns (claims support 1..{MAX_CLAIM_KEYS})"
            )
        self.claim_arities.add(n)
        return ("claim", n, ck, strs)

    def decode_witness(
        self, d: RelDecl, inst: Any, claim: tuple, i: int, t: str
    ) -> Any:
        _, n, ck, strs = claim
        e = Call(
            "choice_field",
            [Call(f"choice_claim{n}", [inst, Num(self.rel_id(d)), ck] + strs), Num(i)],
        )
        return self.decode_field(e, t)

    def decode_field(self, e: Any, t: str) -> Any:
        """A choice key's field `e` (a `choice_field` call) as a value of
        type `t`."""
        b = self.base_type(t)
        if b == NUMBER:
            self.need_choice_number = True
            return Call("choice_number", e.args)
        if b in NUMERIC_PRIMS:
            raise TranslateError(
                f"choice-domain over a column of type {t} is not supported"
            )
        return e

    def rewrite_loop_reads(self, r: Rule, scope: tuple) -> Rule:
        """Read the min-choice relations of the heads' own recursive stratum
        through `R_choice` and claimed witnesses (see `loop_choice`)."""
        if r.body is None:
            return r
        hs = self.head_sccs(r.heads, scope)
        occ: dict = {}  # variable -> number of literals (and heads) it occurs in

        def note(lit: Any) -> None:
            for v in lit_vars(lit, set()):
                occ[v] = occ.get(v, 0) + 1

        def walk(conj: list) -> None:
            for lit in conj:
                if isinstance(lit, Group):
                    for c in lit.disj:
                        walk(c)
                else:
                    note(lit)

        for h in r.heads:
            note(h)
        for c in r.body:
            walk(c)

        def fix_conj(conj: list) -> list:
            out: list = []
            for lit in conj:
                if isinstance(lit, Group):
                    out.append(Group([fix_conj(c) for c in lit.disj]))
                    continue
                d = (
                    self.resolve_decl(lit.name, scope)
                    if isinstance(lit, Atom)
                    else None
                )
                if (
                    isinstance(lit, Atom)
                    and d is not None
                    and self.loop_choice(d)
                    and self.scc.get(id(d)) in hs
                ):
                    out.extend(self.loop_read(lit, d, occ))
                else:
                    out.append(lit)
            return out

        return Rule(r.heads, [fix_conj(c) for c in r.body], r.line, r.sub, r.sx, r.plan)

    def loop_read(self, a: Atom, d: RelDecl, occ: dict) -> list:
        """`occ` counts the literals of the rule each variable occurs in; a
        witness variable occurring once, in this atom only, is not decoded."""
        attrs = self.flat_attrs(d.attrs)
        keys = self.choice_keys(d)
        prefix = a.name[: len(a.name) - len(a.name.split(".")[-1])]
        kargs = [(e, t) for (an, t), e in zip(attrs, a.args) if an in keys]
        wits = [(e, t) for (an, t), e in zip(attrs, a.args) if an not in keys]
        mult = [x.name for x in a.args if isinstance(x, Var)]
        used = [
            (e, t, i)
            for i, (e, t) in enumerate(wits)
            if not isinstance(e, Wild)
            and not (
                isinstance(e, Var)
                and occ.get(e.name, 0) <= 1
                and mult.count(e.name) == 1
            )
        ]
        pre: list = []
        new_k: list = []
        for e, t in kargs:
            if isinstance(e, (Var, Str, Num)) or (isinstance(e, Wild) and not used):
                new_k.append(e)
                continue
            v = Var(self.fresh("chkey"))
            if not isinstance(e, Wild):
                pre.append(Cmp("=", v, e))
            new_k.append(v)
        ck = Var(self.fresh("chck"))
        out = pre + [Atom(f"{prefix}{d.name}_choice", new_k + [ck])]
        if not used:
            return out
        inst, extra = self.claim_tag(d, prefix)
        out.extend(extra)
        claim = self.claim_call(d, ck, [(e, t) for e, (_, t) in zip(new_k, kargs)])
        for e, t, i in used:
            dec = self.decode_witness(d, inst, claim, i, t)
            out.append(Cmp("=", e, dec) if isinstance(e, Var) else Cmp("=", dec, e))
        return out

    def positive_atoms(self, body: list | None):
        for conj in body or []:
            for lit in conj:
                if isinstance(lit, Atom):
                    yield lit
                elif isinstance(lit, Group):
                    yield from self.positive_atoms(lit.disj)

    # ------------------------------------------------------ item transform
    def transform_items(self, items: list, scope: tuple) -> list:
        out: list = []
        extra_rules: list = []
        for it in items:
            if isinstance(it, TypeDecl):
                if isinstance(it.rhs, tuple):
                    if it.rhs[0] == "union":
                        out.append(Raw(f".type {it.name} = {self.base_type(it.name)}"))
                    continue  # a record is flattened away
                out.append(Raw(f".type {it.name} = {self.fl_type(it.rhs)}"))
            elif isinstance(it, Functor):
                params = ", ".join(
                    f"{fl_name(pn)}: {self.fl_prim(pt)}" for pn, pt in it.params
                )
                out.append(
                    Raw(f".extern fn {it.name}({params}) -> {self.fl_prim(it.ret)}")
                )
            elif isinstance(it, RelDecl):
                out.extend(self.transform_decl(it, scope))
            elif isinstance(it, IODecl):
                self.record_io(it, scope)
                r = self.transform_io(it)
                if r is not None:
                    out.append(r)
            elif isinstance(it, Comp):
                supers = [(sn, [self.fl_type(a) for a in sa]) for sn, sa in it.supers]
                out.append(
                    Comp(
                        it.name,
                        it.tparams,
                        supers,
                        self.transform_items(it.items, scope + (it.name,)),
                    )
                )
            elif isinstance(it, Init):
                rec = [a for a in it.targs if self.is_record_type(a)]
                if rec:
                    # FlowLog instantiates the component, so its attributes
                    # of that type could not be flattened
                    raise TranslateError(
                        f".init {it.name}: record type argument {rec[0]} is not supported"
                    )
                out.append(Init(it.name, it.comp, [self.fl_type(a) for a in it.targs]))
            elif isinstance(it, Override):
                out.append(it)
            elif isinstance(it, Pragma):
                self.warn(f"dropped .pragma {' '.join(it.args)}")
                continue
            elif isinstance(it, Rule):
                rules, helpers = self.transform_rule(it, scope)
                out.extend(rules)
                extra_rules.extend(helpers)
            else:
                raise TranslateError(f"unexpected item {it}")
        out.extend(extra_rules)
        return out

    def fl_prim(self, t: str) -> str:
        b = self.base_type(t)
        if b == "symbol":
            return "string"
        if isinstance(b, tuple):
            raise TranslateError(f"record type {t} in functor signature")
        return b

    def split_outputs(self, items: list) -> list:
        """Souffle writes every `.output` of a relation, FlowLog one per
        relation. Of the outputs of a top-level relation, the one that is not
        to a file (sqlite; at most one), else the first, stays on it,
        and each other one writes a copy of the relation, `FlowlogOutputN`,
        to the file Souffle writes. Two outputs to one file are refused."""

        def file(io: IODecl) -> str | None:
            ps = dict(io.params)
            return (
                ps.get("filename", io.name + ".csv")
                if ps.get("IO", "file") == "file"
                else None
            )

        outs: dict[str, list] = {}  # relation -> its distinct outputs
        for it in items:
            # check_relations reports the outputs of undeclared relations
            if (
                isinstance(it, IODecl)
                and it.kind == ".output"
                and it.name in self.global_decls
            ):
                ios = outs.setdefault(it.name, [])
                if all(sorted(o.params) != sorted(it.params) for o in ios):
                    ios.append(it)
        keep = {}  # relation -> the output that stays on it
        for name, ios in outs.items():
            files = [file(o) for o in ios]
            if files.count(None) > 1:
                raise TranslateError(
                    f".output {name}: two outputs of the relation are not to a file"
                )
            dup = [f for f in files if f is not None and files.count(f) > 1]
            if dup:
                raise TranslateError(
                    f".output {name}: two outputs of the relation write '{dup[0]}'"
                )
            keep[name] = ios[files.index(None) if None in files else 0]
        out: list = []
        for it in items:
            if (
                not (
                    isinstance(it, IODecl) and it.kind == ".output" and it.name in outs
                )
                or it is keep[it.name]
            ):
                out.append(it)
            elif any(it is o for o in outs[it.name]):
                attrs = self.global_decls[it.name].attrs
                self.output_copies += 1
                copy = f"FlowlogOutput{self.output_copies}"
                self.decls[copy] = self.global_decls[copy] = RelDecl(
                    copy, list(attrs), [], []
                )
                vs = [Var(self.fresh("out")) for _ in attrs]
                params = [(k, v) for k, v in it.params if k != "filename"] + [
                    ("filename", file(it))
                ]
                out += [
                    self.decls[copy],
                    Rule([Atom(copy, vs)], [[Atom(it.name, list(vs))]]),
                    IODecl(".output", copy, params),
                ]
            # else it repeats an output of the relation: dropped
        return out

    def dedupe_io(self, items: list) -> list:
        seen = set()
        out = []
        for it in items:
            if isinstance(it, Raw) and it.text.startswith(
                (".input", ".output", ".printsize")
            ):
                if it.text in seen:
                    continue
                seen.add(it.text)
            if isinstance(it, Comp):
                it = Comp(it.name, it.tparams, it.supers, self.dedupe_io(it.items))
            out.append(it)
        return out

    def record_io(self, io: IODecl, scope: tuple) -> None:
        """Describe the sqlite and file inputs/outputs in the schema manifest
        (sqlite ones under "inputs"/"outputs", file ones under "files"), so
        tools can find FlowLog's files and map its tables back to Souffle's
        layout (original attribute names, flattened record columns and how
        Souffle writes them, renamed attributes). A directive inside a
        component names, in each instance, the relation its name resolves to
        there (resolve_at: `inst.R` for the instance's own), which is the
        table name, and the default file name, of both Souffle and FlowLog."""
        params = dict(io.params)
        io_kind = params.get("IO", "file")
        if io.kind not in (".input", ".output") or io_kind not in ("file", "sqlite"):
            return
        if io.name.split(".")[-1] in self.dropped_rels:
            return
        if scope and not self.instances.get(scope[-1]):
            return
        d = self.resolve_decl(io.name, scope)
        if d is None:
            raise TranslateError(f"{io.kind} of undeclared relation {io.name}")
        names = [io.name]
        if scope:
            names = list(
                dict.fromkeys(
                    self.resolve_at(io.name, p)[1] for p in self.instances[scope[-1]]
                )
            )
        cols = []
        kind = lambda t: SOUFFLE_KIND.get(self.base_type(t), self.base_type(t))
        for an, at in d.attrs:
            if self.is_record_type(at):
                flat = self.flat_attrs([(an, at)])
                cols.append(
                    {
                        "attr": an,
                        "kind": "record",
                        "fl": [fl_name(a) for a, _ in flat],
                        "fl_kinds": [kind(t) for _, t in flat],
                        "format": self.record_format(at),
                    }
                )
            else:
                cols.append({"attr": an, "kind": kind(at), "fl": [fl_name(an)]})
        if (
            io_kind == "file"
            and params.get("delimiter", "\t") != "\t"
            and any(c["kind"] == "record" for c in cols)
        ):
            raise TranslateError(
                f"{io.kind} {io.name}: record columns in a file need the tab delimiter"
            )
        key = "outputs" if io.kind == ".output" else "inputs"
        lst = (
            self.schema if io_kind == "sqlite" else self.schema.setdefault("files", {})
        ).setdefault(key, [])
        suffix = (
            ".sqlite"
            if io_kind == "sqlite"
            else ".csv" if key == "outputs" else ".facts"
        )
        # An input nothing reads cannot affect the results, so the engine
        # creates it empty (Souffle does not load it). CTADL's query declares
        # such inputs with record columns, which a DB written by Souffle
        # cannot supply.
        unread = (
            key == "inputs"
            and self.read_rels is not None
            and io.name.split(".")[-1] not in self.read_rels
        )
        for name in names:
            if all(e["name"] != name for e in lst):
                entry = {
                    "name": name,
                    "file": params.get("filename", name + suffix),
                    "columns": cols,
                }
                if unread:
                    entry["read"] = False
                lst.append(entry)

    def transform_io(self, io: IODecl) -> Raw | None:
        if io.kind == ".limitsize":
            raise TranslateError(
                f".limitsize {io.name} is not supported (it truncates the relation)"
            )
        if io.name.split(".")[-1] in self.dropped_rels:
            return None
        if io.kind == ".printsize":
            return Raw(f".printsize {io.name}")
        params = []
        for k, v in io.params:
            if k in ("IO", "filename", "delimiter"):
                params.append(f"{k}={fl_string(v)}")
            else:
                self.warn(f"{io.kind} {io.name}: dropped IO parameter {k}={v}")
        p = f"({', '.join(params)})" if params else ""
        return Raw(f"{io.kind} {io.name}{p}")

    def transform_decl(self, d: RelDecl, scope: tuple) -> list:
        attrs = self.flat_attrs(d.attrs)
        quals = [q for q in d.quals if q == "overridable"]
        qtxt = (" " + " ".join(quals)) if quals else ""
        if not d.choice:
            return [Raw(f".decl {d.name}({self.fmt_attrs(attrs)}){qtxt}")]
        pol = self.choice_pol(d.name)
        if len(d.choice) != 1:
            raise TranslateError(
                f"relation {d.name}: multiple choice-domains not supported"
            )
        if pol == "drop":
            self.warn(f"{d.name}: choice-domain dropped (policy=drop)")
            return [Raw(f".decl {d.name}({self.fmt_attrs(attrs)}){qtxt}")]
        if isinstance(pol, dict) and "min" in pol:
            col = pol["min"]
            keys = self.choice_keys(d)
            deps = [a for a, _ in attrs if a not in keys]
            if deps != [col] or attrs[-1][0] != col:
                raise TranslateError(
                    f"{d.name}: min policy needs the single dependent column last"
                )
            base = self.base_type(attrs[-1][1])
            if base not in NUMERIC_PRIMS:
                raise TranslateError(
                    f"{d.name}: min policy needs a numeric dependent column"
                )
            new_attrs = [(a, t) if a != col else (a, f"min<{base}>") for a, t in attrs]
            self.warn(
                f"{d.name}: choice-domain replaced by min<{attrs[-1][1]}> lattice on {col}"
            )
            return [Raw(f".decl {d.name}({self.fmt_attrs(new_attrs)}){qtxt}")]
        if pol in MINCHOICE_POLICIES:
            keys = self.choice_keys(d)
            key_attrs = [(a, t) for a, t in attrs if a in keys]
            dep_attrs = [(a, t) for a, t in attrs if a not in keys]
            if not 1 <= len(dep_attrs) <= 4:
                raise TranslateError(
                    f"{d.name}: minchoice supports 1..4 dependent columns"
                )
            self.warn(
                f"{d.name}: choice-domain encoded as min-choice over {[a for a, _ in dep_attrs]}"
            )
            cname = f"{d.name}_choice"
            if self.resolve_decl(cname, scope) is not None:
                raise TranslateError(
                    f"relation name {cname} is reserved for the choice-domain of {d.name}"
                )
            # the decode rule's variables are the attribute names, and `ck`
            attr_names = {fl_name(a) for a, _ in attrs}
            self.taken |= attr_names
            ck_name = "ck"
            while ck_name in attr_names:
                ck_name += "_"
            ck_var = Var(ck_name)
            out: list = [
                Raw(f".decl {d.name}({self.fmt_attrs(attrs)}){qtxt}"),
                Raw(
                    f".decl {cname}({self.fmt_attrs(key_attrs + [(ck_name, 'min<int64>')])})"
                ),
            ]
            # decode rule; a relation read inside its own loop decodes the
            # witnesses its reads claimed there (see loop_choice)
            body = [Atom(cname, [Var(fl_name(a)) for a, _ in key_attrs] + [ck_var])]
            inst: Any = None
            claim: tuple = ()
            if self.loop_choice(d):
                inst, extra = self.claim_tag(d, "")
                body += extra
                claim = self.claim_call(
                    d, ck_var, [(Var(fl_name(a)), t) for a, t in key_attrs]
                )
            head_args = []
            dep_idx = 0
            for a, t in attrs:
                if a in keys:
                    head_args.append(Var(fl_name(a)))
                    continue
                if self.loop_choice(d):
                    getter = self.decode_witness(d, inst, claim, dep_idx, t)
                else:
                    getter = self.decode_field(
                        Call("choice_field", [ck_var, Num(dep_idx)]), t
                    )
                dep_idx += 1
                head_args.append(getter)
            out.append(Rule([Atom(d.name, head_args)], [body]))
            if self.loop_choice(d):
                # claim each key in the round after it first appears, so its
                # witness is that round's minimum even if no rule of the loop
                # reads it; the claimed key is already a candidate, so this
                # never changes the lattice
                _, n, ck, strs = claim
                forced = Call(
                    f"choice_claim{n}", [inst, Num(self.rel_id(d)), ck] + strs
                )
                out.append(
                    Rule(
                        [
                            Atom(
                                cname,
                                [Var(fl_name(a)) for a, _ in key_attrs] + [forced],
                            )
                        ],
                        [body],
                    )
                )
            return out
        raise TranslateError(f"{d.name}: unknown choice policy {pol!r}")

    def fmt_attrs(self, attrs: list) -> str:
        names = [fl_name(a) for a, _ in attrs]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise TranslateError(
                f"two attributes are both spelled `{dup[0]}` once records are flattened "
                "and names are spelled for FlowLog"
            )
        res = []
        for a, t in attrs:
            if t.startswith("min<") or t.startswith("max<"):
                res.append(f"{fl_name(a)}: {t}")
            else:
                res.append(f"{fl_name(a)}: {self.fl_type(t)}")
        return ", ".join(res)

    # ---------------------------------------------------------------- rules
    def resolve_in(self, name: str, owner: str | None) -> RelDecl | None:
        """The relation `name` denotes inside component `owner` (None: the
        program): one it declares, or one reached through its `.init`s."""
        parts = name.split(".")
        if len(parts) == 1:
            return (
                self.comp_decls.get(owner, {})
                if owner is not None
                else self.global_decls
            ).get(name)
        cur = (
            self.comp_inits.get(owner, {}) if owner is not None else self.global_inits
        ).get(parts[0])
        for inst in parts[1:-1]:
            if cur is None:
                return None
            cur = self.comp_inits.get(cur, {}).get(inst)
        return None if cur is None else self.comp_decls.get(cur, {}).get(parts[-1])

    def resolve_at(self, name: str, path: str) -> tuple:
        """(declaration, full name) of the relation `name` denotes in the
        clauses of the component instance at `path`.  Souffle instantiates
        components inside out, so the innermost instance on the path whose
        component declares the name (or reaches it through an `.init`) owns
        it; failing all, the program does."""
        parts = path.split(".")
        chain = [(self.global_inits.get(parts[0]), parts[0])]
        for i in range(1, len(parts)):
            comp = chain[-1][0]
            inits = self.comp_inits.get(comp, {}) if comp is not None else {}
            chain.append((inits.get(parts[i]), ".".join(parts[: i + 1])))
        for owner, prefix in reversed(chain):
            d = self.resolve_in(name, owner)
            if d is not None:
                return d, f"{prefix}.{name}"
        return self.resolve_in(name, None), name

    def resolve_decl(self, name: str, scope: tuple = ()) -> RelDecl | None:
        """Resolve a relation reference as resolve_at does.  FlowLog
        instantiates components itself, so a component's clauses are
        translated once: a name must denote the same relation in each of its
        instances.  An uninstantiated component's names (which Souffle never
        resolves) are read as its own, else the program's."""
        comp = scope[-1] if scope else None
        if comp is None:
            return self.resolve_in(name, None)
        local = self.resolve_in(name, comp)
        paths = self.instances.get(comp, [])
        if local is not None or not paths:
            return local if local is not None else self.resolve_in(name, None)
        ds = [self.resolve_at(name, p)[0] for p in paths]
        d = ds[0]
        if any(x is None for x in ds):
            return None
        if any(x is not d for x in ds):
            raise TranslateError(
                f"{name} in component {comp} names different relations in its instances "
                "(not supported)"
            )
        if d is not self.resolve_in(name, None) and (
            d.choice or any(at in self.tparams for _, at in d.attrs)
        ):
            # the claims and the type arguments follow the owning instance
            raise TranslateError(
                f"{name} in component {comp} names a relation of an enclosing instance "
                "with a choice-domain or a type parameter (not supported)"
            )
        return d

    def check_relations(self, items: list) -> None:
        """Souffle rejects an atom or directive of an undefined relation and
        an atom of the wrong arity.  So does the translation, before passes
        that drop clauses could hide one from FlowLog.  Uninstantiated
        components are skipped, as Souffle never resolves their names."""

        def check(a: Atom, scope: tuple, line: int) -> None:
            d = self.resolve_decl(a.name, scope)
            if d is None:
                raise TranslateError(f"line {line}: undefined relation {a.name}")
            if len(a.args) != len(d.attrs):
                raise TranslateError(
                    f"line {line}: mismatching arity of relation {a.name} "
                    f"(expected {len(d.attrs)}, got {len(a.args)})"
                )

        def walk(its: list, scope: tuple) -> None:
            for it in its:
                if isinstance(it, Comp):
                    if self.instances.get(it.name):
                        walk(it.items, scope + (it.name,))
                elif isinstance(it, Rule):
                    for h in it.heads:
                        for a in lit_atoms(h):
                            check(a, scope, it.line)
                    for a in body_atoms(it.body or []):
                        check(a, scope, it.line)
                elif (
                    isinstance(it, IODecl) and self.resolve_decl(it.name, scope) is None
                ):
                    raise TranslateError(f"{it.kind} of undefined relation {it.name}")

        walk(items, ())

    def decl_attr_types(self, d: RelDecl, name: str, scope: tuple) -> list:
        """The attributes of `d`, which the reference `name` resolves to, with
        the type arguments of the instances the name walks through (as in
        resolve_decl) in place of the component type parameters. FlowLog
        instantiates components itself, so their declarations keep the
        parameters; inside a component its own parameters stay unknown."""
        parts = name.split(".")
        comp = scope[-1] if scope else None
        owner = (
            comp
            if comp is not None and parts[0] in self.comp_inits.get(comp, {})
            else None
        )
        sub: dict = {}
        for inst in parts[:-1]:
            target = (
                self.comp_inits.get(owner, {})
                if owner is not None
                else self.global_inits
            ).get(inst)
            if target is None:
                return list(d.attrs)
            targs = [sub.get(a, a) for a in self.init_targs.get((owner, inst), [])]
            sub = dict(zip(self.comp_tparams.get(target, []), targs))
            owner = target
        return [(an, sub.get(at, at)) for an, at in d.attrs]

    def transform_rule(self, r: Rule, scope: tuple):
        heads = r.heads
        body = r.body
        if body is not None:
            body = self.simplify_bools(body)
            if body is None:
                self.warn(f"line {r.line}: rule with an unsatisfiable body dropped")
                return [], []
            split = self.alias_constants(heads, body, scope, r.line)
            if split is not None:
                if r.sub is not None and len(split) > 1:
                    raise TranslateError(
                        f"line {r.line}: a choice clause with a disjunction is not supported here"
                    )
                out: list = []
                for hs, conj in split:
                    out.extend(
                        self.transform_rule(
                            Rule(hs, [conj], r.line, r.sub, r.sx, r.plan), scope
                        )[0]
                    )
                return out, []
        types = self.decl_var_types(heads, body or [], scope, r.line)
        self.check_casts(heads + [lit for c in body or [] for lit in c], types, r.line)
        heads, body = self.type_context_partials(heads, body, types, scope, r.line)
        f = self.expr_rewriter(types, r.line)
        heads = [Atom(h.name, [map_expr(a, f) for a in h.args]) for h in heads]
        if body is not None:
            body = self.map_body(body, lambda lit: map_lit_exprs(lit, f))
            body = self.rewrite_agg_bodies(body, f)
        folded = self.fold_constants(heads, body, scope, r.line)
        if folded is None:
            self.warn(f"line {r.line}: rule with an unsatisfiable body dropped")
            return [], []
        heads, body = folded
        if body is not None:
            body = self.lower_record_eqs(body, types, r.line)
        flat = lambda a: self.flatten_atom(a, scope)
        heads = [flat(h) for h in heads]
        helpers: list = []
        if body is not None:
            body = self.map_atoms(body, flat)
        recs = {v for v, t in types.items() if self.is_record_type(t)}
        left: set = set()
        for h in heads:
            lit_vars(h, left)
        for c in body or []:
            for lit in c:
                lit_vars(lit, left)
        if recs & left:
            raise TranslateError(
                f"line {r.line}: record variable `{sorted(recs & left)[0]}` is used "
                "other than as an atom argument or in a record equality"
            )
        if body is not None:
            body = self.drop_self_eq(body)
            body, helpers = self.rewrite_ranges(body, scope, types)
            if not body:
                # every disjunct enumerates an empty constant range
                return [], []

        def no_range(e):
            if isinstance(e, Call) and e.name == "range":
                raise TranslateError(
                    f"line {r.line}: range is supported only as `v = range(lo, hi)` in a rule body"
                )
            return e

        rules = [Rule(heads, body, r.line, r.sub, r.sx, r.plan)] + [
            h for h in helpers if isinstance(h, Rule)
        ]
        for x in rules:
            for h in x.heads:
                deep_map_lit(h, no_range)
            for c in x.body or []:
                for lit in c:
                    deep_map_lit(lit, no_range)
        decls = [h for h in helpers if not isinstance(h, Rule)]
        out: list = []
        for x in rules:
            out.extend(self.finish_rule(x, scope, types))
        return out + decls, []

    def finish_rule(self, r: Rule, scope: tuple, types: dict) -> list:
        """Final per-rule passes; returns the rule plus any helper items.
        `types` are the rule's variable types (decl_var_types)."""
        r = self.rewrite_loop_reads(r, scope)
        body = r.body
        if body is not None:
            body = self.scope_aggregates(body, r.heads, r.line)
            body = self.lift_atom_args(body)
        items = self.split_computed_negations(
            Rule(r.heads, body, r.line, r.sub, r.sx, r.plan), scope
        )
        out: list = []
        for it in items:
            if not isinstance(it, Rule):
                out.append(it)
                continue
            heads, body = it.heads, it.body
            heads = [self.choice_head(h, scope, it.line, it.sub) for h in heads]
            if body == [[]]:
                body = None
            tenv = self.rule_type_env(body, types, scope)
            heads, body = self.anchor_widths(heads, body, tenv)
            if body is None:
                if any(not isinstance(a, (Num, Str)) for h in heads for a in h.args):
                    # FlowLog only accepts constant facts; a computed one
                    # (e.g. a choice key) reads the one-row unit relation
                    self.need_unit = True
                    body = [[Atom(UNIT_REL, [Wild()])]]
            else:
                # FlowLog rejects a conjunction reading no relation
                anchored = [
                    c if has_source(c) else [Atom(UNIT_REL, [Wild()])] + c for c in body
                ]
                self.need_unit |= anchored != body
                body = anchored
            sub = self.spell_vars(heads, body)
            if sub:
                heads = [subst_lit(h, sub) for h in heads]
                if body is not None:
                    body = [[subst_lit(l, sub) for l in c] for c in body]
            out.append(
                Rule(heads, body, it.line, it.sub, it.sx, self.flowlog_plan(it, body))
            )
        return out

    def note_plan_atoms(self, items: list) -> None:
        """Souffle applies a `.plan` to each clause it makes of the rule, once
        relations are inlined; split_choice_rules and inline_relations make
        the same clauses (in Souffle's order of literals)."""
        for _, r in iter_rules(items):
            if r.plan is not None and r.body is not None:
                r.plan = Plan(r.plan.orders, plan_atoms(souffle_dnf(r.body)))

    def flowlog_plan(self, r: Rule, body: list | None) -> tuple | None:
        """FlowLog's `.plan` for a rule whose source had a Souffle one.
        FlowLog joins every version of a rule in one order, so the order of
        Souffle's version 0 (a delta on the first recursive atom) is kept, if
        each clause of the translated rule reads the positive atoms the plan
        orders (plan_atoms): a range rewrite or the `ChoiceInstance` atom of
        a min-choice read changes them."""
        p = r.plan
        if p is None or 0 not in p.orders:
            return None
        atoms = plan_atoms(flowlog_clauses(body)) if body is not None else None
        if atoms is None or p.atoms is None or atoms != p.atoms:
            self.warn(
                f"line {r.line}: .plan dropped (the translated rule reads other atoms)"
            )
            return None
        order = p.orders[0]
        if sorted(order) != list(range(1, len(atoms) + 1)):
            self.warn(
                f"line {r.line}: .plan dropped (version 0 does not order the rule's {len(atoms)} atoms)"
            )
            return None
        return order

    @staticmethod
    def spell_vars(heads: list, body: list | None) -> dict:
        """A rule's variables respelled as FlowLog identifiers (fl_name),
        kept distinct: a legal name stays, an illegal one gets the first free
        `fl_name(v)`, `fl_name(v)_1`, ..."""
        names: set = set()
        for h in heads:
            lit_vars(h, names)
        for c in body or []:
            for l in c:
                lit_vars(l, names)
        used = {v for v in names if fl_name(v) == v}
        sub: dict = {}
        for v in sorted(names - used):
            cand, k = fl_name(v), 0
            while cand in used:
                k += 1
                cand = f"{fl_name(v)}_{k}"
            used.add(cand)
            sub[v] = Var(cand)
        return sub

    # -- negated atoms over computed values
    def value_kinds(self, conj: list) -> dict:
        """Mirror FlowLog's assignment substitution: maps each variable of a
        conjunction to 'atomic' (bound by a positive atom or an aggregate, or
        assigned a variable/constant) or 'computed' (assigned an expression).
        FlowLog expands a disjunction into one rule per branch, so a variable
        atomic in every branch of one is atomic."""
        kind: dict = {}
        for lit in conj:
            if isinstance(lit, Atom):
                for a in lit.args:
                    if isinstance(a, Var):
                        kind[a.name] = "atomic"
        for lit in conj:
            if isinstance(lit, Group) and lit.disj:
                branches = [self.value_kinds(c) for c in lit.disj]
                for v in set.intersection(*(set(b) for b in branches)):
                    if all(b[v] == "atomic" for b in branches):
                        kind.setdefault(v, "atomic")
        changed = True
        while changed:
            changed = False
            for lit in conj:
                if not (isinstance(lit, Cmp) and lit.op == "="):
                    continue
                for x, e in ((lit.l, lit.r), (lit.r, lit.l)):
                    if not isinstance(x, Var) or x.name in kind:
                        continue
                    if not expr_vars(e, set(), include_agg=False) <= set(kind):
                        continue
                    if isinstance(e, (Agg, Num, Str)):
                        kind[x.name] = "atomic"
                    elif isinstance(e, Var):
                        kind[x.name] = kind[e.name]
                    else:
                        kind[x.name] = "computed"
                    changed = True
                    break
        return kind

    def split_computed_negations(self, r: Rule, scope: tuple) -> list:
        """FlowLog substitutes `t = expr` into every use of `t`, but a negated
        atom (or an atom inside an aggregate) only accepts a variable or a
        constant. A conjunction with such a negation is split: its positive
        part is materialized into a fresh relation that the negation then
        filters."""
        if r.body is None:
            return [r]
        keep: list = []
        split: list = []
        for conj in r.body:
            kind = self.value_kinds(conj)
            bad = []
            for i, lit in enumerate(conj):
                if isinstance(lit, Neg):
                    vs = [a.name for a in lit.atom.args if isinstance(a, Var)]
                    if any(kind.get(v, "computed") == "computed" for v in vs):
                        bad.append(i)
                elif isinstance(lit, Group):
                    for c2 in lit.disj:
                        for l2 in c2:
                            if isinstance(l2, Neg) and any(
                                isinstance(a, Var) and kind.get(a.name) == "computed"
                                for a in l2.atom.args
                            ):
                                raise TranslateError(
                                    f"line {r.line}: negation over a computed value inside a disjunction"
                                )
            for agg in [a for lit in conj for a in all_aggs_in_lit(lit, [])]:
                for l2 in agg.body[0]:
                    if isinstance(l2, Atom) and any(
                        isinstance(a, Var) and kind.get(a.name) == "computed"
                        for a in l2.args
                    ):
                        raise TranslateError(
                            f"line {r.line}: aggregate atom over a computed value"
                        )
            (split if bad else keep).append((conj, bad))
        if not split:
            return [r]
        items: list = []
        if keep:
            items.append(
                Rule(r.heads, [c for c, _ in keep], r.line, r.sub, r.sx, r.plan)
            )
        for conj, bad in split:
            pre = [lit for i, lit in enumerate(conj) if i not in bad]
            negs = [conj[i] for i in bad]
            wanted: list = []
            for h in r.heads:
                for a in h.args:
                    for v in sorted(expr_vars(a, set())):
                        if v not in wanted:
                            wanted.append(v)
            for n in negs:
                for a in n.atom.args:
                    if isinstance(a, Var) and a.name not in wanted:
                        wanted.append(a.name)
            pre_vars: set = set()
            for lit in pre:
                lit_vars(lit, pre_vars)
            need = [v for v in wanted if v in pre_vars]
            types = self.infer_var_types(pre, scope)
            missing = [v for v in need if v not in types]
            if missing:
                raise TranslateError(
                    f"line {r.line}: cannot infer the type of {missing}"
                )
            name = f"NegPre{self.fresh('')}"
            attrs = ", ".join(
                f"c{i}: {self.fl_type(types[v])}" for i, v in enumerate(need)
            )
            items.append(Raw(f".decl {name}({attrs})"))
            # the positive part keeps the source's atoms, and so its `.plan`
            items.append(
                Rule([Atom(name, [Var(v) for v in need])], [pre], r.line, plan=r.plan)
            )
            items.append(
                Rule(
                    r.heads,
                    [[Atom(name, [Var(v) for v in need])] + negs],
                    r.line,
                    r.sub,
                    r.sx,
                )
            )
        return items

    def infer_var_types(self, conj: list, scope: tuple) -> dict:
        types: dict = {}

        def visit_atoms(lits):
            for lit in lits:
                if isinstance(lit, Atom):
                    for t, a in zip(self.atom_col_types(lit.name, scope), lit.args):
                        if isinstance(a, Var):
                            types.setdefault(a.name, t)
                elif isinstance(lit, Group):
                    for c in lit.disj:
                        visit_atoms(c)

        def eqs(lits):
            for lit in lits:
                if isinstance(lit, Cmp) and lit.op == "=":
                    yield lit
                elif isinstance(lit, Group):
                    for c in lit.disj:
                        yield from eqs(c)

        visit_atoms(conj)
        changed = True
        while changed:
            changed = False
            for lit in eqs(conj):
                for x, e in ((lit.l, lit.r), (lit.r, lit.l)):
                    if isinstance(x, Var) and x.name not in types:
                        t = self.expr_type(e, types)
                        if t is not None:
                            types[x.name] = t
                            changed = True
        return types

    def atom_col_types(self, name: str, scope: tuple) -> list:
        """Column types of an atom, including the relations the translation
        introduces (`R_choice`, `ChoiceInstance`, the unit relation)."""
        d = self.resolve_decl(name, scope)
        if d is not None:
            return [t for _, t in self.flat_attrs(self.decl_attr_types(d, name, scope))]
        base = name.split(".")[-1]
        if base in ("ChoiceInstance", UNIT_REL):
            return ["int32"]
        if base.endswith("_choice"):
            d = self.resolve_decl(name[: -len("_choice")], scope)
            if d is not None and d.choice and self.is_minchoice(d.name):
                keys = self.choice_keys(d)
                attrs = self.flat_attrs(
                    self.decl_attr_types(d, name[: -len("_choice")], scope)
                )
                return [t for a, t in attrs if a in keys] + ["int64"]
        return []

    def expr_type(self, e: Any, env: dict, lit: bool = True) -> str | None:
        """The Souffle type of `e`; with `lit` false, None for a numeric
        literal (whose type Souffle takes from the context)."""
        if isinstance(e, Var):
            return env.get(e.name)
        if isinstance(e, Str):
            return "symbol"
        if isinstance(e, Num):
            return (
                None if not lit else "float" if isinstance(e.val, float) else "number"
            )
        if isinstance(e, Cast):
            return e.ty
        if isinstance(e, Un):
            return self.expr_type(e.e, env, lit)
        if isinstance(e, Agg):
            if e.kind == "count":
                return "number"
            return (
                self.expr_type(e.target, env, lit) if e.target is not None else "number"
            )
        if isinstance(e, Bin):
            # a literal takes the other operand's type: `u / 2` is unsigned
            for x in sorted((e.l, e.r), key=literal_expr):
                t = self.expr_type(x, env, lit)
                if t is not None:
                    return t
            return None
        if isinstance(e, Call):
            if e.name in ("cat", "substr", "to_string", "choice_field", ERROR_OF):
                return "symbol"
            if e.name in ("strlen", "ord", "to_number", "choice_number"):
                return "number"
            for udf, _, ret in SOUFFLE_UDFS.values():
                if e.name == udf:
                    return UDF_RESULT[ret]
                if e.name == udf + "_error":
                    return "symbol"
            if e.name in self.functors:
                return self.functors[e.name].ret
        return None

    def partial_port(self, e: Bin, types: dict, line: int) -> str:
        """The SOUFFLE_UDFS key of `/`, `%` or `^` for its operands' type."""
        t = self.expr_type(e, types)
        b = self.base_type(t) if t is not None else NUMBER
        if b == NUMBER:
            return e.op
        if b == PRIM_TYPES["unsigned"] and e.op in ("/", "%"):
            return "u" + e.op
        raise TranslateError(
            f"line {line}: operator {e.op} over type {t} is not supported"
        )

    def type_context_partials(
        self, heads: list, body: list | None, types: dict, scope: tuple, line: int
    ):
        """Souffle types a `/`, `%` or `^` whose operands have no type of
        their own (literals) by its context, as other arithmetic: with u
        unsigned, `u > (0 - 1) / 2` divides unsigned.  partial_port reads the
        operands only, so such an operation in an unsigned context is
        respelled `u/` or `u%` here; in other contexts but number (`^` on
        unsigned, a type parameter) it is rejected, as partial_port does."""

        def own(e):
            return self.expr_type(e, types, lit=False)

        def needed(e):
            return isinstance(e, Bin) and e.op in ("/", "%", "^") and own(e) is None

        if not any(
            collect_exprs(lit, needed, [])
            for lit in heads + [l1 for c in body or [] for l1 in c]
        ):
            return heads, body

        def expr(e, t):
            if isinstance(e, Bin):
                if own(e) is not None:
                    t = own(e)
                elif e.op in ("/", "%", "^") and t is not None:
                    try:
                        b = self.base_type(t)
                    except TranslateError:
                        b = None
                    if b == PRIM_TYPES["unsigned"] and e.op != "^":
                        return Bin("u" + e.op, expr(e.l, t), expr(e.r, t))
                    if b != NUMBER:
                        raise TranslateError(
                            f"line {line}: operator {e.op} over type {t} is not supported"
                        )
                return Bin(e.op, expr(e.l, t), expr(e.r, t))
            if isinstance(e, Un):
                return Un(e.op, expr(e.e, t))
            if isinstance(e, Call):
                if e.name == "range" and not e.functor:
                    ts = [t] * len(e.args)
                elif e.functor and e.name in self.functors:
                    ts = [pt for _, pt in self.functors[e.name].params]
                else:
                    ts = []
                ts += [None] * (len(e.args) - len(ts))
                return Call(
                    e.name, [arg(a, pt) for a, pt in zip(e.args, ts)], e.functor
                )
            if isinstance(e, Rec):
                return Rec([expr(x, None) for x in e.elems])
            if isinstance(e, Cast):
                return Cast(expr(e.e, None), e.ty)
            if isinstance(e, Agg):
                target = None if e.target is None else expr(e.target, None)
                return Agg(e.kind, target, [[lit(l2) for l2 in c] for c in e.body])
            return e

        def arg(e, t):
            fields = self.safe_record_fields(t)
            if (
                isinstance(e, Rec)
                and fields is not None
                and len(fields) == len(e.elems)
            ):
                return Rec([arg(x, ft) for x, (_, ft) in zip(e.elems, fields)])
            return expr(e, t)

        def atom(a):
            d = self.resolve_decl(a.name, scope)
            ts = [None] * len(a.args)
            if d is not None and len(d.attrs) == len(a.args):
                ts = [t for _, t in self.decl_attr_types(d, a.name, scope)]
            return Atom(a.name, [arg(x, t) for x, t in zip(a.args, ts)])

        def lit(x):
            if isinstance(x, Atom):
                return atom(x)
            if isinstance(x, Neg):
                return Neg(atom(x.atom))
            if isinstance(x, Cmp):
                t = own(x.l) or own(x.r)
                return Cmp(x.op, arg(x.l, t), arg(x.r, t))
            if isinstance(x, Pred):
                return Pred(x.name, [expr(a, None) for a in x.args], x.neg)
            if isinstance(x, Group):
                return Group([[lit(l2) for l2 in c] for c in x.disj])
            return x

        heads = [atom(h) for h in heads]
        if body is not None:
            body = [[lit(l1) for l1 in c] for c in body]
        return heads, body

    def expr_rewriter(self, types: dict, line: int):
        def f(e: Any) -> Any:
            if isinstance(e, Call) and e.name == ERROR_OF:
                # its argument, a partial call, is rewritten already
                c = e.args[0]
                key = PARTIAL_BY_PORT[c.name]
                self.error_udfs.add(key)
                return Call(c.name + "_error", c.args)
            if isinstance(e, Call):
                if e.functor:
                    if e.name not in self.functors:
                        raise TranslateError(f"unknown functor @{e.name}")
                    return Call(e.name, e.args, functor=False)
                if e.name == "cat" and len(e.args) > 2:
                    acc = e.args[-1]
                    for a in reversed(e.args[:-1]):
                        acc = Call("cat", [a, acc])
                    return acc
                if e.name == "cat" and len(e.args) == 1:
                    return e.args[0]
                if e.name == "to_string":
                    t = self.expr_type(e.args[0], types)
                    if t is not None and self.base_type(t) == PRIM_TYPES["symbol"]:
                        return e.args[0]
                if e.name in (
                    "min",
                    "max",
                    "lnot",
                    "bnot",
                    "to_unsigned",
                    "to_float",
                    "autoinc",
                ):
                    raise TranslateError(
                        f"builtin {e.name} is not supported by FlowLog"
                    )
                if e.name in SOUFFLE_UDFS:
                    self.souffle_udfs.add(e.name)
                    return Call(SOUFFLE_UDFS[e.name][0], e.args)
            if isinstance(e, Num) and isinstance(e.val, float):
                raise TranslateError(f"float literal {e.val} is not supported")
            if isinstance(e, Agg) and e.kind == "mean":
                raise TranslateError("mean is not supported (its result is a float)")
            if isinstance(e, Bin) and e.op in ("u/", "u%"):
                self.souffle_udfs.add(e.op)
                return Call(SOUFFLE_UDFS[e.op][0], [e.l, e.r])
            if isinstance(e, Bin) and e.op in ("/", "%", "^"):
                key = self.partial_port(e, types, line)
                self.souffle_udfs.add(key)
                return Call(SOUFFLE_UDFS[key][0], [e.l, e.r])
            if isinstance(e, Cast):
                return e.e
            if isinstance(e, Un) and e.op == "-":
                if isinstance(e.e, Num):
                    return Num(-e.e.val)
                return Bin("-", Num(0), e.e)
            if isinstance(e, Un):
                raise TranslateError(f"unary {e.op} not supported")
            if isinstance(e, Nil):
                raise TranslateError("nil records are not supported")
            if isinstance(e, Bin) and e.op not in (
                "+",
                "-",
                "*",
                "band",
                "bor",
                "bxor",
                "bshl",
                "bshr",
                "bshru",
            ):
                raise TranslateError(f"operator {e.op} not supported")
            return e

        return f

    def map_body(self, body: list, f) -> list:
        return [[f(lit) for lit in conj] for conj in body]

    def rewrite_agg_bodies(self, body: list, f) -> list:
        def g(e):
            if isinstance(e, Agg):
                nb = self.map_body(e.body, lambda lit: map_lit_exprs(lit, f))
                nb = self.rewrite_agg_bodies(nb, f)
                return Agg(e.kind, e.target, nb)
            return e

        return self.map_body(body, lambda lit: map_lit_exprs(lit, g))

    def map_atoms(self, body: list, f) -> list:
        def lit_f(lit):
            if isinstance(lit, Atom):
                return f(lit)
            if isinstance(lit, Neg):
                return Neg(f(lit.atom))
            if isinstance(lit, Group):
                return Group(self.map_atoms(lit.disj, f))
            return lit

        def agg_f(e):
            if isinstance(e, Agg):
                return Agg(e.kind, e.target, self.map_atoms(e.body, f))
            return e

        body = [[lit_f(lit) for lit in conj] for conj in body]
        return self.map_body(body, lambda lit: map_lit_exprs(lit, agg_f))

    # -- integer widths
    def int_prim(self, t: str | None) -> str | None:
        """int64 or uint64 for an integer type, else None (also for an
        unknown type or a component type parameter)."""
        if t is None or t in INT_RANGES:
            return t
        try:
            b = self.base_type(t)
        except TranslateError:
            return None
        return b if isinstance(b, str) and b in INT_RANGES else None

    def param_prims(self, c: Call) -> list:
        """int_prim of each parameter of a port or functor call."""
        if c.name in PORT_PARAMS:
            ps = PORT_PARAMS[c.name]
        elif c.name in self.functors:
            ps = [t for _, t in self.functors[c.name].params]
        else:
            ps = []
        return [self.int_prim(t) for t in ps] + [None] * (len(c.args) - len(ps))

    def safe_record_fields(self, t: str | None) -> list | None:
        try:
            return (
                self.record_fields(t)
                if t is not None and self.is_record_type(t)
                else None
            )
        except TranslateError:
            return None

    def involves_unsigned(self, heads: list, body: list, scope: tuple) -> bool:
        """Whether a rule may compute on `unsigned`: an atom (at any depth)
        has a column of that base type or of a component type parameter, or
        a cast or functor names such a type."""

        def maybe(t):
            if self.int_prim(t) == PRIM_TYPES["unsigned"]:
                return True
            try:
                b = self.base_type(t)
            except TranslateError:
                return False
            return isinstance(b, str) and b in self.tparams

        found: list = []

        def probe(e):
            if isinstance(e, Cast) and maybe(e.ty):
                found.append(e)
            elif isinstance(e, Call) and e.functor and e.name in self.functors:
                fn = self.functors[e.name]
                if maybe(fn.ret) or any(maybe(t) for _, t in fn.params):
                    found.append(e)
            return e

        for lit in heads + [l1 for c in body for l1 in c]:
            if any(
                maybe(t)
                for a in lit_atoms(lit)
                for t in self.atom_col_types(a.name, scope)
            ):
                return True
            deep_map_lit(lit, probe)
        return bool(found)

    def alias_constants(
        self, heads: list, body: list, scope: tuple, line: int
    ) -> list | None:
        """Souffle's ResolveAliases replaces a variable equated to a constant
        expression by that expression before it types the clause, so each
        copy takes its type from where it lands: in `A(x) :- x = 3, x - 10 <
        0.` with A unsigned, `3 - 10 < 0` compares numbers (true), where
        FlowLog, typing `x` once, would compare unsigned (false).  In rules
        that may compute on unsigned (in the others every integer is a number
        either way), this substitutes the variables of the alias classes no
        positive atom binds (at any depth), which ResolveAliases always
        resolves, and checks the rest (see alias_constants_conj).

        Souffle resolves each clause of the rule's disjunctive normal form
        apart, so a rule changed here is split into them.  Returns None if
        nothing changes, else the clauses as (heads, conjunction) pairs."""
        if not self.involves_unsigned(heads, body, scope):
            return None
        out: list = []
        changed = False
        for conj in souffle_dnf(body):
            sub = self.alias_constants_conj(heads, conj, scope, line)
            if sub:
                changed = True
                out.append(
                    (
                        [subst_lit(h, sub) for h in heads],
                        [subst_lit(l1, sub) for l1 in conj],
                    )
                )
            else:
                out.append((heads, conj))
        return out if changed else None

    def alias_constants_conj(
        self, heads: list, conj: list, scope: tuple, line: int
    ) -> dict:
        """The substitution of the variables Souffle surely replaces by a
        constant expression in this clause of a rule (see alias_constants).

        Souffle 2.5's ResolveAliases takes the clause's `=` (aggregate bodies
        included) last first: `v = w` maps v to w, and `v = t` maps v to t
        unless an argument of a top-level positive atom binds v.  A class of
        variables equated to each other and to one constant expression, none
        bound by a positive atom, is therefore always replaced by it.  Where
        an atom binds a member, inside an aggregate (left alone or moved to a
        clause of its own by materialization) or alongside a member it does
        not bind, whether a copy of the constant or a variable lands at a use
        depends on the order of the equalities.  That use is typed alike
        either way unless all its other operands are constants too; the rule
        is rejected when such a use is computed on an unsigned or unknown
        type with an operator whose result depends on the signedness."""
        eqs: list = []
        top: set = set()
        bound: set = set()

        def binds(atom, out):
            for a in atom.args:
                if isinstance(a, Var):
                    out.add(a.name)
                for rec in sx_records(a, []):
                    out.update(x.name for x in rec.elems if isinstance(x, Var))

        def pair(l, r):
            if (
                isinstance(l, Rec)
                and isinstance(r, Rec)
                and len(l.elems) == len(r.elems)
            ):
                for a, b in zip(l.elems, r.elems):
                    pair(a, b)
            elif l != r:
                eqs.append((l, r))

        def walk(lits, at_top):
            for lit in lits:
                if isinstance(lit, Group):
                    for c in lit.disj:
                        walk(c, False)
                    continue
                if isinstance(lit, Atom):
                    binds(lit, bound)
                    if at_top:
                        binds(lit, top)
                elif isinstance(lit, Cmp) and lit.op == "=":
                    pair(lit.l, lit.r)
                for agg in all_aggs_in_lit(lit, []):
                    for c in agg.body:
                        walk(c, False)

        walk(conj, True)
        if not eqs:
            return {}
        parent: dict = {}

        def find(v):
            parent.setdefault(v, v)
            while parent[v] != v:
                parent[v] = parent[parent[v]]
                v = parent[v]
            return v

        for l, r in eqs:
            if isinstance(l, Var) and isinstance(r, Var):
                parent[find(l.name)] = find(r.name)
        defs: dict = {}
        for l, r in eqs:
            for v, t in ((l, r), (r, l)):
                if (
                    isinstance(v, Var)
                    and not isinstance(t, Var)
                    and v.name not in expr_vars(t, set())
                    and not isinstance(t, Agg)
                    and not (
                        isinstance(t, Call)
                        and not t.functor
                        and t.name in SX_GENERATORS
                    )
                ):
                    d = defs.setdefault(find(v.name), [])
                    if t not in d:
                        d.append(t)
        if not defs:
            return {}
        members: dict = {}
        for v in list(parent):
            members.setdefault(find(v), set()).add(v)

        def shaped(t, ok):
            """t is a constant expression over the variables of classes in ok."""
            if isinstance(t, Num):
                return isinstance(t.val, int)
            if isinstance(t, Var):
                return find(t.name) in ok
            if isinstance(t, Un):
                return t.op == "-" and shaped(t.e, ok)
            if isinstance(t, Bin):
                return t.op in CONST_OPS and shaped(t.l, ok) and shaped(t.r, ok)
            return False

        def lfp(cands, need):
            ok: set = set()
            grew = True
            while grew:
                grew = False
                for c in cands:
                    if c not in ok and need(shaped(t, ok) for t in defs[c]):
                        ok.add(c)
                        grew = True
            return ok

        # the classes surely replaced, and those that may be
        pure = lfp([c for c in defs if not members[c] & bound], all)
        consts = lfp([c for c in defs if not members[c] <= top], any)
        for c in sorted(consts, key=lambda c: sorted(members[c])):
            if len(defs[c]) > 1:
                v = sorted(members[c])[0]
                raise TranslateError(
                    f"line {line}: `{v}` is equated to several expressions ({', '.join(sx_text(t) for t in defs[c])}); "
                    "Souffle keeps one, chosen by the order of the equalities, and types its copies by where "
                    "they land — equate it to one only"
                )
        memo: dict = {}

        def res(t):
            def f(x):
                if isinstance(x, Var) and find(x.name) in consts:
                    c = find(x.name)
                    if c not in memo:
                        memo[c] = res(defs[c][0])
                    return memo[c]
                return x

            return map_expr(t, f)

        kvars = {m for c in consts for m in members[c]}
        risky = {m for c in consts - pure for m in members[c]}
        if risky:
            self.check_alias_uses(heads, conj, scope, line, kvars, risky, res)
        return {m: res(Var(m)) for c in pure for m in sorted(members[c])}

    def check_alias_uses(
        self,
        heads: list,
        conj: list,
        scope: tuple,
        line: int,
        kvars: set,
        risky: set,
        res,
    ) -> None:
        """Rejects a use of a `risky` variable (one Souffle may or may not
        replace by its constant, see alias_constants_conj) that is typed by
        nothing but variables of `kvars` (those it may replace) and constants,
        and computed with an operator sensitive to signedness on a type other
        than number.  A use is the operands of a comparison, or an argument,
        with the arithmetic around them: Souffle types them together."""
        units: list = []

        def scan(e, u):
            # u = [variables, typed, signed, equality sides, text]
            if isinstance(e, Var):
                u[0].add(e.name)
                if e.name not in kvars:
                    u[1] = True
            elif isinstance(e, Un) and e.op == "-":
                scan(e.e, u)
            elif isinstance(e, Bin):
                u[2] = u[2] or e.op in SIGNED_OPS
                scan(e.l, u)
                scan(e.r, u)
            elif isinstance(e, Call) and not e.functor and e.name in SX_GENERATORS:
                u[2] = True
                for a in e.args:
                    scan(a, u)
            elif not isinstance(e, (Num, Wild)):
                u[1] = (
                    True  # a string, cast, call, record or aggregate has a type of its own
                )
                if isinstance(e, Agg):
                    if e.target is not None:
                        unit(e.target, False, e.kind in ("min", "max"))
                    for c in e.body:
                        for l2 in c:
                            visit(l2)
                elif isinstance(e, Call):
                    for a in e.args:
                        unit(
                            a,
                            e.functor or e.name in TYPED_PARAM_BUILTINS,
                            e.name != "ord",
                        )
                else:
                    for k in expr_children(e):
                        unit(k, isinstance(e, Rec), not isinstance(e, Cast))

        def unit(e, typed, signed):
            u = [set(), typed, signed, None, e]
            scan(e, u)
            units.append(u)

        def visit(lit):
            if isinstance(lit, (Atom, Neg)):
                for a in (lit.atom if isinstance(lit, Neg) else lit).args:
                    unit(a, True, False)
            elif isinstance(lit, Cmp):
                u = [
                    set(),
                    False,
                    lit.op not in ("=", "!="),
                    (lit.l, lit.r) if lit.op == "=" else None,
                    lit,
                ]
                scan(lit.l, u)
                scan(lit.r, u)
                units.append(u)
            elif isinstance(lit, Pred):
                for a in lit.args:
                    unit(a, True, False)
            elif isinstance(lit, Group):
                for c in lit.disj:
                    for l2 in c:
                        visit(l2)

        for lit in heads + conj:
            visit(lit)
        try:
            types = self.decl_var_types(heads, [conj], scope, line)
        except TranslateError:
            types = {}
        for vs, typed, signed, eq, what in units:
            if typed or not signed or (eq is not None and res(eq[0]) == res(eq[1])):
                continue
            hit = sorted(v for v in vs & risky if self.int_prim(types.get(v)) != NUMBER)
            if hit:
                v = hit[0]
                t = types.get(v) or "an unknown type"
                text = (
                    f"{sx_text(what.l)} {what.op} {sx_text(what.r)}"
                    if isinstance(what, Cmp)
                    else sx_text(what)
                )
                raise TranslateError(
                    f"line {line}: Souffle computes `{text}` on {t} or on number, as its alias resolution "
                    f"leaves `{v}` or replaces it by its constant {sx_text(res(Var(v)))} (which depends on "
                    "the order of the equalities and on aggregate materialization); write the constant or "
                    f"an atom-bound variable in place of `{v}` there"
                )

    def fold_constants(self, heads: list, body: list | None, scope: tuple, line: int):
        """Evaluates the rule's integer constant expressions as Souffle does,
        on 64 bits with wrap-around: FlowLog computes an expression that no
        column or parameter types on int32.  A variable equated to a
        constant is valued in its conjunction (and the aggregates and
        disjunctions in it), comparisons of constants become true or false,
        and an aggregate over an always false body becomes 0 (count, sum),
        or makes its literal false (min, max: no value).

        The type of an expression comes from the column or parameter it
        fills, else from the declared type of a variable in it (variables
        equated to literals only have none), else it is `number` when it
        reads literals only.  Otherwise it is folded only where `number` and
        `unsigned` agree.  Returns None if the body is never satisfied."""
        certain = self.decl_var_types(heads, body or [], scope, line, lit=False)

        def ctype(e):
            return self.int_prim(self.expr_type(e, certain, lit=False))

        def evaluate(e, p, env):
            if p is not None:
                return eval_const(e, p, env)
            a, b = eval_const(e, "int64", env), eval_const(e, "uint64", env)
            return a if a is not None and a == b else None

        def fold(e, p, env):
            if isinstance(e, Agg):
                return fold_agg(e, env)
            if isinstance(e, Call):
                if e.name == "to_string":
                    a = e.args[0]
                    return Call(
                        e.name,
                        [
                            fold(
                                a,
                                ctype(a) or (NUMBER if literal_expr(a) else None),
                                env,
                            )
                        ],
                        e.functor,
                    )
                ps = [p] * len(e.args) if e.name == "range" else self.param_prims(e)
                return Call(
                    e.name, [fold(a, q, env) for a, q in zip(e.args, ps)], e.functor
                )
            if isinstance(e, Bin):
                p = p or ctype(e)
                v = evaluate(e, p, env)
                if v is not None:
                    return Num(v)
                return Bin(e.op, fold(e.l, p, env), fold(e.r, p, env))
            if isinstance(e, Rec):
                return Rec([fold(x, None, env) for x in e.elems])
            return e

        def typed_fold(e, t, env):
            fields = self.safe_record_fields(t)
            if (
                isinstance(e, Rec)
                and fields is not None
                and len(fields) == len(e.elems)
            ):
                return Rec(
                    [typed_fold(x, ft, env) for x, (_, ft) in zip(e.elems, fields)]
                )
            return fold(e, self.int_prim(t), env)

        def fold_atom(a, env):
            d = self.resolve_decl(a.name, scope)
            ts = [None] * len(a.args)
            if d is not None and len(d.attrs) == len(a.args):
                ts = [t for _, t in self.decl_attr_types(d, a.name, scope)]
            return Atom(a.name, [typed_fold(x, t, env) for x, t in zip(a.args, ts)])

        def fold_agg(a, env):
            tvars = expr_vars(a.target, set()) if a.target is not None else set()
            inner = {k: v for k, v in env.items() if k not in tvars}
            if len(a.body) == 1:
                conjs = [fold_conj(a.body[0], inner)]
            else:
                conjs = [fold_conj(c, dict(inner)) for c in a.body]
                inner = {}
            folded = self.simplify_bools(conjs)
            if folded is None:
                if a.kind in ("count", "sum"):
                    return Num(0)
                raise EmptyAggregate()
            target = (
                None if a.target is None else fold(a.target, ctype(a.target), inner)
            )
            return Agg(a.kind, target, folded)

        def fold_cmp_lit(c, env, is_def):
            p = (
                ctype(c.l)
                or ctype(c.r)
                or (NUMBER if literal_expr(c.l) and literal_expr(c.r) else None)
            )
            sides = []
            for x, y in ((c.l, c.r), (c.r, c.l)):
                t = certain.get(y.name) if isinstance(y, Var) else None
                sides.append(
                    typed_fold(x, t, env)
                    if self.safe_record_fields(t) is not None
                    else fold(x, p, env)
                )
            l, r = sides
            if not is_def:
                vl, vr = evaluate(l, p, env), evaluate(r, p, env)
                if vl is not None and vr is not None:
                    return BoolLit(CMP_FNS[c.op](vl, vr))
            return Cmp(c.op, l, r)

        def fold_lit(lit, env, is_def):
            try:
                if isinstance(lit, Atom):
                    return fold_atom(lit, env)
                if isinstance(lit, Neg):
                    return Neg(fold_atom(lit.atom, env))
                if isinstance(lit, Cmp):
                    return fold_cmp_lit(lit, env, is_def)
                if isinstance(lit, Pred):
                    return Pred(
                        lit.name, [fold(a, None, env) for a in lit.args], lit.neg
                    )
                if isinstance(lit, Group):
                    return Group([fold_conj(c, dict(env)) for c in lit.disj])
            except EmptyAggregate:
                return BoolLit(False)
            return lit

        def def_value(x, e, env):
            """The value `x = e` gives the variable x, if e is a constant."""
            t = certain.get(x.name)
            p = self.int_prim(t)
            if t is not None and p is None:
                try:
                    b = self.base_type(t)
                except TranslateError:
                    return None
                if not (isinstance(b, str) and b in self.tparams):
                    return None
            if isinstance(e, Num) and isinstance(e.val, int):
                v = e.val
            elif isinstance(e, Var) and e.name in env:
                v = env[e.name]
            else:
                return None
            lo, hi = (
                INT_RANGES[p]
                if p is not None
                else (INT_RANGES["int64"][0], INT_RANGES["uint64"][1])
            )
            return v if lo <= v <= hi else None

        def fold_conj(conj, env):
            """The conjunction folded; `env` (variable -> value) receives the
            values its equalities give."""
            conj = list(conj)
            defs: set = set()
            changed = True
            while changed:
                changed = False
                for i, lit in enumerate(conj):
                    new = fold_lit(lit, env, i in defs)
                    if new != lit:
                        conj[i] = new
                        changed = True
                    if i in defs or not (isinstance(new, Cmp) and new.op == "="):
                        continue
                    for x, e in ((new.l, new.r), (new.r, new.l)):
                        if isinstance(x, Var) and x.name not in env:
                            v = def_value(x, e, env)
                            if v is not None:
                                env[x.name] = v
                                defs.add(i)
                                changed = True
                                break
            return conj

        henv: dict = {}
        if body is not None:
            envs = [{} for _ in body]
            body = self.simplify_bools(
                [fold_conj(c, env) for c, env in zip(body, envs)]
            )
            if body is None:
                return None
            if len(envs) == 1:
                henv = envs[0]
        try:
            heads = [fold_atom(h, henv) for h in heads]
        except EmptyAggregate:
            pass  # an aggregate in a head is rejected later
        return heads, body

    @staticmethod
    def anchor(e: Any, prim: str) -> Cast:
        """`e` pinned to the integer primitive `prim` (a FlowLog `as`)."""
        return Cast(e, prim)

    def rule_type_env(self, body: list | None, types: dict, scope: tuple) -> dict:
        """The rule's variable types (decl_var_types), with those
        infer_var_types finds in each conjunction, disjunct and aggregate
        body for the variables the later passes introduced."""
        tenv = dict(types)

        def note_types(conj):
            for k, v in self.infer_var_types(conj, scope).items():
                tenv.setdefault(k, v)

            def probe(e):
                if isinstance(e, Agg):
                    for c in e.body:
                        note_types(c)
                return e

            for lit in conj:
                if isinstance(lit, Group):
                    for c in lit.disj:
                        note_types(c)
                else:
                    map_lit_exprs(lit, probe)

        for c in body or []:
            note_types(c)
        return tenv

    def anchor_widths(self, heads: list, body: list | None, tenv: dict) -> tuple:
        """Pins to 64 bits the integer expressions FlowLog would compute on
        int32 (after fold_constants, those reading variables).

        FlowLog types a variable bound to a literal, a count, or an
        expression of these, from its uses, through the equality defining
        it: a column, a parameter, or a comparison with a typed operand pins
        it.  A comparison of such terms only, the argument of `to_string`,
        and an arithmetic argument of a UDF fall back to int32, as do the
        aggregates and literals in them.  In these contexts each use of a
        count or computed variable is pinned with `as(_, int64)` (or
        `uint64`), an aggregate being first bound to a variable; failing
        these, if the context computes or holds a literal beyond int32, its
        literal variables and literals beyond int32 are pinned, else its
        first literal.  `tenv` is rule_type_env's, and receives the types
        of the variables introduced."""

        def strong_leaf(x, cls):
            if isinstance(x, Var):
                return x.name in cls[0]
            if isinstance(x, Agg):
                if x.target is None or x.kind == "count":
                    return False
                icls, _ = classify(x.body[0], cls)
                return any(strong_leaf(y, icls) for y in arith_leaves(x.target))
            return isinstance(x, (Str, Call, Cast))

        def def_kind(e, cls):
            if isinstance(e, Num):
                return ("lit", e.val)
            leaves = arith_leaves(e)
            if any(strong_leaf(x, cls) for x in leaves):
                return "strong"
            if isinstance(e, Agg):
                return "agg"
            if all(
                isinstance(x, (Num, Agg)) or isinstance(x, Var) and x.name in cls[1]
                for x in leaves
            ):
                return "expr"
            return None

        def merge_weak(weaks, strong):
            """The weak variables of alternative conjunctions: a literal
            variable stays one if it is in each, with its largest value."""
            out: dict = {}
            for w in weaks:
                for k, v in w.items():
                    old = out.get(k)
                    if k in strong or old is not None and not isinstance(old, tuple):
                        continue
                    if (
                        old is None
                        or not isinstance(v, tuple)
                        or abs(v[1]) > abs(old[1])
                    ):
                        out[k] = v
            return out

        def classify(conj, cls):
            """(strong, weak) for a conjunction, given its context's: the
            variables typed by an atom, or by an equality with a typed term,
            and the others an equality defines, as ("lit", value), "agg"
            or "expr"; with the indices of these defining equalities.  A
            disjunction types the variables it types in each disjunct."""
            strong, weak = set(cls[0]), dict(cls[1])
            for lit in conj:
                if isinstance(lit, Atom):
                    strong.update(a.name for a in lit.args if isinstance(a, Var))
            defs: set = set()
            changed = True
            while changed:
                changed = False
                for i, lit in enumerate(conj):
                    if isinstance(lit, Group) and lit.disj:
                        alts = [classify(c, (strong, weak))[0] for c in lit.disj]
                        both = set.intersection(*(a[0] for a in alts)) - strong
                        new = {
                            k: v
                            for k, v in merge_weak(
                                [a[1] for a in alts], strong | both
                            ).items()
                            if k not in weak
                        }
                        if both or new:
                            strong |= both
                            weak.update(new)
                            changed = True
                    if i in defs or not (isinstance(lit, Cmp) and lit.op == "="):
                        continue
                    for x, e in ((lit.l, lit.r), (lit.r, lit.l)):
                        if not isinstance(x, Var) or x.name in strong or x.name in weak:
                            continue
                        k = def_kind(e, (strong, weak))
                        if k is None:
                            continue
                        if k == "strong":
                            strong.add(x.name)
                        else:
                            weak[x.name] = k
                        defs.add(i)
                        changed = True
                        break
            return (strong, weak), defs

        def anchor_ctx(exprs, prim, cls, hoist):
            leaves = [x for e in exprs for x in arith_leaves(e)]
            if any(strong_leaf(x, cls) for x in leaves):
                return exprs
            weak = cls[1]
            p = prim
            for e in exprs:
                p = p or self.int_prim(self.expr_type(e, tenv))
            p = p or NUMBER
            wrapped: list = []

            def wrap_if(test):
                def g(x):
                    if test(x):
                        wrapped.append(x)
                        return self.anchor(x, p)
                    return x

                return g

            def hoist_agg(x):
                if not isinstance(x, Agg) or hoist is None:
                    return x
                v = Var(self.fresh("agg_v"))
                hoist.append(Cmp("=", v, x))
                weak[v.name] = "agg"
                tenv[v.name] = SOUFFLE_KIND[p]
                return v

            exprs = [map_arith_leaves(e, hoist_agg) for e in exprs]
            exprs = [
                map_arith_leaves(
                    e,
                    wrap_if(
                        lambda x: isinstance(x, Agg)
                        or isinstance(x, Var)
                        and weak.get(x.name) in ("agg", "expr")
                    ),
                )
                for e in exprs
            ]
            if wrapped:
                return exprs

            def lit_var(x):
                return isinstance(x, Var) and isinstance(weak.get(x.name), tuple)

            def big(x):
                v = (
                    x.val
                    if isinstance(x, Num)
                    else weak[x.name][1] if lit_var(x) else None
                )
                return v is not None and not INT32_RANGE[0] <= v <= INT32_RANGE[1]

            leaves = [x for e in exprs for x in arith_leaves(e)]
            if not any(isinstance(e, Bin) for e in exprs) and not any(
                big(x) for x in leaves
            ):
                return exprs
            exprs = [
                map_arith_leaves(e, wrap_if(lambda x: big(x) or lit_var(x)))
                for e in exprs
            ]
            if wrapped:
                return exprs
            first = next((x for x in leaves if isinstance(x, Num)), None) or next(
                (x for x in leaves if isinstance(x, Var)), None
            )
            return [
                map_arith_leaves(e, wrap_if(lambda x: x is first and not wrapped))
                for e in exprs
            ]

        def fix_expr(e, cls, hoist):
            """`e` with the contexts in it anchored: `to_string` arguments
            and arithmetic UDF arguments, aggregate bodies."""
            if isinstance(e, Bin):
                return Bin(e.op, fix_expr(e.l, cls, hoist), fix_expr(e.r, cls, hoist))
            if isinstance(e, Agg):
                icls, _ = classify(e.body[0], cls)
                target = None if e.target is None else fix_expr(e.target, icls, None)
                return Agg(e.kind, target, [fix_conj(e.body[0], cls, False)])
            if isinstance(e, Call):
                args = [fix_expr(a, cls, hoist) for a in e.args]
                if e.name == "to_string":
                    args = anchor_ctx(args, None, cls, hoist)
                elif e.name != "cat":
                    ps = self.param_prims(e)
                    args = [
                        anchor_ctx([a], q, cls, hoist)[0] if isinstance(a, Bin) else a
                        for a, q in zip(args, ps)
                    ]
                return Call(e.name, args, e.functor)
            return e

        def fix_lit(lit, cls, is_def, hoist):
            if isinstance(lit, Atom):
                return Atom(lit.name, [fix_expr(a, cls, hoist) for a in lit.args])
            if isinstance(lit, Neg):
                return Neg(fix_lit(lit.atom, cls, False, hoist))
            if isinstance(lit, Pred):
                return Pred(
                    lit.name, [fix_expr(a, cls, hoist) for a in lit.args], lit.neg
                )
            if isinstance(lit, Group):
                return Group([fix_conj(c, cls, hoist is not None) for c in lit.disj])
            if isinstance(lit, Cmp):
                l, r = fix_expr(lit.l, cls, hoist), fix_expr(lit.r, cls, hoist)
                if not is_def:
                    l, r = anchor_ctx([l, r], None, cls, hoist)
                return Cmp(lit.op, l, r)
            return lit

        def fix_conj(conj, cls, top):
            """Aggregates are bound to variables before the literal using
            them in a rule body (`top`), wrapped in place in an aggregate
            body."""
            cls, defs = classify(conj, cls)
            out: list = []
            for i, lit in enumerate(conj):
                hoist: list | None = [] if top else None
                lit = fix_lit(lit, cls, i in defs, hoist)
                out += (hoist or []) + [lit]
            return out

        hcls: tuple = (set(), {})
        if body is not None:
            clss = [classify(c, hcls)[0] for c in body]
            body = [fix_conj(c, hcls, True) for c in body]
            if clss:
                # the head sees the variables typed in every disjunct as typed
                strong = set.intersection(*(c[0] for c in clss))
                hcls = (strong, merge_weak([c[1] for c in clss], strong))
        heads = [Atom(h.name, [fix_expr(a, hcls, None) for a in h.args]) for h in heads]
        return heads, body

    # -- booleans
    def simplify_bools(self, disj: list) -> list | None:
        """Remove true/false; returns None if the disjunction is unsatisfiable."""
        out = []
        for conj in disj:
            new_conj = []
            ok = True
            for lit in conj:
                if isinstance(lit, BoolLit):
                    if lit.val:
                        continue
                    ok = False
                    break
                if isinstance(lit, Group):
                    inner = self.simplify_bools(lit.disj)
                    if inner is None:
                        ok = False
                        break
                    if inner == [[]]:
                        continue
                    if len(inner) == 1:
                        new_conj.extend(inner[0])
                        continue
                    lit = Group(inner)
                new_conj.append(lit)
            if ok:
                out.append(new_conj)
        if not out:
            return None
        if any(c == [] for c in out):
            return [[]]
        return out

    # -- records
    def flatten_atom(self, a: Atom, scope: tuple = ()) -> Atom:
        d = self.resolve_decl(a.name, scope)
        if d is None or not any(self.is_record_type(t) for _, t in d.attrs):
            return a
        if len(d.attrs) != len(a.args):
            raise TranslateError(f"arity mismatch for {a.name}")
        args = []
        for (an, at), arg in zip(d.attrs, a.args):
            if self.is_record_type(at):
                args.extend(self.flatten_record_arg(arg, at))
            else:
                args.append(arg)
        return Atom(a.name, args)

    def flatten_record_arg(self, arg: Any, t: str) -> list:
        fields = self.record_fields(t)
        if isinstance(arg, Wild):
            return [Wild() for _ in range(self.record_width(t))]
        if isinstance(arg, Rec):
            if len(arg.elems) != len(fields):
                raise TranslateError("record arity mismatch")
            out = []
            for (fn_, ft), el in zip(fields, arg.elems):
                if self.is_record_type(ft):
                    out.extend(self.flatten_record_arg(el, ft))
                else:
                    out.append(el)
            return out
        if isinstance(arg, Var):
            return [
                Var(self.flat_var(arg.name, fn_)) for fn_, _ in self.flat_attrs(fields)
            ]
        raise TranslateError(f"unsupported record argument {arg}")

    def decl_var_types(
        self, heads: list, body: list, scope: tuple, line: int, lit: bool = True
    ) -> dict:
        """Variable -> declared type, from the attributes of the (unflattened)
        atoms it occurs in, at any depth (record literals, disjunctions,
        aggregate bodies), propagated through equalities (with `lit` false,
        not to a variable equated to literals only).

        Souffle types each clause of a disjunction, and each aggregate's local
        variables, apart, so one name may stand for variables of different
        types there.  The passes reading this map type a name once for the
        whole rule, so a name given two base types is rejected.  A component
        type parameter may be instantiated with any type but a record, so it
        agrees with other types; it is kept (the casts of such a variable are
        then rejected) and spread through equalities."""
        types: dict = {}
        eqs: list = []

        def is_param(b):
            return isinstance(b, str) and b in self.tparams

        def note(name, t):
            old = types.setdefault(name, t)
            if old == t:
                return
            bo, bt = self.base_type(old), self.base_type(t)
            if bo == bt:
                return
            if (
                (is_param(bo) or is_param(bt))
                and isinstance(bo, str)
                and isinstance(bt, str)
            ):
                if is_param(bt) and not is_param(bo):
                    types[name] = t
                return
            raise TranslateError(
                f"line {line}: variable `{name}` is typed both {old} and {t} (one name "
                "in several disjuncts or aggregates); rename one of them"
            )

        def visit_arg(a, t):
            if isinstance(a, Var):
                note(a.name, t)
            elif isinstance(a, Rec) and self.is_record_type(t):
                fields = self.record_fields(t)
                if len(fields) == len(a.elems):
                    for (_, ft), el in zip(fields, a.elems):
                        visit_arg(el, ft)

        def visit(lit):
            atom = lit.atom if isinstance(lit, Neg) else lit
            if isinstance(atom, Atom):
                d = self.resolve_decl(atom.name, scope)
                if d is not None and len(d.attrs) == len(atom.args):
                    for (_, t), a in zip(
                        self.decl_attr_types(d, atom.name, scope), atom.args
                    ):
                        visit_arg(a, t)
            elif isinstance(lit, Cmp) and lit.op == "=":
                eqs.append(lit)
            elif isinstance(lit, Group):
                for c in lit.disj:
                    for l2 in c:
                        visit(l2)

            def probe(e):
                if isinstance(e, Agg):
                    for c in e.body:
                        for l2 in c:
                            visit(l2)
                return e

            if not isinstance(lit, Group):
                map_lit_exprs(lit, probe)

        for h in heads:
            visit(h)
        for c in body:
            for l1 in c:
                visit(l1)
        # types from atoms and variables first: a variable equated both to a
        # literal and to a typed variable has the latter's type
        for lits in (False, True) if lit else (False,):
            changed = True
            while changed:
                before = dict(types)
                for eq in eqs:
                    for x, e in ((eq.l, eq.r), (eq.r, eq.l)):
                        if (
                            isinstance(x, Var)
                            and x.name in types
                            and isinstance(e, Rec)
                        ):
                            visit_arg(e, types[x.name])
                        elif (
                            isinstance(x, Var)
                            and x.name in types
                            and isinstance(e, Var)
                        ):
                            note(e.name, types[x.name])
                        elif isinstance(x, Var) and x.name not in types:
                            t = self.expr_type(e, types, lits)
                            if t is not None:
                                types[x.name] = t
                # names are only added, or switched once to a type parameter
                changed = types != before
        return types

    def check_casts(self, lits: list, types: dict, line: int) -> None:
        """`as(e, T)` only relabels a value whose base type is T's; other
        casts reinterpret bits in Souffle, so they are rejected."""

        def probe(e):
            if isinstance(e, Cast):
                src = self.expr_type(e.e, types)
                try:
                    ok = src is not None and self.base_type(src) == self.base_type(e.ty)
                except TranslateError:
                    ok = False
                if not ok:
                    raise TranslateError(
                        f"line {line}: as(..., {e.ty}) is supported only between types "
                        "of the same base type"
                    )
            return e

        for lit in lits:
            deep_map_lit(lit, probe)

    def lower_record_eqs(self, body: list, types: dict, line: int) -> list:
        """`x = [a, b]` and `x = y` over records become equalities of the
        flattened fields (wildcard fields dropped)."""

        def rec_type(e):
            t = types.get(e.name) if isinstance(e, Var) else None
            return t if t is not None and self.is_record_type(t) else None

        def is_rec(e):
            return isinstance(e, Rec) or rec_type(e) is not None

        def agg_f(e):
            if isinstance(e, Agg):
                return Agg(e.kind, e.target, [lower(c) for c in e.body])
            return e

        def lower(conj):
            out = []
            for lit in conj:
                if isinstance(lit, Group):
                    out.append(Group([lower(c) for c in lit.disj]))
                elif isinstance(lit, Cmp) and (is_rec(lit.l) or is_rec(lit.r)):
                    if lit.op != "=":
                        raise TranslateError(
                            f"line {line}: record comparison `{lit.op}` is not supported"
                        )
                    t = rec_type(lit.l) or rec_type(lit.r)
                    if t is None:
                        raise TranslateError(
                            f"line {line}: cannot tell the record type of `{pl(lit)}`"
                        )
                    ls = self.flatten_record_arg(lit.l, t)
                    rs = self.flatten_record_arg(lit.r, t)
                    out += [
                        Cmp("=", x, y)
                        for x, y in zip(ls, rs)
                        if not isinstance(x, Wild) and not isinstance(y, Wild)
                    ]
                else:
                    out.append(map_lit_exprs(lit, agg_f))
            return out

        return [lower(c) for c in body]

    # -- self equalities
    def drop_self_eq(self, body: list) -> list:
        def keep(lit):
            return not (
                isinstance(lit, Cmp)
                and lit.op == "="
                and isinstance(lit.l, Var)
                and isinstance(lit.r, Var)
                and lit.l.name == lit.r.name
            )

        def fix(disj):
            out = []
            for conj in disj:
                nc = []
                for lit in conj:
                    if not keep(lit):
                        continue
                    if isinstance(lit, Group):
                        lit = Group(fix(lit.disj))
                    nc.append(lit)
                out.append(nc)
            return out

        return fix(body)

    # -- range
    def rewrite_ranges(self, body: list, scope: tuple, types: dict):
        """`i = range(lo, hi)` enumerates lo <= i < hi inside the clause, or
        lo >= i > hi when hi < lo (Souffle counts down), so in Souffle the
        clause fires in the same iteration as its other literals. When
        0 <= lo and hi < RANGE_TABLE an ascending range reads the
        precomputed `FlowlogRange(hi, i)` table (a lower stratum), which
        keeps that timing; other bounds, and descending ranges, fall back to
        a per-use recursive helper relation, which yields one element per
        iteration."""
        helpers: list = []
        new_body = []

        def is_range(lit):
            return (
                isinstance(lit, Cmp)
                and lit.op == "="
                and isinstance(lit.r, Call)
                and lit.r.name == "range"
                and isinstance(lit.l, Var)
            )

        work = [list(c) for c in body]
        while work:
            conj = work.pop(0)
            for k, lit in enumerate(conj):
                if (
                    isinstance(lit, Cmp)
                    and lit.op == "="
                    and isinstance(lit.l, Call)
                    and lit.l.name == "range"
                    and isinstance(lit.r, Var)
                ):
                    conj[k] = Cmp("=", lit.r, lit.l)
                if isinstance(lit, Group):
                    for c2 in lit.disj:
                        for l2 in c2:
                            if isinstance(l2, Cmp) and (
                                (isinstance(l2.r, Call) and l2.r.name == "range")
                                or (isinstance(l2.l, Call) and l2.l.name == "range")
                            ):
                                raise TranslateError(
                                    "range inside a disjunction is not supported"
                                )
            cands = [k for k, lit in enumerate(conj) if is_range(lit)]
            if not cands:
                new_body.append(conj)
                continue
            # one range at a time, preferring one whose bounds the other
            # literals ground; the rewritten clause goes back on the list
            g0 = grounded_vars([lit for lit in conj if not is_range(lit)])
            idx = next(
                (k for k in cands if expr_vars(conj[k].r, set()) <= g0), cands[0]
            )
            rlit = conj[idx]
            ivar = rlit.l.name
            rargs = rlit.r.args
            if len(rargs) != 2:
                raise TranslateError("range with a step is not supported")
            # The request rule keeps the literals that do not mention the
            # generated variable and whose variables are grounded by them.
            rest = [lit for k, lit in enumerate(conj) if k != idx]
            pre = [
                lit
                for lit in rest
                if ivar not in lit_vars(lit, set()) and not is_range(lit)
            ]
            while True:
                grounded = grounded_vars(pre)
                keep = [lit for lit in pre if lit_vars(lit, set()) <= grounded]
                if len(keep) == len(pre):
                    break
                pre = keep
            lo, hi = rargs
            # the helpers and the table are on number
            for x in (Var(ivar), lo, hi):
                t = self.expr_type(x, types)
                if t is not None and self.base_type(t) != NUMBER:
                    raise TranslateError(f"range over {t} is not supported")
            before, after = list(conj[:idx]), list(conj[idx + 1 :])
            in_table = [fold_cmp(">=", lo, Num(0)), fold_cmp("<", hi, Num(RANGE_TABLE))]
            ascending = fold_cmp("<", lo, hi) is not False
            descending = fold_cmp("<", hi, lo) is not False
            table = ascending and False not in in_table
            outs: list = []
            if table:
                self.need_range = True
                low = (
                    []
                    if fold_cmp("<=", lo, Num(0)) is True
                    else [Cmp(">=", Var(ivar), lo)]
                )
                guards = [c for c in in_table if c is not True]
                outs.append(
                    before + guards + [Atom(RANGE_REL, [hi, Var(ivar)])] + low + after
                )
            # The cases the table does not serve, as conditions on the
            # helper's (lo, hi): the seeds of its enumeration.
            L, H, I = Var("lo"), Var("hi"), Var("i")
            up: list = []
            if ascending and not table:
                up = [[Cmp("<", L, H)]]
            elif table:
                if in_table[0] is not True:
                    up.append([Cmp("<", L, H), Cmp("<", L, Num(0))])
                if in_table[1] is not True:
                    up.append([Cmp("<", L, H), Cmp(">=", H, Num(RANGE_TABLE))])
            down = [[Cmp("<", H, L)]] if descending else []
            if not up and not down:
                work[:0] = outs
                continue
            for bound in (lo, hi):
                if not expr_vars(bound, set()) <= grounded_vars(pre):
                    raise TranslateError(
                        "range bounds are not grounded independently of the generated variable"
                    )
            self.range_counter += 1
            rn = f"Range{self.range_counter}"
            req = f"{rn}_req"
            helpers.append(Raw(f".decl {req}(lo: {NUMBER}, hi: {NUMBER})"))
            helpers.append(Raw(f".decl {rn}(lo: {NUMBER}, hi: {NUMBER}, i: {NUMBER})"))
            helpers.append(Rule([Atom(req, [lo, hi])], [list(pre)] if pre else None))
            for seed in up + down:
                helpers.append(
                    Rule([Atom(rn, [L, H, L])], [[Atom(req, [L, H])] + seed])
                )
            if up:
                helpers.append(
                    Rule(
                        [Atom(rn, [L, H, Bin("+", I, Num(1))])],
                        [[Atom(rn, [L, H, I]), Cmp("<", Bin("+", I, Num(1)), H)]],
                    )
                )
            if down:
                helpers.append(
                    Rule(
                        [Atom(rn, [L, H, Bin("-", I, Num(1))])],
                        [[Atom(rn, [L, H, I]), Cmp(">", Bin("-", I, Num(1)), H)]],
                    )
                )
            outs.append(before + [Atom(rn, [lo, hi, Var(ivar)])] + after)
            work[:0] = outs
        return new_body, helpers

    def range_table(self) -> list:
        """`FlowlogRange(hi, i)` for 0 <= i < hi < RANGE_TABLE, built from
        RANGE_DIGITS facts by a cross product (no recursion)."""
        dig, nat = f"{RANGE_REL}Digit", f"{RANGE_REL}Nat"
        out: list = [
            Raw(f".decl {dig}(d: {NUMBER})"),
            Raw(f".decl {nat}(n: {NUMBER})"),
            Raw(f".decl {RANGE_REL}(hi: {NUMBER}, i: {NUMBER})"),
        ]
        out += [Rule([Atom(dig, [Num(d)])], None) for d in range(RANGE_DIGITS)]
        out.append(
            Rule(
                [
                    Atom(
                        nat, [Bin("+", Bin("*", Var("a"), Num(RANGE_DIGITS)), Var("b"))]
                    )
                ],
                [[Atom(dig, [Var("a")]), Atom(dig, [Var("b")])]],
            )
        )
        out.append(
            Rule(
                [Atom(RANGE_REL, [Var("hi"), Var("i")])],
                [
                    [
                        Atom(nat, [Var("hi")]),
                        Atom(nat, [Var("i")]),
                        Cmp("<", Var("i"), Var("hi")),
                    ]
                ],
            )
        )
        return out

    # -- aggregates
    def scope_aggregates(self, body: list, heads: list, line: int) -> list:
        """Spell out Souffle's aggregate scoping for FlowLog (see scope_agg).
        A conjunction combining aggregates and disjunctions is multiplied out
        first, as Souffle's parser does: a variable can be grounded outside
        an aggregate in one disjunct and not in another."""
        if any(has_agg(h) for h in heads):
            raise TranslateError(f"line {line}: aggregate in a rule head")
        head_vars: set = set()
        for h in heads:
            lit_vars(h, head_vars)
        out: list = []
        for conj in body:
            conjs = [conj]
            if any(isinstance(l, Group) for l in conj) and any(
                has_agg(l) for l in conj
            ):
                conjs = souffle_dnf([conj])
            for c in conjs:
                outer = set(head_vars)
                for l in c:
                    lit_vars(l, outer, include_agg=False)
                out.append(self.scope_conj(c, outer, line))
        return out

    def scope_conj(self, conj: list, outer: set, line: int) -> list:
        """Scope the outermost aggregates of a conjunction, given the
        variables `outer` grounded around them."""
        out: list = []
        for l in conj:
            extra: list = []
            l = map_lit_top(
                l,
                lambda e: map_outer_aggs(
                    e, lambda a: self.scope_agg(a, outer, line, extra)
                ),
            )
            out += extra + [l]
        return out

    def scope_agg(self, a: Agg, outer: set, line: int, extra: list) -> Any:
        """Souffle (2.5) injects into an aggregate the variables of its body
        that are grounded outside it (in the clause, or the bodies of the
        enclosing aggregates), except those of its target expression; all
        its other variables are local to it (UniqueAggregationVariables).
        FlowLog correlates an aggregate on every variable it shares with
        the rule, so the locals get fresh names.

        Souffle aggregates over the tuples of the body's atom, unless
        sx_materialized, when it aggregates over the distinct bindings of
        the body's named variables; FlowLog always does the latter, so the
        wildcards of a non-materialised body are named.

        Returns the expression replacing the aggregate; `extra` receives
        literals for the enclosing conjunction."""
        if len(a.body) != 1 or any(isinstance(l, Group) for l in a.body[0]):
            raise TranslateError(
                f"line {line}: disjunction inside an aggregate body is not supported"
            )
        if any(isinstance(l, Neg) for l in a.body[0]):
            raise TranslateError(
                f"line {line}: negation inside an aggregate body is not supported by FlowLog"
            )
        if a.target is not None and has_agg(a.target):
            raise TranslateError(
                f"line {line}: aggregate inside an aggregate target is not supported"
            )
        mat = sx_materialized(a)
        target_vars = expr_vars(a.target, set()) if a.target is not None else set()
        injected = (expr_vars(a, set()) & outer) - target_vars
        ren = {
            v: Var(self.fresh(f"{v}_ag"))
            for v in sorted(expr_vars(a, set()) - injected)
        }
        local = {v.name for v in ren.values()}
        target = None if a.target is None else subst_expr(a.target, ren)
        conj = [subst_lit(l, ren) for l in a.body[0]]
        if not mat:

            def name_wild(x):
                if isinstance(x, Wild):
                    x = Var(self.fresh("agg_w"))
                    local.add(x.name)
                return x

            conj = [
                (
                    Atom(l.name, [name_wild(x) for x in l.args])
                    if isinstance(l, Atom)
                    else l
                )
                for l in conj
            ]
        lifted: list = []
        for l in conj:
            if not isinstance(l, Atom):
                lifted.append(l)
                continue
            args = []
            for x in l.args:
                if not isinstance(x, (Var, Wild, Num, Str)):
                    v = Var(self.fresh("lift_t"))
                    local.add(v.name)
                    lifted.append(Cmp("=", v, x))
                    x = v
                args.append(x)
            lifted.append(Atom(l.name, args))
        conj = lifted
        inner: set = set(outer)
        for l in conj:
            lit_vars(l, inner, include_agg=False)
        if target is not None:
            expr_vars(target, inner, include_agg=False)
        conj = self.scope_conj(conj, inner, line)
        if target is not None and not expr_vars(target, set()) & local:
            # FlowLog rejects a target without a local variable; Souffle
            # adds it up once per match
            if a.kind != "sum":
                raise TranslateError(
                    f"line {line}: {a.kind} of an expression without local variables "
                    "is not supported"
                )
            n = Var(self.fresh("agg_n"))
            extra.append(Cmp("=", n, Agg("count", None, [conj])))
            return Bin("*", target, n)
        return Agg(a.kind, target, [conj])

    # -- atom argument lifting
    def lift_atom_args(self, body: list) -> list:
        """FlowLog atoms take variables and constants only: a computed
        argument becomes `t = expr` (scope_agg lifts those of aggregate
        bodies)."""

        def fix_conj(conj):
            out = []
            for lit in conj:
                if isinstance(lit, Group):
                    out.append(Group([fix_conj(c) for c in lit.disj]))
                    continue
                atom = (
                    lit.atom
                    if isinstance(lit, Neg)
                    else lit if isinstance(lit, Atom) else None
                )
                if atom is None:
                    out.append(lit)
                    continue
                pre = []
                args = []
                for a in atom.args:
                    if isinstance(a, (Var, Wild, Num, Str)):
                        args.append(a)
                    else:
                        v = Var(self.fresh("lift_t"))
                        pre.append(Cmp("=", v, a))
                        args.append(v)
                out.extend(pre)
                na = Atom(atom.name, args)
                out.append(Neg(na) if isinstance(lit, Neg) else na)
            return out

        return [fix_conj(c) for c in body]

    # -- choice heads
    def choice_head(self, h: Atom, scope: tuple, line: int, sub: Any = None) -> Atom:
        base = h.name.split(".")[-1]
        d = self.resolve_decl(h.name, scope)
        if d is None or not d.choice or not self.is_minchoice(base):
            return h
        attrs = self.flat_attrs(d.attrs)
        keys = self.choice_keys(d)
        kargs, dargs = [], []
        for (a, t), e in zip(attrs, h.args):
            if a in keys:
                kargs.append(e)
            else:
                if self.base_type(t) in NUMERIC_PRIMS:
                    e = Call("to_string", [e])
                dargs.append(e)
        prio = self.choice_prio.get((line, sub, id(d)))
        if prio is None:
            raise TranslateError(f"line {line}: no min-choice priority for {h.name}")
        prefix = h.name[: len(h.name) - len(base)]
        return Atom(
            f"{prefix}{base}_choice",
            kargs + [Call(f"choice_key{len(dargs)}", [Num(prio)] + dargs)],
        )


# ============================================================================
# Printer
# ============================================================================

# FlowLog's operator ladder (flowlog-parser ArithmeticOperator::precedence);
# runs of one level group left to right, except ^.
FL_PREC = {
    "bor": 0,
    "bxor": 1,
    "band": 2,
    "bshl": 3,
    "bshr": 3,
    "bshru": 3,
    "+": 4,
    "-": 4,
    "*": 5,
    "/": 5,
    "%": 5,
    "^": 6,
}
FL_RIGHT_ASSOC = {"^"}


def pe(e: Any, operand: tuple[int, bool] | None = None) -> str:
    """Prints an expression; `operand` = (precedence of the enclosing binary
    operator, whether an operand of equal precedence needs parentheses)."""
    if isinstance(e, Var):
        return e.name
    if isinstance(e, Wild):
        return "_"
    if isinstance(e, Num):
        s = str(e.val)
        return f"({s})" if operand is not None and e.val < 0 else s
    if isinstance(e, Str):
        return fl_string(e.val)
    if isinstance(e, Call):
        return f"{e.name}({', '.join(pe(a) for a in e.args)})"
    if isinstance(e, Cast):
        # FlowLog's `as` takes a factor: an arithmetic operand is parenthesized
        return f"as({'(' + pe(e.e) + ')' if isinstance(e.e, Bin) else pe(e.e)}, {e.ty})"
    if isinstance(e, Bin):
        p = FL_PREC[e.op]
        ra = e.op in FL_RIGHT_ASSOC
        s = f"{pe(e.l, (p, ra))} {e.op} {pe(e.r, (p, not ra))}"
        if operand is not None and (p < operand[0] or (p == operand[0] and operand[1])):
            return f"({s})"
        return s
    if isinstance(e, Agg):
        body = ", ".join(pl(l) for l in e.body[0])
        if e.kind == "count":
            s = f"count : {{ {body} }}"
        else:
            s = f"{e.kind} {pe(e.target, (len(FL_PREC), True))} : {{ {body} }}"
        return f"({s})" if operand is not None else s
    raise TranslateError(f"cannot print expression {e}")


def pl(lit: Any) -> str:
    if isinstance(lit, Atom):
        return f"{lit.name}({', '.join(pe(a) for a in lit.args)})"
    if isinstance(lit, Neg):
        return "!" + pl(lit.atom)
    if isinstance(lit, Cmp):
        return f"{pe(lit.l)} {lit.op} {pe(lit.r)}"
    if isinstance(lit, Pred):
        return (
            "!" if lit.neg else ""
        ) + f"{lit.name}({', '.join(pe(a) for a in lit.args)})"
    if isinstance(lit, Group):
        return (
            "(" + "; ".join(", ".join(pl(l) for l in conj) for conj in lit.disj) + ")"
        )
    raise TranslateError(f"cannot print literal {lit}")


def print_items(items: list, indent: str = "") -> list[str]:
    lines: list[str] = []
    for it in items:
        if isinstance(it, Raw):
            lines.append(indent + it.text)
        elif isinstance(it, Comp):
            hdr = f".comp {it.name}"
            if it.tparams:
                hdr += "<" + ", ".join(it.tparams) + ">"
            if it.supers:
                hdr += " : " + ", ".join(
                    s + (f"<{', '.join(a)}>" if a else "") for s, a in it.supers
                )
            lines.append(indent + hdr + " {")
            lines.extend(print_items(it.items, indent + "  "))
            lines.append(indent + "}")
        elif isinstance(it, Init):
            ta = f"<{', '.join(it.targs)}>" if it.targs else ""
            lines.append(f"{indent}.init {it.name} = {it.comp}{ta}")
        elif isinstance(it, Override):
            lines.append(f"{indent}.override {it.name}")
        elif isinstance(it, Rule):
            heads = ", ".join(pl(h) for h in it.heads)
            if it.body is None:
                lines.append(f"{indent}{heads}.")
            else:
                body = (";\n" + indent + "    ").join(
                    ", ".join(pl(l) for l in conj) for conj in it.body
                )
                lines.append(f"{indent}{heads} :-\n{indent}    {body}.")
                if isinstance(it.plan, tuple) and it.plan:
                    lines.append(f"{indent}.plan ({', '.join(map(str, it.plan))})")
        else:
            raise TranslateError(f"cannot print item {it}")
    return lines


# ============================================================================
# CLI
# ============================================================================


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument(
        "--policy",
        help='JSON policy file: {"choice": {RELATION: "drop" | "minchoice" | '
        '{"min": COLUMN}}}; a choice-domain relation it does not list is encoded as minchoice',
    )
    ap.add_argument(
        "--schema", help="write a JSON schema manifest (record columns, sqlite I/O)"
    )
    ap.add_argument(
        "--extra", action="append", default=[], help="FlowLog text appended verbatim"
    )
    args = ap.parse_args()

    policy = {}
    if args.policy:
        with open(args.policy) as fh:
            policy = json.load(fh)
    with open(args.input) as fh:
        src = fh.read()
    try:
        items = Parser(lex(src)).parse_program()
        tr = Translator(items, policy)
        out_items = tr.run()
        text = "\n".join(print_items(out_items)) + "\n"
    except (TranslateError, ParseFail) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for extra in args.extra:
        with open(extra) as fh:
            text += "\n" + fh.read()
    header = f"// Generated by souffle2flowlog.py from {args.input}\n"
    with open(args.output, "w") as fh:
        fh.write(header + text)
    for w in tr.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if args.schema:
        with open(args.schema, "w") as fh:
            json.dump(tr.schema, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
