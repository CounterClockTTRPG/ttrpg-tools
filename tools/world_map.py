"""World-map DSL — parser, compiler, MCP tools.

Source-of-truth DSL files live at campaigns/<n>/maps/<slug>.map.
The compiler emits per-view GeoJSON (cached at <slug>.<view>.geojson).

Grammar overview (block-based, Structurizr-style):

    !include path/to/other.map         # preprocessor directive (any depth, any block)

    workspace <ident> {
        styles { <style-rules> }       # optional workspace-level defaults
        model {
            kingdom <ident> { <props> }
            city <ident> { <props> contains { <child-features> } }
            road <ident> { from <ident>; to <ident>; via <coord>... }
            river <ident> { points <coord>... }
            terrain <ident> { polygon <coord>...; biome <ident> }
            poi <ident> { at <coord>; <props> }
            dungeon <ident> { in <ident-path>; room <ident>{...}; passage{...} }
            ...
        }
        views {
            view <ident> { scope <ident>; include <kinds>; units <ident>; styles{...} }
        }
    }

Generic properties on any feature: description "...", tags [a, b, c], doc <path>.
Style selectors: kind | kind.prop=value | kind.prop>value | [tag].
"""
from __future__ import annotations

import heapq
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import _campaign as _c

BASE_DIR = Path(__file__).parent.parent
_MAX_INCLUDE_DEPTH = 8


# ============================================================================
# AST node types
# ============================================================================

@dataclass
class Coord:
    x: float
    y: float

    def as_pair(self) -> list[float]:
        return [self.x, self.y]

    def __repr__(self) -> str:
        return f"({self.x},{self.y})"


@dataclass
class Property:
    name: str                 # e.g. 'at', 'pop', 'tags', 'description'
    values: list[Any]         # ints, floats, str, Coord, IdentPath, list[str] for tags
    line: int = 0


@dataclass
class StyleRule:
    """A single `selector { attrs }` rule.

    selector encodes one of:
      kind only          ─ {'kind': 'city'}
      kind + property    ─ {'kind': 'terrain', 'prop': 'biome', 'op': '=', 'value': 'forest'}
      tag                ─ {'tag': 'walled'}
    """
    selector: dict
    attrs: dict[str, Any]
    line: int = 0


@dataclass
class IdentPath:
    """Dotted identifier reference, e.g. orlane.temple-merikka."""
    parts: list[str]

    @property
    def text(self) -> str:
        return ".".join(self.parts)

    def __repr__(self) -> str:
        return self.text


@dataclass
class Feature:
    kind: str                       # 'city', 'road', 'kingdom', 'building', ...
    name: str                       # identifier
    properties: dict[str, Property] = field(default_factory=dict)
    children: list["Feature"] = field(default_factory=list)  # contains/dungeon body
    line: int = 0

    def get(self, prop: str, default=None):
        p = self.properties.get(prop)
        return p.values if p is not None else default

    def get_one(self, prop: str, default=None):
        p = self.properties.get(prop)
        if p is None or not p.values:
            return default
        return p.values[0]


@dataclass
class View:
    name: str
    properties: dict[str, Property] = field(default_factory=dict)
    styles: list[StyleRule] = field(default_factory=list)
    line: int = 0


@dataclass
class Workspace:
    name: str
    styles: list[StyleRule] = field(default_factory=list)
    model: list[Feature] = field(default_factory=list)
    views: list[View] = field(default_factory=list)
    manifest: list[Path] = field(default_factory=list)  # all source files (for cache invalidation)


# ============================================================================
# Preprocessor — resolve !include directives anywhere a statement is valid
# ============================================================================

def _preprocess(text: str, base_dir: Path, _seen: set | None = None,
                _depth: int = 0, _manifest: list | None = None) -> tuple[str, list[Path]]:
    """Inline !include directives recursively.

    !include is processed line-by-line BEFORE tokenization, so it works at
    top level, inside model {}, contains {}, views {}, etc. Cycles are
    rejected; depth is bounded.

    Returns (flat_text, manifest_of_source_paths).
    """
    if _depth > _MAX_INCLUDE_DEPTH:
        raise SyntaxError(f"!include nesting depth exceeds {_MAX_INCLUDE_DEPTH}")
    if _seen is None:
        _seen = set()
    if _manifest is None:
        _manifest = []

    out: list[str] = []
    for raw in text.splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("!include"):
            parts = stripped.split(None, 1)
            if len(parts) < 2:
                raise SyntaxError("!include requires a path")
            target = parts[1].strip().strip('"')
            inc_path = (base_dir / target).resolve()
            key = str(inc_path)
            if key in _seen:
                raise SyntaxError(f"!include cycle detected at: {inc_path}")
            if not inc_path.exists():
                raise SyntaxError(f"!include file not found: {inc_path}")
            _seen.add(key)
            _manifest.append(inc_path)
            inc_text = inc_path.read_text(encoding="utf-8")
            inc_resolved, _ = _preprocess(inc_text, inc_path.parent, _seen, _depth + 1, _manifest)
            out.append(inc_resolved)
            _seen.discard(key)
        else:
            out.append(raw)
    return "\n".join(out), _manifest


# ============================================================================
# Tokenizer
# ============================================================================

@dataclass
class Token:
    type: str
    value: Any
    line: int

    def __repr__(self) -> str:
        return f"{self.type}({self.value!r}@{self.line})"


# Order matters — STRING before HEX/COMMENT (so '#' inside strings is text);
# HEX before COMMENT (so #abc is a colour, # foo is a comment);
# COORD before NUMBER (so '412,308' is one token).
_TOKEN_RE = re.compile(
    r"""
    (?P<STRING>   "(?:[^"\\\n]|\\.)*" )                       # "double-quoted"
    | (?P<HEX>      \#[0-9a-fA-F]{3,8}\b )                    # #abc / #aabbcc
    | (?P<COMMENT>  \#[^\n]* )                                 # # to end of line
    | (?P<NEWLINE>  \n )
    | (?P<WS>       [ \t\r]+ )
    | (?P<COORD>    -?\d+(?:\.\d+)?,-?\d+(?:\.\d+)? )         # 412,308 or -1.5,2
    | (?P<NUMBER>   -?\d+(?:\.\d+)? )
    | (?P<OP>       != | >= | <= | = | > | < )
    | (?P<LBRACE>   \{ )
    | (?P<RBRACE>   \} )
    | (?P<LBRACKET> \[ )
    | (?P<RBRACKET> \] )
    | (?P<COMMA>    , )
    | (?P<SEMI>     ; )
    | (?P<IDENT>    [a-zA-Z_][\w./-]* )
    """,
    re.VERBOSE,
)


def _tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    line = 1
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            ch = text[pos]
            raise SyntaxError(f"line {line}: unexpected character {ch!r}")
        kind = m.lastgroup
        raw = m.group()
        pos = m.end()
        if kind == "WS":
            continue
        if kind == "COMMENT":
            continue
        if kind == "NEWLINE":
            tokens.append(Token("NEWLINE", "\n", line))
            line += 1
            continue
        if kind == "STRING":
            # strip quotes; decode \" and \\
            s = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            tokens.append(Token("STRING", s, line))
        elif kind == "HEX":
            tokens.append(Token("HEX", raw.lower(), line))
        elif kind == "NUMBER":
            v = float(raw) if "." in raw else int(raw)
            tokens.append(Token("NUMBER", v, line))
        elif kind == "COORD":
            xs, ys = raw.split(",")
            x = float(xs) if "." in xs else int(xs)
            y = float(ys) if "." in ys else int(ys)
            tokens.append(Token("COORD", Coord(x, y), line))
        elif kind == "IDENT":
            tokens.append(Token("IDENT", raw, line))
        elif kind == "OP":
            tokens.append(Token("OP", raw, line))
        else:
            tokens.append(Token(kind, raw, line))
    tokens.append(Token("EOF", None, line))
    return tokens


# ============================================================================
# Parser — recursive descent
# ============================================================================

# Properties whose value is a flat list of coords until terminator.
_COORD_LIST_PROPS = {"polygon", "points", "via"}
# Reserved selector property name suffix in style rule keys (for parsing convenience).
_VALID_OPS = {"=", "!=", ">", "<", ">=", "<="}


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    # --- low-level helpers -------------------------------------------------
    def peek(self, offset: int = 0) -> Token:
        i = self.pos + offset
        if i < len(self.tokens):
            return self.tokens[i]
        return self.tokens[-1]

    def at(self, type_: str, value: Any = None) -> bool:
        t = self.peek()
        if t.type != type_:
            return False
        if value is not None and t.value != value:
            return False
        return True

    def consume(self, type_: str, value: Any = None) -> Token:
        t = self.peek()
        if t.type != type_:
            raise SyntaxError(f"line {t.line}: expected {type_}, got {t.type} {t.value!r}")
        if value is not None and t.value != value:
            raise SyntaxError(f"line {t.line}: expected {value!r}, got {t.value!r}")
        self.pos += 1
        return t

    def skip_term(self) -> None:
        """Skip statement terminators: NEWLINE or SEMI."""
        while self.peek().type in ("NEWLINE", "SEMI"):
            self.pos += 1

    # --- top-level ---------------------------------------------------------
    def parse(self) -> Workspace:
        self.skip_term()
        self.consume("IDENT", "workspace")
        name = self.consume("IDENT").value
        self.consume("LBRACE")
        ws = Workspace(name=name)
        while True:
            self.skip_term()
            t = self.peek()
            if t.type == "RBRACE":
                break
            if t.type != "IDENT":
                raise SyntaxError(f"line {t.line}: expected section keyword, got {t.type} {t.value!r}")
            section = t.value
            if section == "styles":
                ws.styles.extend(self.parse_styles_block())
            elif section == "model":
                ws.model.extend(self.parse_model_block())
            elif section == "views":
                ws.views.extend(self.parse_views_block())
            else:
                raise SyntaxError(f"line {t.line}: unknown workspace section {section!r}")
        self.consume("RBRACE")
        self.skip_term()
        return ws

    # --- styles ------------------------------------------------------------
    def parse_styles_block(self) -> list[StyleRule]:
        self.consume("IDENT", "styles")
        self.consume("LBRACE")
        rules: list[StyleRule] = []
        while True:
            self.skip_term()
            if self.at("RBRACE"):
                break
            rules.append(self.parse_style_rule())
        self.consume("RBRACE")
        return rules

    def parse_style_rule(self) -> StyleRule:
        """Parse one selector { attr value; ... } rule."""
        t = self.peek()
        line = t.line
        # selector forms:
        #   IDENT                              kind only
        #   IDENT '.' IDENT OP value           kind + prop match
        #   '[' IDENT ']'                      tag
        if self.at("LBRACKET"):
            self.consume("LBRACKET")
            tag = self.consume("IDENT").value
            self.consume("RBRACKET")
            selector = {"tag": tag}
        else:
            kind_tok = self.consume("IDENT")
            kind_full = kind_tok.value  # may be 'terrain.biome' (dotted ident) or 'city'
            if "." in kind_full:
                kind, prop = kind_full.split(".", 1)
                # require an OP next
                op_tok = self.consume("OP")
                val = self._parse_scalar()
                selector = {"kind": kind, "prop": prop, "op": op_tok.value, "value": val}
            else:
                selector = {"kind": kind_full}
        # body
        self.consume("LBRACE")
        attrs: dict[str, Any] = {}
        while True:
            self.skip_term()
            if self.at("RBRACE"):
                break
            key = self.consume("IDENT").value
            val = self._parse_scalar()
            attrs[key] = val
            # consume optional terminator handled by skip_term at loop top
        self.consume("RBRACE")
        return StyleRule(selector=selector, attrs=attrs, line=line)

    def _parse_scalar(self) -> Any:
        t = self.peek()
        if t.type in ("NUMBER", "STRING", "HEX"):
            self.pos += 1
            return t.value
        if t.type == "IDENT":
            self.pos += 1
            return t.value
        raise SyntaxError(f"line {t.line}: expected scalar, got {t.type} {t.value!r}")

    # --- model -------------------------------------------------------------
    def parse_model_block(self) -> list[Feature]:
        self.consume("IDENT", "model")
        self.consume("LBRACE")
        features: list[Feature] = []
        while True:
            self.skip_term()
            if self.at("RBRACE"):
                break
            features.append(self.parse_feature())
        self.consume("RBRACE")
        return features

    def parse_feature(self) -> Feature:
        kind_tok = self.consume("IDENT")
        kind = kind_tok.value
        # `passage` is the one feature that doesn't take a name (anonymous).
        if kind == "passage":
            name = ""
        else:
            name = self.consume("IDENT").value
        feat = Feature(kind=kind, name=name, line=kind_tok.line)
        self.consume("LBRACE")
        while True:
            self.skip_term()
            if self.at("RBRACE"):
                break
            # Either a nested `contains { ... }` block, or a property.
            t = self.peek()
            if t.type == "IDENT" and t.value == "contains":
                self.pos += 1
                self.consume("LBRACE")
                while True:
                    self.skip_term()
                    if self.at("RBRACE"):
                        break
                    feat.children.append(self.parse_feature())
                self.consume("RBRACE")
                continue
            # Otherwise a nested feature (rooms inside dungeon) OR a property.
            # Heuristic: known feature kinds (room, building, street, passage) → child
            # Anything else → property.
            if t.type == "IDENT" and t.value in {"room", "building", "street", "passage"}:
                feat.children.append(self.parse_feature())
                continue
            feat.properties.update(self._parse_property_into({}))
        self.consume("RBRACE")
        return feat

    # --- properties --------------------------------------------------------
    def _parse_property_into(self, dst: dict[str, Property]) -> dict[str, Property]:
        """Consume one property and add it to dst. Returns dst for convenience."""
        name_tok = self.consume("IDENT")
        name = name_tok.value
        line = name_tok.line
        values: list[Any] = []
        if name in _COORD_LIST_PROPS:
            # Read coords until terminator/brace.
            while self.peek().type in ("COORD", "NUMBER"):
                # NUMBER alone is invalid in a coord list — coerce to error
                if self.peek().type == "NUMBER":
                    raise SyntaxError(
                        f"line {self.peek().line}: '{name}' expects coord pairs (x,y), "
                        f"got bare number {self.peek().value}"
                    )
                values.append(self.consume("COORD").value)
        elif name == "tags":
            self.consume("LBRACKET")
            while True:
                if self.at("RBRACKET"):
                    break
                v = self.consume("IDENT").value
                values.append(v)
                if self.at("COMMA"):
                    self.pos += 1
            self.consume("RBRACKET")
        elif name == "include":
            # Comma-separated list of plural kind names: include kingdoms, cities, roads
            while True:
                t = self.peek()
                if t.type in ("NEWLINE", "SEMI", "RBRACE", "EOF"):
                    break
                if t.type == "COMMA":
                    self.pos += 1
                    continue
                if t.type == "IDENT":
                    self.pos += 1
                    values.append(t.value)
                else:
                    break
        else:
            # All other properties take exactly ONE value.
            # This makes `at 0,0 size 4,4` parse as two properties (`at` then `size`)
            # without requiring a semicolon between them.
            t = self.peek()
            if t.type in ("NEWLINE", "SEMI", "RBRACE", "EOF"):
                pass  # property with no value (e.g., `walled` as a flag — rare but allowed)
            elif t.type == "COORD":
                self.pos += 1
                values.append(t.value)
            elif t.type == "NUMBER":
                self.pos += 1
                values.append(t.value)
            elif t.type == "STRING":
                self.pos += 1
                values.append(t.value)
            elif t.type == "HEX":
                self.pos += 1
                values.append(t.value)
            elif t.type == "IDENT":
                self.pos += 1
                if "." in t.value:
                    values.append(IdentPath(parts=t.value.split(".")))
                else:
                    values.append(t.value)
            else:
                raise SyntaxError(
                    f"line {t.line}: property {name!r}: unexpected token {t.type} {t.value!r}"
                )
        dst[name] = Property(name=name, values=values, line=line)
        return dst

    # --- views -------------------------------------------------------------
    def parse_views_block(self) -> list[View]:
        self.consume("IDENT", "views")
        self.consume("LBRACE")
        views: list[View] = []
        while True:
            self.skip_term()
            if self.at("RBRACE"):
                break
            views.append(self.parse_view())
        self.consume("RBRACE")
        return views

    def parse_view(self) -> View:
        view_tok = self.consume("IDENT", "view")
        name = self.consume("IDENT").value
        self.consume("LBRACE")
        v = View(name=name, line=view_tok.line)
        while True:
            self.skip_term()
            if self.at("RBRACE"):
                break
            t = self.peek()
            if t.type == "IDENT" and t.value == "styles":
                v.styles.extend(self.parse_styles_block())
                continue
            self._parse_property_into(v.properties)
        self.consume("RBRACE")
        return v


# ============================================================================
# Reference resolution & validation (second pass)
# ============================================================================

# Known feature kinds. The kind set is closed; new kinds need code support.
_KNOWN_KINDS = {
    "kingdom", "city", "road", "river", "lake", "terrain", "poi",
    "building", "street", "dungeon", "room", "passage",
}


def _validate(ws: Workspace) -> None:
    """Second-pass: build identifier index, validate references."""
    index: dict[str, Feature] = {}

    def walk(features: list[Feature], parent_path: str = "") -> None:
        for f in features:
            if f.kind not in _KNOWN_KINDS:
                raise SyntaxError(f"line {f.line}: unknown feature kind {f.kind!r}")
            if f.name:
                key = f.name if not parent_path else f"{parent_path}.{f.name}"
                if key in index:
                    raise SyntaxError(
                        f"line {f.line}: duplicate identifier {key!r} (already at line {index[key].line})"
                    )
                index[key] = f
            walk(f.children, f.name if not parent_path else f"{parent_path}.{f.name}")

    walk(ws.model)

    # Validate references on roads (from/to), `in` on cities/buildings/dungeons, etc.
    # Resolution is scoped: try the parent's scope first, then walk up to top.
    def resolve_ref(name: str | IdentPath, scope_path: str, line: int) -> Feature:
        text = name.text if isinstance(name, IdentPath) else name
        # Try fully-qualified (already dotted) first.
        if text in index:
            return index[text]
        # Try in the current scope, then progressively wider scopes.
        scope = scope_path
        while scope:
            candidate = f"{scope}.{text}"
            if candidate in index:
                return index[candidate]
            scope = scope.rsplit(".", 1)[0] if "." in scope else ""
        raise SyntaxError(f"line {line}: unresolved reference {text!r}")

    def check_refs(features: list[Feature], scope_path: str = "") -> None:
        for f in features:
            for prop_name in ("from", "to", "in"):
                p = f.properties.get(prop_name)
                if p and p.values:
                    resolve_ref(p.values[0], scope_path, p.line)
            child_scope = f.name if not scope_path else f"{scope_path}.{f.name}"
            check_refs(f.children, child_scope)

    check_refs(ws.model)

    # Views reference scope by ident; validate.
    for v in ws.views:
        scope = v.properties.get("scope")
        if scope and scope.values:
            target = scope.values[0]
            text = target.text if isinstance(target, IdentPath) else target
            if text != ws.name and text not in index:
                raise SyntaxError(
                    f"line {v.line}: view {v.name!r} scope {text!r} does not match workspace or any feature"
                )

    ws._index = index  # stash for compiler use


# ============================================================================
# Top-level entry: parse source text → validated Workspace
# ============================================================================

def parse_dsl(text: str, base_dir: Path | None = None, root_path: Path | None = None) -> Workspace:
    """Parse a DSL source string into a validated Workspace.

    base_dir is used as the resolution root for !include directives
    (defaults to cwd). root_path, if given, is added to the manifest as
    the originating file.
    """
    base = base_dir or Path.cwd()
    flat, manifest = _preprocess(text, base)
    tokens = _tokenize(flat)
    ws = _Parser(tokens).parse()
    if root_path is not None:
        ws.manifest = [Path(root_path).resolve()] + manifest
    else:
        ws.manifest = manifest
    _validate(ws)
    return ws


def parse_file(path: Path) -> Workspace:
    p = Path(path).resolve()
    return parse_dsl(p.read_text(encoding="utf-8"), base_dir=p.parent, root_path=p)


# ============================================================================
# Style cascade — built-in defaults, workspace styles, view-level overrides
# ============================================================================

_BUILTIN_STYLES: list[StyleRule] = [
    StyleRule({"kind": "kingdom"},  {"color": "#888888", "fill-opacity": 0.15, "stroke": "#444", "stroke-width": 1}),
    StyleRule({"kind": "terrain"},  {"color": "#aaaaaa", "fill-opacity": 0.3,  "stroke": "#666", "stroke-width": 1}),
    StyleRule({"kind": "terrain", "prop": "biome", "op": "=", "value": "marsh"},
              {"color": "#4a6b3a", "fill-opacity": 0.4}),
    StyleRule({"kind": "terrain", "prop": "biome", "op": "=", "value": "forest"},
              {"color": "#2d5a2d", "fill-opacity": 0.5}),
    StyleRule({"kind": "terrain", "prop": "biome", "op": "=", "value": "hills"},
              {"color": "#8a7350", "fill-opacity": 0.3}),
    StyleRule({"kind": "terrain", "prop": "biome", "op": "=", "value": "desert"},
              {"color": "#d4a25a", "fill-opacity": 0.4}),
    StyleRule({"kind": "terrain", "prop": "biome", "op": "=", "value": "mountain"},
              {"color": "#6e6e6e", "fill-opacity": 0.5}),
    StyleRule({"kind": "terrain", "prop": "biome", "op": "=", "value": "plains"},
              {"color": "#c9b870", "fill-opacity": 0.3}),
    StyleRule({"kind": "terrain", "prop": "biome", "op": "=", "value": "tundra"},
              {"color": "#dcdcd2", "fill-opacity": 0.5}),
    StyleRule({"kind": "terrain", "prop": "biome", "op": "=", "value": "jungle"},
              {"color": "#2d5a3a", "fill-opacity": 0.5}),
    StyleRule({"kind": "lake"},     {"color": "#3a6da3", "fill-opacity": 0.5, "stroke": "#1a4d83", "stroke-width": 1}),
    StyleRule({"kind": "river"},    {"stroke": "#3a6da3", "stroke-width": 2}),
    StyleRule({"kind": "road"},     {"stroke": "#8a6b3a", "stroke-width": 2}),
    StyleRule({"kind": "city"},     {"marker": "circle",  "color": "#222222", "marker-size": 6}),
    StyleRule({"kind": "poi"},      {"marker": "pin",     "color": "#bb0000", "marker-size": 8}),
    StyleRule({"kind": "building"}, {"color": "#7B5C3A",  "fill-opacity": 0.6, "stroke": "#3a2a1a", "stroke-width": 1}),
    StyleRule({"kind": "street"},   {"stroke": "#aaaaaa", "stroke-width": 2}),
    StyleRule({"kind": "room"},     {"color": "#d8d4c8",  "fill-opacity": 0.7, "stroke": "#444", "stroke-width": 1}),
    StyleRule({"kind": "passage"},  {"stroke": "#666666", "stroke-width": 2}),
]


def _selector_matches(selector: dict, feat: Feature) -> bool:
    """True if the rule's selector matches this feature."""
    if "tag" in selector:
        tags = feat.get("tags") or []
        return selector["tag"] in tags
    if selector.get("kind") != feat.kind:
        return False
    if "prop" in selector:
        prop_val = feat.get_one(selector["prop"])
        if prop_val is None:
            return False
        op = selector["op"]
        target = selector["value"]
        try:
            if op == "=":   return prop_val == target
            if op == "!=":  return prop_val != target
            if op == ">":   return float(prop_val) > float(target)
            if op == "<":   return float(prop_val) < float(target)
            if op == ">=":  return float(prop_val) >= float(target)
            if op == "<=":  return float(prop_val) <= float(target)
        except (TypeError, ValueError):
            return False
    return True


def _resolve_styles(feat: Feature, view: View, ws: Workspace) -> dict:
    """Cascade builtin → workspace → view style rules. Last match wins per attr."""
    style: dict = {}
    for layer in (_BUILTIN_STYLES, ws.styles, view.styles):
        for rule in layer:
            if _selector_matches(rule.selector, feat):
                style.update(rule.attrs)
    return style


# ============================================================================
# View compiler — emit GeoJSON FeatureCollection
# ============================================================================

# Map plural-form `include` keywords → singular feature kinds.
_INCLUDE_PLURAL_TO_KIND = {
    "kingdoms":  "kingdom",
    "cities":    "city",
    "roads":     "road",
    "rivers":    "river",
    "lakes":     "lake",
    "terrain":   "terrain",
    "poi":       "poi",
    "buildings": "building",
    "streets":   "street",
    "dungeons":  "dungeon",
    "rooms":     "room",
    "passages":  "passage",
}

# Geometry-source properties — omitted from output `properties` to avoid duplication.
_GEOMETRY_PROPS = {"at", "size", "polygon", "points", "from", "to", "via"}


def _ident_text(v: Any) -> str:
    return v.text if isinstance(v, IdentPath) else str(v)


def _coord_pair(c: Coord) -> list[float]:
    return [c.x, c.y]


def _polygon_ring(points: list[Coord]) -> list[list[float]]:
    """Close a polygon ring (GeoJSON requires first==last)."""
    ring = [[p.x, p.y] for p in points]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _edge_seed(a: list, b: list) -> int:
    """Order-independent integer seed for an edge endpoint pair.

    Two polygons that share an edge will compute the same seed regardless
    of which side they approach it from, so the perpendicular jitter
    applied during roughening is identical and the polygons stay
    connected (no gaps appearing along their shared boundary)."""
    import hashlib
    a, b = sorted([(round(a[0], 4), round(a[1], 4)),
                   (round(b[0], 4), round(b[1], 4))])
    return int(hashlib.md5(f"{a}|{b}".encode()).hexdigest()[:8], 16)


def _roughen_ring(ring: list[list[float]], intensity: float,
                  segments_per_edge: int = 4) -> list[list[float]]:
    """Subdivide each polygon edge and jitter the inserted midpoints
    perpendicular to the edge by ``intensity * edge_length``.

    Original vertices are preserved (so adjacent polygons sharing those
    vertices stay aligned). Each inserted midpoint is computed against
    a canonical (sorted) edge orientation and seeded on the same
    canonical edge, so two polygons that share an edge — even when they
    traverse it in opposite directions — produce identical midpoint
    coordinates and remain visually connected.

    A natural-looking default for hand-drawn-feeling terrain is around
    0.03–0.06; values above ~0.10 start looking cartoonish."""
    import random
    if intensity <= 0 or len(ring) < 4:
        return ring
    out: list[list[float]] = []
    for i in range(len(ring) - 1):  # last == first (closed); iterate edges
        a, b = ring[i], ring[i + 1]
        out.append(list(a))

        # Canonical edge — same regardless of polygon traversal direction.
        ca, cb = sorted([(round(a[0], 4), round(a[1], 4)),
                         (round(b[0], 4), round(b[1], 4))])
        cdx, cdy = cb[0] - ca[0], cb[1] - ca[1]
        edge_len = (cdx * cdx + cdy * cdy) ** 0.5
        if edge_len < 1e-6:
            continue
        # Canonical perpendicular (rotated +90 from canonical direction).
        cpx, cpy = -cdy / edge_len, cdx / edge_len
        max_jitter = edge_len * intensity
        rng = random.Random(_edge_seed(a, b))

        # Compute midpoints along the canonical edge, then jitter.
        midpoints = []
        for s in range(1, segments_per_edge):
            t = s / segments_per_edge
            mx = ca[0] + cdx * t
            my = ca[1] + cdy * t
            j = (rng.random() * 2 - 1) * max_jitter
            midpoints.append([mx + cpx * j, my + cpy * j])

        # If this polygon traverses the edge in reverse-canonical order,
        # the midpoints need to be inserted reversed so they appear in
        # the polygon's own traversal direction. The COORDINATES are
        # identical to the other polygon's — just listed back-to-front.
        forward = ((round(a[0], 4), round(a[1], 4)) == ca)
        if not forward:
            midpoints.reverse()
        out.extend(midpoints)

    out.append(list(out[0]))   # close ring
    return out


def _rect_from_at_size(at: Coord, size: Coord) -> list[list[float]]:
    """Axis-aligned rectangle anchored at `at`, width=size.x, height=size.y."""
    x, y, w, h = at.x, at.y, size.x, size.y
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]


def _room_center(room: Feature) -> list[float] | None:
    """Return the centroid of a room (for passage endpoints)."""
    at, size = room.get_one("at"), room.get_one("size")
    if isinstance(at, Coord) and isinstance(size, Coord):
        return [at.x + size.x / 2, at.y + size.y / 2]
    poly = room.get("polygon")
    if poly:
        return [sum(p.x for p in poly) / len(poly), sum(p.y for p in poly) / len(poly)]
    return None


def _feature_geometry(feat: Feature, ws: Workspace, scope_index: dict[str, Feature]) -> dict | None:
    """Derive the GeoJSON geometry for one feature, or None if not drawable."""
    k = feat.kind
    if k in ("city", "poi"):
        c = feat.get_one("at")
        if isinstance(c, Coord):
            return {"type": "Point", "coordinates": _coord_pair(c)}
    elif k in ("building", "room"):
        poly = feat.get("polygon")
        if poly:
            return {"type": "Polygon", "coordinates": [_polygon_ring(poly)]}
        at, size = feat.get_one("at"), feat.get_one("size")
        if isinstance(at, Coord) and isinstance(size, Coord):
            return {"type": "Polygon", "coordinates": [_rect_from_at_size(at, size)]}
    elif k == "road":
        ls: list[list[float]] = []
        from_ref = feat.get_one("from")
        if from_ref:
            target = ws._index.get(_ident_text(from_ref))
            if target:
                fc = target.get_one("at")
                if isinstance(fc, Coord):
                    ls.append(_coord_pair(fc))
        for v in feat.get("via", []) or []:
            if isinstance(v, Coord):
                ls.append(_coord_pair(v))
        to_ref = feat.get_one("to")
        if to_ref:
            target = ws._index.get(_ident_text(to_ref))
            if target:
                tc = target.get_one("at")
                if isinstance(tc, Coord):
                    ls.append(_coord_pair(tc))
        if len(ls) >= 2:
            return {"type": "LineString", "coordinates": ls}
    elif k == "passage":
        from_ref = feat.get_one("from")
        to_ref = feat.get_one("to")
        if from_ref and to_ref:
            a = scope_index.get(_ident_text(from_ref))
            b = scope_index.get(_ident_text(to_ref))
            if a and b:
                ac, bc = _room_center(a), _room_center(b)
                if ac and bc:
                    return {"type": "LineString", "coordinates": [ac, bc]}
    elif k in ("river", "street"):
        pts = feat.get("points")
        if pts:
            return {"type": "LineString", "coordinates": [[c.x, c.y] for c in pts]}
    elif k in ("kingdom", "terrain", "lake"):
        pts = feat.get("polygon")
        if pts:
            ring = _polygon_ring(pts)
            # Roughen the polygon for natural-looking, hand-drawn-feeling
            # outlines instead of crisp axis-aligned rectangles. Default
            # intensity tuned to look cartographic without going noisy;
            # override per-feature with `roughness 0.06` (or 0 to disable).
            roughness = feat.get_one("roughness")
            try:
                intensity = float(roughness) if roughness is not None else 0.04
            except (ValueError, TypeError):
                intensity = 0.04
            if intensity > 0:
                ring = _roughen_ring(ring, intensity)
            return {"type": "Polygon", "coordinates": [ring]}
    return None


def _feature_properties(feat: Feature) -> dict:
    """Carry over non-geometry properties + tags + description, with IdentPath flattened.

    The output reserves `id`, `kind`, and `_style` for system use. If the DSL
    sets a `kind` property (e.g. `poi sun-gate { kind gate }` to sub-classify
    a POI), it is emitted as `category` so the system `kind` (which carries
    the DSL feature type — `poi`, `city`, etc.) is preserved for layer grouping
    and renderer dispatch. Style selectors like `poi.kind=gate` still match
    against the AST and continue to work unchanged.
    """
    out: dict[str, Any] = {"id": feat.name, "kind": feat.kind}
    for prop in feat.properties.values():
        if prop.name in _GEOMETRY_PROPS:
            continue
        # Map reserved output names to safe alternatives.
        if prop.name == "kind":
            out_name = "category"
        elif prop.name in ("id", "_style"):
            continue  # silently drop conflicting overrides
        else:
            out_name = prop.name
        if prop.name == "tags":
            out[out_name] = list(prop.values)
        elif len(prop.values) == 1:
            v = prop.values[0]
            out[out_name] = _ident_text(v) if isinstance(v, IdentPath) else v
        else:
            out[out_name] = [_ident_text(v) if isinstance(v, IdentPath) else v for v in prop.values]
    return out


def _scope_target(ws: Workspace, view: View) -> tuple[list[Feature], dict[str, Feature]]:
    """Resolve the view's scope to (features-to-iterate, name→feature index for child refs)."""
    scope_p = view.properties.get("scope")
    if not scope_p or not scope_p.values:
        scope_id = ws.name
    else:
        scope_id = _ident_text(scope_p.values[0])
    if scope_id == ws.name:
        return ws.model, ws._index
    target = ws._index.get(scope_id)
    if target is None:
        return [], {}
    # Children are addressable both by short name (within scope) and by full path.
    idx: dict[str, Feature] = {}
    for c in target.children:
        if c.name:
            idx[c.name] = c
            idx[f"{scope_id}.{c.name}"] = c
    return target.children, idx


def _included_kinds(view: View) -> set[str]:
    inc = view.properties.get("include")
    if not inc or not inc.values:
        return set(_KNOWN_KINDS)
    return {_INCLUDE_PLURAL_TO_KIND.get(v, v) for v in inc.values}


def compile_view(ws: Workspace, view_name: str) -> dict:
    """Compile one view to a GeoJSON FeatureCollection dict."""
    view = next((v for v in ws.views if v.name == view_name), None)
    if view is None:
        raise ValueError(f"view {view_name!r} not found in workspace {ws.name!r}")
    scope_features, scope_index = _scope_target(ws, view)
    kinds = _included_kinds(view)
    units_p = view.properties.get("units")
    units = units_p.values[0] if units_p and units_p.values else None
    scope_p = view.properties.get("scope")
    scope_name = _ident_text(scope_p.values[0]) if scope_p and scope_p.values else ws.name

    out_features: list[dict] = []
    for f in scope_features:
        if f.kind not in kinds:
            continue
        geom = _feature_geometry(f, ws, scope_index)
        if geom is None:
            continue
        props = _feature_properties(f)
        props["_style"] = _resolve_styles(f, view, ws)
        out_features.append({"type": "Feature", "geometry": geom, "properties": props})

    return {
        "type": "FeatureCollection",
        "name": view.name,
        "metadata": {
            "view":      view.name,
            "scope":     scope_name,
            "units":     units,
            "workspace": ws.name,
            "kinds":     sorted(kinds),
        },
        "features": out_features,
    }


# ============================================================================
# Routing graph + Dijkstra (no networkx — keep dependency surface small)
# ============================================================================

# Travel-time multipliers per surface keyword; default 1.0 for unknown surfaces.
_SURFACE_MULTIPLIERS: dict[str, float] = {
    "paved":  0.8,
    "stone":  0.85,
    "cobble": 0.9,
    "dirt":   1.0,
    "track":  1.2,
    "trail":  1.5,
    "rough":  1.8,
}


def _euclid(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _build_routing_graph(ws: Workspace) -> dict[str, list[tuple[str, float, str]]]:
    """Build an undirected adjacency list: node → [(neighbor, weight, road_name), ...].

    Nodes are top-level cities. Each road becomes one edge whose weight is the
    Euclidean length of (from-city → vias → to-city) multiplied by the
    surface multiplier.
    """
    graph: dict[str, list[tuple[str, float, str]]] = {}
    for f in ws.model:
        if f.kind != "road":
            continue
        from_ref = f.get_one("from")
        to_ref = f.get_one("to")
        if not from_ref or not to_ref:
            continue
        a_feat = ws._index.get(_ident_text(from_ref))
        b_feat = ws._index.get(_ident_text(to_ref))
        if not (a_feat and b_feat):
            continue
        a_at = a_feat.get_one("at")
        b_at = b_feat.get_one("at")
        if not (isinstance(a_at, Coord) and isinstance(b_at, Coord)):
            continue
        path: list[tuple[float, float]] = [(a_at.x, a_at.y)]
        for v in f.get("via", []) or []:
            if isinstance(v, Coord):
                path.append((v.x, v.y))
        path.append((b_at.x, b_at.y))
        length = sum(_euclid(path[i], path[i + 1]) for i in range(len(path) - 1))
        surface = f.get_one("surface", "dirt")
        mult = _SURFACE_MULTIPLIERS.get(str(surface), 1.0)
        weight = length * mult
        graph.setdefault(a_feat.name, []).append((b_feat.name, weight, f.name))
        graph.setdefault(b_feat.name, []).append((a_feat.name, weight, f.name))
    return graph


def _shortest_path(graph: dict, start: str, goal: str):
    """Dijkstra. Returns (total_weight, path_nodes, path_roads) or None."""
    if start not in graph or goal not in graph:
        return None
    dist: dict[str, float] = {start: 0.0}
    prev: dict[str, tuple[str, str]] = {}
    pq: list[tuple[float, str]] = [(0.0, start)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == goal:
            break
        if d > dist.get(u, math.inf):
            continue
        for v, w, road in graph.get(u, []):
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = (u, road)
                heapq.heappush(pq, (nd, v))
    if goal not in dist:
        return None
    nodes: list[str] = [goal]
    roads: list[str] = []
    cur = goal
    while cur in prev:
        p, road = prev[cur]
        nodes.append(p)
        roads.append(road)
        cur = p
    nodes.reverse()
    roads.reverse()
    return dist[goal], nodes, roads


def _resolve_point(ws: Workspace, ref: Any) -> tuple[float, float] | None:
    """Accept a feature ident, a 'x,y' literal, or an [x, y] tuple/list. Return (x, y) or None."""
    if isinstance(ref, (list, tuple)) and len(ref) == 2:
        try:
            return (float(ref[0]), float(ref[1]))
        except (TypeError, ValueError):
            return None
    if isinstance(ref, str):
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", ref)
        if m:
            return (float(m.group(1)), float(m.group(2)))
        feat = ws._index.get(ref)
        if feat is not None:
            c = feat.get_one("at")
            if isinstance(c, Coord):
                return (c.x, c.y)
    return None


# ============================================================================
# Storage helpers — campaigns/<name>/maps/
# ============================================================================

def _maps_dir() -> Path:
    """Return the maps directory for the active campaign, creating it if needed."""
    cfg = _c.load_campaign()
    d = cfg["_data_dir"] / "maps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _map_path(slug: str) -> Path:
    safe = re.sub(r"[^a-z0-9_-]", "-", slug.lower())
    if safe != slug:
        # caller may have given a name with caps/spaces — accept but normalise
        pass
    return _maps_dir() / f"{safe}.map"


def _safe_slug(slug: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", slug.lower())


def _is_workspace_file(path: Path) -> bool:
    """True if path begins with `workspace <ident> { … }` — i.e., a top-level
    map, not a `!include`-only fragment. Detected by scanning for the first
    non-blank, non-comment line and checking for the `workspace` keyword."""
    try:
        with path.open(encoding="utf-8") as f:
            for raw in f:
                s = raw.strip()
                if not s or s.startswith("#"):
                    continue
                # Accept `workspace name {`, `workspace name`, etc.
                return bool(re.match(r"workspace\b", s))
    except OSError:
        return False
    return False


_SKELETON_TEMPLATE = """\
workspace {slug} {{
    # styles {{ }}    # workspace-level style overrides

    model {{
        # Add features here:
        # kingdom name  {{ color #6b7c4a }}
        # city    name  {{ at 100,100; pop 1000; in kingdom-name; description "..." }}
        # road    name  {{ from city-a; to city-b; via 50,50 75,75; surface dirt }}
        # river   name  {{ points 0,0 50,50 100,80 }}
        # terrain name  {{ polygon 0,0 100,0 100,100 0,100; biome forest }}
        # poi     name  {{ at 50,50; kind event; description "..." }}
    }}

    views {{
        view world {{
            scope {slug}
            include kingdoms, cities, roads, rivers, terrain, poi
            units miles
        }}
    }}
}}
"""


# ============================================================================
# Surgical text-level edit helpers (for add/remove feature)
# ============================================================================

def _find_model_close_line(text: str) -> int | None:
    """Return the 1-indexed line number of the closing `}` of the model block."""
    tokens = _tokenize(text)
    n = len(tokens)
    for i in range(n - 1):
        if tokens[i].type == "IDENT" and tokens[i].value == "model" and tokens[i + 1].type == "LBRACE":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if tokens[j].type == "LBRACE":
                    depth += 1
                elif tokens[j].type == "RBRACE":
                    depth -= 1
                    if depth == 0:
                        return tokens[j].line
                j += 1
    return None


def _find_feature_line_span(text: str, name: str) -> tuple[int, int] | None:
    """Return (start_line, end_line) of the named feature, both 1-indexed.

    Searches token stream for `KIND name {` and walks brace depth back to 0.
    """
    tokens = _tokenize(text)
    n = len(tokens)
    for i in range(n - 2):
        if (
            tokens[i].type == "IDENT" and tokens[i].value in _KNOWN_KINDS
            and tokens[i + 1].type == "IDENT" and tokens[i + 1].value == name
            and tokens[i + 2].type == "LBRACE"
        ):
            start = tokens[i].line
            depth = 1
            j = i + 3
            while j < n and depth > 0:
                if tokens[j].type == "LBRACE":
                    depth += 1
                elif tokens[j].type == "RBRACE":
                    depth -= 1
                    if depth == 0:
                        return (start, tokens[j].line)
                j += 1
    return None


def _splice_lines(text: str, drop_start_1indexed: int, drop_end_1indexed: int) -> str:
    """Remove lines [drop_start, drop_end] inclusive (1-indexed). Preserves trailing newline."""
    lines = text.splitlines(keepends=True)
    return "".join(lines[: drop_start_1indexed - 1] + lines[drop_end_1indexed:])


def _insert_before_line(text: str, before_line_1indexed: int, fragment: str, indent: str = "        ") -> str:
    """Insert `fragment` at the start of `before_line_1indexed` (push existing content down)."""
    lines = text.splitlines(keepends=True)
    body = fragment.rstrip("\n")
    indented = "\n".join((indent + ln) if ln.strip() else ln for ln in body.splitlines())
    insert_block = indented + "\n"
    return "".join(lines[: before_line_1indexed - 1] + [insert_block] + lines[before_line_1indexed - 1 :])


# ============================================================================
# MCP tool registration
# ============================================================================

def register(mcp):

    @mcp.tool()
    def create_world_map(slug: str) -> dict:
        """Create a new world-map DSL file in the active campaign.

        Writes a minimal workspace skeleton with one default `world` view to
        campaigns/<active>/maps/<slug>.map. Errors if the file already exists.

        After creation, populate features via `update_world_map` (overwrite full DSL)
        or `add_world_map_feature` (append a single feature block).
        """
        safe = _safe_slug(slug)
        path = _maps_dir() / f"{safe}.map"
        if path.exists():
            return {"error": f"Map '{safe}' already exists at {path}"}
        skeleton = _SKELETON_TEMPLATE.format(slug=safe)
        _c.atomic_write_text(path, skeleton)
        return {"slug": safe, "path": str(path)}

    @mcp.tool()
    def get_world_map(slug: str, format: str = "dsl", view: str = "") -> dict:
        """Read a world map. format='dsl' returns raw text; format='geojson' compiles a view.

        For format='geojson', the `view` argument is required and must name a
        view declared in the workspace's `views { … }` block.
        """
        path = _maps_dir() / f"{_safe_slug(slug)}.map"
        if not path.exists():
            return {"error": f"Map '{slug}' not found at {path}"}
        if format == "dsl":
            return {"slug": slug, "dsl": path.read_text(encoding="utf-8")}
        if format == "geojson":
            if not view:
                return {"error": "view name required for format='geojson'"}
            try:
                ws = parse_file(path)
                fc = compile_view(ws, view)
            except (SyntaxError, ValueError) as e:
                return {"error": str(e)}
            return {"slug": slug, "view": view, "geojson": fc}
        return {"error": f"unknown format '{format}' (expected 'dsl' or 'geojson')"}

    @mcp.tool()
    def update_world_map(slug: str, dsl: str) -> dict:
        """Overwrite a world map's DSL. Validates by parsing first; rejects on syntax error."""
        path = _maps_dir() / f"{_safe_slug(slug)}.map"
        if not path.exists():
            return {"error": f"Map '{slug}' not found at {path}"}
        try:
            parse_dsl(dsl, base_dir=path.parent, root_path=path)
        except SyntaxError as e:
            return {"error": f"DSL syntax error: {e}"}
        _c.atomic_write_text(path, dsl)
        return {"slug": slug, "updated": True, "path": str(path)}

    @mcp.tool()
    def list_world_maps() -> dict:
        """List all world-map slugs in the active campaign's maps/ directory.

        Files containing only fragments (no top-level `workspace` block — typically
        `!include` targets) are listed separately under `fragments`.
        """
        d = _maps_dir()
        maps:      list[str] = []
        fragments: list[str] = []
        for p in sorted(d.glob("*.map")):
            (maps if _is_workspace_file(p) else fragments).append(p.stem)
        return {"maps": maps, "fragments": fragments, "dir": str(d)}

    @mcp.tool()
    def list_world_map_views(slug: str) -> dict:
        """List the views declared in a world map, with their scope, include, and units."""
        path = _maps_dir() / f"{_safe_slug(slug)}.map"
        if not path.exists():
            return {"error": f"Map '{slug}' not found at {path}"}
        try:
            ws = parse_file(path)
        except SyntaxError as e:
            return {"error": str(e)}
        out = []
        for v in ws.views:
            scope_p = v.properties.get("scope")
            inc_p = v.properties.get("include")
            units_p = v.properties.get("units")
            out.append({
                "name":    v.name,
                "scope":   _ident_text(scope_p.values[0]) if scope_p and scope_p.values else None,
                "include": list(inc_p.values) if inc_p else None,
                "units":   units_p.values[0] if units_p and units_p.values else None,
            })
        return {"slug": slug, "views": out}

    @mcp.tool()
    def place_party_on_map(slug: str, x: float, y: float, label: str = "") -> dict:
        """Place or move the party marker on a world map.

        Writes a separate `<slug>.party.geojson` overlay alongside the DSL
        (the DSL stays declarative; the overlay is mutable scratch state).
        Coordinates are in the map's authoring frame; pick them to match the
        scale of whichever view the marker should show on.
        """
        path = _maps_dir() / f"{_safe_slug(slug)}.map"
        if not path.exists():
            return {"error": f"Map '{slug}' not found at {path}"}
        overlay_path = _maps_dir() / f"{_safe_slug(slug)}.party.geojson"
        overlay = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(x), float(y)]},
                "properties": {
                    "id":     "party",
                    "kind":   "party",
                    "label":  label or "Party",
                    "_style": {
                        "color":        "#ffcc00",
                        "stroke":       "#000000",
                        "stroke-width": 2,
                        "marker-size":  10,
                    },
                },
            }],
        }
        _c.atomic_write_text(overlay_path, json.dumps(overlay, indent=2))
        return {"slug": slug, "x": float(x), "y": float(y), "label": label or "Party"}

    @mcp.tool()
    def world_map_distance_direct(slug: str, a: str, b: str) -> dict:
        """Euclidean distance between two world-map points or features.

        `a` and `b` may each be:
          - a feature identifier (looks up the feature's `at` coord), or
          - an explicit coord literal 'x,y' (e.g. '412,308').
        """
        path = _maps_dir() / f"{_safe_slug(slug)}.map"
        if not path.exists():
            return {"error": f"Map '{slug}' not found at {path}"}
        try:
            ws = parse_file(path)
        except SyntaxError as e:
            return {"error": str(e)}
        pa = _resolve_point(ws, a)
        pb = _resolve_point(ws, b)
        if pa is None: return {"error": f"could not resolve point {a!r}"}
        if pb is None: return {"error": f"could not resolve point {b!r}"}
        return {"distance": _euclid(pa, pb), "from": list(pa), "to": list(pb)}

    @mcp.tool()
    def world_map_distance_via_roads(slug: str, a: str, b: str) -> dict:
        """Shortest road-network distance between two cities.

        Returns total weighted distance, the city path, and the roads traversed.
        Edge weight = segment Euclidean length × surface multiplier
        (paved 0.8, stone 0.85, cobble 0.9, dirt 1.0, track 1.2, trail 1.5, rough 1.8).
        """
        path = _maps_dir() / f"{_safe_slug(slug)}.map"
        if not path.exists():
            return {"error": f"Map '{slug}' not found at {path}"}
        try:
            ws = parse_file(path)
        except SyntaxError as e:
            return {"error": str(e)}
        graph = _build_routing_graph(ws)
        if a not in graph:
            return {"error": f"{a!r} is not connected to any road"}
        if b not in graph:
            return {"error": f"{b!r} is not connected to any road"}
        result = _shortest_path(graph, a, b)
        if result is None:
            return {"error": f"no road path between {a!r} and {b!r}"}
        dist, nodes, roads = result
        return {"distance": dist, "path": nodes, "roads": roads}

    @mcp.tool()
    def world_map_nearest(slug: str, point: str, kind: str = "city", n: int = 1) -> dict:
        """Find the N nearest features of `kind` to `point` (Euclidean).

        `point` is a feature ident or an 'x,y' literal. `kind` is a singular
        feature kind (`city`, `poi`, `building`, ...).
        """
        path = _maps_dir() / f"{_safe_slug(slug)}.map"
        if not path.exists():
            return {"error": f"Map '{slug}' not found at {path}"}
        try:
            ws = parse_file(path)
        except SyntaxError as e:
            return {"error": str(e)}
        p = _resolve_point(ws, point)
        if p is None:
            return {"error": f"could not resolve point {point!r}"}

        results: list[tuple[str, float]] = []
        # Walk every feature in the index (including children) and check kind + at.
        for fname, feat in ws._index.items():
            if feat.kind != kind:
                continue
            c = feat.get_one("at")
            if isinstance(c, Coord):
                results.append((fname, _euclid(p, (c.x, c.y))))
        results.sort(key=lambda x: x[1])
        return {
            "point":   list(p),
            "kind":    kind,
            "results": [{"name": name, "distance": d} for name, d in results[:max(1, n)]],
        }

    @mcp.tool()
    def add_world_map_feature(slug: str, dsl_fragment: str) -> dict:
        """Append a feature block to a world map's model section.

        `dsl_fragment` is a complete feature block such as:
            city orlane { at 398,224; pop 100; in geoff }
            road south-trade-track { from hochoch; to orlane; surface dirt }

        The fragment is inserted just before the closing `}` of `model`. The
        result is parsed before writing — if the new feature is malformed, or
        references identifiers that don't exist, the write is rejected.
        """
        path = _maps_dir() / f"{_safe_slug(slug)}.map"
        if not path.exists():
            return {"error": f"Map '{slug}' not found at {path}"}
        text = path.read_text(encoding="utf-8")
        close_line = _find_model_close_line(text)
        if close_line is None:
            return {"error": "could not find a `model { … }` block in the DSL"}
        new_text = _insert_before_line(text, close_line, dsl_fragment)
        try:
            parse_dsl(new_text, base_dir=path.parent, root_path=path)
        except SyntaxError as e:
            return {"error": f"insertion produced invalid DSL: {e}"}
        _c.atomic_write_text(path, new_text)
        return {"slug": slug, "added": True}

    @mcp.tool()
    def remove_world_map_feature(slug: str, name: str) -> dict:
        """Remove a feature by identifier from a world map.

        Removes the entire `KIND name { … }` block. Validates that no remaining
        feature references the removed identifier (via from/to/in); if any do,
        the removal is rejected and the offending references are reported.
        """
        path = _maps_dir() / f"{_safe_slug(slug)}.map"
        if not path.exists():
            return {"error": f"Map '{slug}' not found at {path}"}
        text = path.read_text(encoding="utf-8")
        span = _find_feature_line_span(text, name)
        if span is None:
            return {"error": f"feature {name!r} not found in map '{slug}'"}
        new_text = _splice_lines(text, span[0], span[1])
        try:
            parse_dsl(new_text, base_dir=path.parent, root_path=path)
        except SyntaxError as e:
            return {"error": f"removal would orphan a reference: {e}"}
        _c.atomic_write_text(path, new_text)
        return {"slug": slug, "removed": name, "lines_removed": span[1] - span[0] + 1}

    @mcp.tool()
    def compile_world_map_view(slug: str, view: str) -> dict:
        """Force-compile a world map view to GeoJSON (no caching). Useful for inspection.

        Returns the FeatureCollection dict directly. To save it as a static
        artifact alongside the DSL, also writes <slug>.<view>.geojson.
        """
        path = _maps_dir() / f"{_safe_slug(slug)}.map"
        if not path.exists():
            return {"error": f"Map '{slug}' not found at {path}"}
        try:
            ws = parse_file(path)
            fc = compile_view(ws, view)
        except (SyntaxError, ValueError) as e:
            return {"error": str(e)}
        out_path = path.with_suffix("").parent / f"{_safe_slug(slug)}.{view}.geojson"
        _c.atomic_write_text(out_path, json.dumps(fc, indent=2))
        return {
            "slug":     slug,
            "view":     view,
            "features": len(fc["features"]),
            "path":     str(out_path),
            "geojson":  fc,
        }


# ============================================================================
# Self-tests (run with: python3 -m tools.world_map)
# ============================================================================

_SAMPLE = """\
workspace sheldomar {
    styles {
        terrain.biome=marsh  { color #4a6b3a; fill-opacity 0.4 }
        terrain.biome=forest { color #2d5a2d }
        city                 { marker circle; color #222; marker-size 6 }
        [walled]             { stroke #444; stroke-width 2 }
    }

    model {
        kingdom geoff   { color #6b7c4a; capital hochoch
                          description "Frontier duchy on the Sheldomar's western edge." }
        city hochoch {
            at  412,308
            pop 2400
            in  geoff
            tags [walled, trade, militia]
            description "Frontier town."
            doc locations/hochoch.md
        }
        city orlane {
            at  398,224
            pop 100
            in  geoff
            tags [farming, troubled]
            description "Farming village in the southern Sheldomar."
            contains {
                building golden-grain-inn { at 12,8;  size 6,4;  type inn; tags [cult-hq] }
                building temple-merikka   { at 18,6;  size 8,12; type temple; tags [walled] }
                street  main-road         { points 0,10 30,10 }
            }
        }
        road south-trade-track {
            from hochoch
            to   orlane
            via  410,260 405,240
            surface dirt
        }
        river javan { points 380,400 385,380 390,360 }
        terrain rushmoors { polygon 350,200 380,180 390,260 360,280; biome marsh }
        poi crocodile-attack { at 372,250; kind event; description "Caravan lost wagons here." }
        dungeon temple-cellars {
            in orlane.temple-merikka
            room antechamber { at 0,0 size 4,4 }
            room pit-chamber { at 4,0 size 6,6; tags [hazard] }
            passage          { from antechamber; to pit-chamber }
        }
    }

    views {
        view world {
            scope sheldomar
            include kingdoms, cities, roads, rivers, terrain, poi
            units miles
            styles {
                terrain.biome=desert { color #f0c070 }
            }
        }
        view orlane-city    { scope orlane;         include buildings, streets, poi; units feet }
        view temple-cellars { scope temple-cellars; include rooms, passages;         units feet }
    }
}
"""


def _self_test() -> None:
    """Smoke-test the parser against the sample DSL."""
    ws = parse_dsl(_SAMPLE)

    # Top-level structure
    assert ws.name == "sheldomar", f"workspace name: {ws.name}"
    assert len(ws.styles) == 4, f"workspace styles: {len(ws.styles)}"
    assert len(ws.views) == 3, f"views: {len(ws.views)}"

    # Find the orlane city, check its children survived parsing
    cities = [f for f in ws.model if f.kind == "city"]
    assert len(cities) == 2, f"cities: {len(cities)}"
    orlane = next(c for c in cities if c.name == "orlane")
    assert len(orlane.children) == 3, f"orlane children: {len(orlane.children)}"
    assert orlane.children[0].kind == "building"
    assert orlane.children[0].name == "golden-grain-inn"
    assert orlane.get_one("pop") == 100
    assert orlane.get_one("description") == "Farming village in the southern Sheldomar."
    assert orlane.get("tags") == [["farming", "troubled"]] or orlane.get("tags") == ["farming", "troubled"], (
        f"orlane tags: {orlane.get('tags')}"
    )

    # Coord parsing
    hochoch = next(c for c in cities if c.name == "hochoch")
    at = hochoch.get_one("at")
    assert isinstance(at, Coord) and at.x == 412 and at.y == 308, f"hochoch at: {at}"

    # Road from/to references resolve
    road = next(f for f in ws.model if f.kind == "road")
    assert road.get_one("from") == "hochoch"
    assert road.get_one("to") == "orlane"
    assert len(road.get("via")) == 2, "via has 2 coords"

    # Dotted IdentPath
    dungeon = next(f for f in ws.model if f.kind == "dungeon")
    parent_ref = dungeon.get_one("in")
    assert isinstance(parent_ref, IdentPath), f"in is not IdentPath: {parent_ref!r}"
    assert parent_ref.parts == ["orlane", "temple-merikka"]

    # Dungeon rooms + passage
    assert len(dungeon.children) == 3, f"dungeon children: {len(dungeon.children)}"
    passage = dungeon.children[2]
    assert passage.kind == "passage" and passage.name == ""

    # Views — world view contains style override
    world = next(v for v in ws.views if v.name == "world")
    assert len(world.styles) == 1, f"world view styles: {len(world.styles)}"
    assert world.styles[0].selector == {
        "kind": "terrain", "prop": "biome", "op": "=", "value": "desert"
    }

    # Reference validation should have populated _index
    assert hasattr(ws, "_index")
    assert "orlane" in ws._index
    assert "orlane.temple-merikka" in ws._index

    print("OK — parser self-test passed")
    print(f"  workspace: {ws.name}")
    print(f"  features:  {len(ws.model)} top-level, {len(ws._index)} total")
    print(f"  styles:    {len(ws.styles)} workspace, "
          f"{sum(len(v.styles) for v in ws.views)} view-level")
    print(f"  views:     {[v.name for v in ws.views]}")


def _self_test_includes(tmp_dir: Path) -> None:
    """Verify nested !include works inside a contains block."""
    tmp_dir.mkdir(exist_ok=True)
    (tmp_dir / "buildings.map").write_text(
        "building inn { at 1,2; size 3,4; type inn }\n"
        "building temple { at 5,6; size 4,4; type temple }\n",
        encoding="utf-8",
    )
    main = (
        "workspace t { model {\n"
        "  city village { at 0,0; contains {\n"
        "    !include buildings.map\n"
        "  } }\n"
        "} views { view v { scope village; include buildings } } }\n"
    )
    ws = parse_dsl(main, base_dir=tmp_dir)
    village = ws.model[0]
    assert village.name == "village"
    assert len(village.children) == 2, f"included buildings: {len(village.children)}"
    assert village.children[0].name == "inn"
    assert village.children[1].name == "temple"
    print("OK — !include inside contains works")


def _self_test_compiler() -> None:
    """Compile each view and verify GeoJSON shape + style cascade."""
    ws = parse_dsl(_SAMPLE)

    # ── World view ────────────────────────────────────────────────────────
    world = compile_view(ws, "world")
    assert world["type"] == "FeatureCollection"
    assert world["metadata"]["scope"] == "sheldomar"
    assert world["metadata"]["units"] == "miles"

    by_id = {f["properties"]["id"]: f for f in world["features"]}
    # Cities
    assert by_id["hochoch"]["geometry"] == {"type": "Point", "coordinates": [412, 308]}
    assert by_id["orlane"]["geometry"]["type"] == "Point"
    # Tags survived
    assert "walled" in by_id["hochoch"]["properties"]["tags"]
    # Description survived
    assert by_id["hochoch"]["properties"]["description"] == "Frontier town."
    # doc soft-link survived
    assert by_id["hochoch"]["properties"]["doc"] == "locations/hochoch.md"
    # Road geometry: hochoch → 410,260 → 405,240 → orlane
    road = by_id["south-trade-track"]
    assert road["geometry"]["type"] == "LineString"
    assert road["geometry"]["coordinates"][0] == [412, 308]    # hochoch
    assert road["geometry"]["coordinates"][-1] == [398, 224]   # orlane
    assert len(road["geometry"]["coordinates"]) == 4
    # Terrain polygon closed
    rushmoors = by_id["rushmoors"]
    assert rushmoors["geometry"]["type"] == "Polygon"
    ring = rushmoors["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1], "polygon ring must be closed"

    # ── Style cascade ─────────────────────────────────────────────────────
    # Workspace-level: terrain biome=marsh → #4a6b3a (overrides built-in default)
    assert rushmoors["properties"]["_style"]["color"] == "#4a6b3a"
    # City has marker:circle from built-in (workspace's `city` rule overrides too)
    hochoch_style = by_id["hochoch"]["properties"]["_style"]
    assert hochoch_style["marker"] == "circle"
    # Tag selector hit: hochoch is [walled] → stroke #444, stroke-width 2 from workspace
    assert hochoch_style.get("stroke") == "#444"
    assert hochoch_style.get("stroke-width") == 2

    # ── Orlane city view (scope orlane, only buildings/streets/poi) ───────
    orlane_view = compile_view(ws, "orlane-city")
    assert orlane_view["metadata"]["scope"] == "orlane"
    assert orlane_view["metadata"]["units"] == "feet"
    feats = orlane_view["features"]
    kinds = {f["properties"]["kind"] for f in feats}
    assert kinds == {"building", "street"}, f"orlane-city kinds: {kinds}"
    # Building rectangle from at+size
    inn = next(f for f in feats if f["properties"]["id"] == "golden-grain-inn")
    assert inn["geometry"]["type"] == "Polygon"
    ring = inn["geometry"]["coordinates"][0]
    # at 12,8  size 6,4  → rect (12,8)-(18,8)-(18,12)-(12,12)
    assert ring[0] == [12, 8] and ring[1] == [18, 8] and ring[2] == [18, 12] and ring[3] == [12, 12]
    # tag-selector should make temple-merikka have stroke=#444
    temple = next(f for f in feats if f["properties"]["id"] == "temple-merikka")
    assert temple["properties"]["_style"].get("stroke") == "#444"

    # ── Temple cellars view (rooms + passages) ────────────────────────────
    cellars = compile_view(ws, "temple-cellars")
    feats = cellars["features"]
    kinds = {f["properties"]["kind"] for f in feats}
    assert kinds == {"room", "passage"}, f"temple-cellars kinds: {kinds}"
    # Passage geometry: line between room centers
    passage = next(f for f in feats if f["properties"]["kind"] == "passage")
    assert passage["geometry"]["type"] == "LineString"
    # antechamber center: (0+4/2, 0+4/2) = (2, 2)
    # pit-chamber center: (4+6/2, 0+6/2) = (7, 3)
    assert passage["geometry"]["coordinates"] == [[2, 2], [7, 3]]

    # ── View-level style override ─────────────────────────────────────────
    # World view defines: terrain.biome=desert → #f0c070
    # Sample has no desert terrain, so we just verify the rule was honored
    # by parsing — the cascade ran without error. Add a desert in a synthetic test:
    extra = """
    workspace t {
        styles { terrain.biome=desert { color #aaa } }
        model { terrain dunes { polygon 0,0 1,0 1,1 0,1; biome desert } }
        views { view v { scope t; include terrain; styles { terrain.biome=desert { color #f0c070 } } } }
    }
    """
    ws2 = parse_dsl(extra)
    fc = compile_view(ws2, "v")
    desert = fc["features"][0]
    # View-level rule wins over workspace-level rule
    assert desert["properties"]["_style"]["color"] == "#f0c070", (
        f"view override failed: {desert['properties']['_style']}"
    )

    print("OK — compiler self-test passed")
    print(f"  world view:        {len(world['features'])} features")
    print(f"  orlane-city view:  {len(orlane_view['features'])} features")
    print(f"  temple-cellars:    {len(cellars['features'])} features")


def _self_test_edits() -> None:
    """Verify text-level add/remove against a sample DSL."""
    text = _SAMPLE
    # Find where `model {` closes
    close = _find_model_close_line(text)
    assert close is not None, "model close line not found"
    # The model block must contain at least one of our top-level features
    span = _find_feature_line_span(text, "hochoch")
    assert span is not None, "hochoch span not found"
    # hochoch is multiple lines: at, pop, in, tags, description, doc
    assert span[1] > span[0], f"hochoch span not multi-line: {span}"

    # Insert a new POI before model close
    fragment = 'poi shrine { at 100,100; kind landmark; description "Roadside shrine." }'
    new_text = _insert_before_line(text, close, fragment)
    ws2 = parse_dsl(new_text)
    assert "shrine" in ws2._index, "added shrine not in index"

    # Remove an existing feature (poi crocodile-attack — no inbound refs)
    span2 = _find_feature_line_span(text, "crocodile-attack")
    assert span2 is not None
    removed_text = _splice_lines(text, span2[0], span2[1])
    ws3 = parse_dsl(removed_text)
    assert "crocodile-attack" not in ws3._index

    # Refusing removal that would orphan refs: orlane is referenced by the road
    span3 = _find_feature_line_span(text, "orlane")
    assert span3 is not None
    orphan_text = _splice_lines(text, span3[0], span3[1])
    try:
        parse_dsl(orphan_text)
    except SyntaxError as e:
        assert "orlane" in str(e), f"unexpected error: {e}"
    else:
        raise AssertionError("expected orphan reference error after removing orlane")

    print("OK — surgical-edit self-test passed")


def _self_test_routing() -> None:
    """Verify routing graph + Dijkstra against a small connected sample."""
    dsl = """
    workspace test {
        model {
            city a { at 0,0 }
            city b { at 10,0 }
            city c { at 10,10 }
            city d { at 50,50 }
            road a-b { from a; to b; surface paved }
            road b-c { from b; to c; surface dirt  }
            road a-c { from a; to c; surface trail }
        }
        views { view world { scope test; include cities, roads } }
    }
    """
    ws = parse_dsl(dsl)
    g = _build_routing_graph(ws)
    assert "a" in g and "b" in g and "c" in g
    assert "d" not in g, "d has no roads, must not be in the graph"

    # Direct: a-b paved (10 * 0.8 = 8); a-c trail (sqrt(200) * 1.5 ≈ 21.2)
    res = _shortest_path(g, "a", "b")
    assert res is not None
    dist, nodes, roads = res
    assert math.isclose(dist, 10 * 0.8), f"a→b dist: {dist}"
    assert nodes == ["a", "b"]

    # a → c: direct trail vs a-b-c (paved + dirt): 8 + 10 = 18 < 21.2 → routed via b
    res = _shortest_path(g, "a", "c")
    dist, nodes, roads = res
    assert math.isclose(dist, 18.0), f"a→c shortest: {dist}"
    assert nodes == ["a", "b", "c"], f"a→c path: {nodes}"

    # No path
    res = _shortest_path(g, "a", "d")
    assert res is None, "expected no path to d"

    # _resolve_point: ident, coord literal, tuple
    assert _resolve_point(ws, "a") == (0, 0)
    assert _resolve_point(ws, "12.5,7") == (12.5, 7.0)
    assert _resolve_point(ws, [3, 4]) == (3.0, 4.0)
    assert _resolve_point(ws, "nonexistent") is None

    print("OK — routing self-test passed")


if __name__ == "__main__":
    import tempfile
    _self_test()
    with tempfile.TemporaryDirectory() as td:
        _self_test_includes(Path(td))
    _self_test_compiler()
    _self_test_edits()
    _self_test_routing()
    print("All self-tests passed.")
