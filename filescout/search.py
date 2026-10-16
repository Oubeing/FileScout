"""Query language: tokenizer, parser, compiler and executor.

Supported syntax:

  term1 term2          implicit AND
  term1 | term2        OR (also the keyword ``or``)
  !term / <>term       NOT
  ( ... )              grouping
  * ?                  wildcards (``*`` = any sequence, ``?`` = one char);
                       a term without wildcards matches anywhere in the name
  regex:pattern        regular expression term
  path: / wfp: / fn: / wfn: / stem:      search targets
  ext:txt;md  /  ext:<>exe               extension filter (``;`` separated)
  folder: / file: / dupe:                object kind / duplicate names
  size:>10mb  size:10mb..20mb  size:<1gb size expressions
  dm: dc: da:         date modified / created / accessed
                       (today, yesterday, thisweek, lastweek, thismonth,
                        lastmonth, thisyear, lastyear, past7days, absolute
                        dates, ``>``/``<`` prefixes and ``a..b`` ranges)
  attrib:D  attrib:RS  attrib:<>H         attribute filters
  depth:n  depth:>2  depth:1..3           depth below the index root
  content:text        (slow) file content search
  zip: doc: exe: pic: audio: video:       file type macros
  sort:name  sort:size-descending  ...    result ordering

Queries that only need plain name/path/size/date/extension matching are
compiled to a single SQL statement (fast path).  Anything needing regular
expressions, whole-word or diacritic-insensitive matching, depth, duplicates
or content search is evaluated with Python predicates while streaming results.
"""
from __future__ import annotations

import re
import time

from .util import normalize_diacritics, parse_date_expr, parse_size_expr

# ---------------------------------------------------------------------------
# macro tables
# ---------------------------------------------------------------------------

EXT_MACROS = {
    "audio": ("aac aiff ape au flac m4a mid midi mp2 mp3 ogg opus ra wav wma "
              "amr awb ac3 dts mka aif").split(),
    "zip": "7z zip rar tar gz tgz bz2 xz z cab arj lzh lha ace jar war sit".split(),
    "doc": ("doc docx rtf txt pdf odt tex md log csv xls xlsx ppt pptx odp pub "
            "chm htm html xps epub mobi azw azw3 djvu pages numbers key").split(),
    "exe": "exe com bat cmd msi scr pif".split(),
    "pic": ("png jpg jpeg gif bmp ico tif tiff webp svg psd raw cr2 nef arw orf "
            "rw2 dng xcf heic avif jfif pbm pgm ppm xbm xpm pcx tga dds exr hdr").split(),
    "video": ("mp4 mkv avi mov wmv flv webm mpeg mpg m4v 3gp 3g2 ts m2ts mts rm "
              "rmvb vob asf divx f4v ogv mxf").split(),
}

ATTRIB_MAP = {
    "R": 0x00000001, "H": 0x00000002, "S": 0x00000004, "D": 0x00000010,
    "A": 0x00000020, "E": 0x00000040, "N": 0x00000080, "T": 0x00000100,
    "P": 0x00000200, "L": 0x00000400, "O": 0x00001000, "I": 0x00002000,
    "V": 0x00010000,
}

MACROS = {
    "folder", "file", "dupe", "depth", "path", "fn", "wfn", "wfp", "ext",
    "size", "dm", "dc", "da", "attrib", "content", "regex", "zip", "doc",
    "exe", "pic", "audio", "video", "stem", "type",
}

SORT_LABELS = {
    "name": "Name", "path": "Path", "size": "Size", "extension": "Extension",
    "dm": "Date Modified", "dc": "Date Created", "da": "Date Accessed",
    "attrib": "Attributes",
}

SORT_ALIASES = {
    "name": "name", "path": "path", "size": "size", "extension": "extension",
    "ext": "extension", "dm": "dm", "modified": "dm", "date-modified": "dm",
    "datemodified": "dm", "dc": "dc", "created": "dc", "date-created": "dc",
    "datecreated": "dc", "da": "da", "accessed": "da", "date-accessed": "da",
    "dateaccessed": "da", "attrib": "attrib", "attributes": "attrib",
}

SORT_SQL = {
    "name": "name",
    "path": "parent || name",
    "size": "size",
    "extension": "extension",
    "dm": "date_modified",
    "dc": "date_created",
    "da": "date_accessed",
    "attrib": "attributes",
}

_COLS = ("name", "parent", "is_folder", "size", "date_modified",
         "date_created", "date_accessed", "attributes", "extension")

_DATE_COLS = {"dm": "date_modified", "dc": "date_created", "da": "date_accessed"}


class QueryError(Exception):
    """Raised for malformed queries (bad size/date/attrib/regex syntax)."""


# ---------------------------------------------------------------------------
# options / results
# ---------------------------------------------------------------------------

class SearchOptions:
    def __init__(self, match_case=False, match_whole_word=False, match_path=False,
                 regex=False, match_diacritics=False, duplicates=False,
                 folders_first=False, sort="name", sort_ascending=True,
                 max_results=10000, roots=None, content_max_size=8 * 1024 * 1024,
                 dupes=None):
        self.match_case = match_case
        self.match_whole_word = match_whole_word
        self.match_path = match_path
        self.regex = regex
        self.match_diacritics = match_diacritics
        self.duplicates = duplicates
        self.folders_first = folders_first
        self.sort = SORT_ALIASES.get(str(sort).lower(), "name")
        self.sort_ascending = sort_ascending
        self.max_results = max(1, int(max_results))
        self.roots = list(roots or [])
        self.content_max_size = content_max_size
        self.dupes = dupes  # set of lowercased duplicate names (filled by run_search)

    @classmethod
    def from_config(cls, cfg, **overrides):
        s = cfg.search
        opts = cls(
            match_case=s["match_case"], match_whole_word=s["match_whole_word"],
            match_path=s["match_path"], regex=s["regex"],
            match_diacritics=s["match_diacritics"], duplicates=s["duplicates"],
            folders_first=s["folders_first"], sort=s["sort"],
            sort_ascending=s["sort_ascending"], max_results=cfg.general["max_results"],
            roots=cfg.roots(), content_max_size=cfg.general["content_max_size"],
        )
        for key, val in overrides.items():
            if val is not None and hasattr(opts, key):
                setattr(opts, key, val)
        return opts


class SearchResult:
    def __init__(self):
        self.rows = []          # list of dict rows
        self.truncated = False  # more matches exist beyond max_results
        self.total_size = 0
        self.took = 0.0
        self.query = ""


# ---------------------------------------------------------------------------
# tokenizer / parser
# ---------------------------------------------------------------------------

def tokenize(query):
    """Split into tokens: words, '(', ')', '|', '!' ('<>' normalizes to '!').

    ``!`` and ``<>`` only act as operators at the *start* of a token, so
    values like ``ext:<>exe`` or file names containing ``!`` stay intact.
    Inside a ``regex:`` / ``content:`` value, ``()``, ``|`` and ``!`` are
    literal characters (up to the next whitespace), so patterns such as
    ``regex:^(cli|search)\\.py$`` survive tokenization.
    """
    tokens = []
    buf = []
    special = False  # inside a regex:/content: value

    def flush():
        nonlocal buf, special
        if buf:
            tokens.append("".join(buf))
            buf = []
            special = False

    i = 0
    n = len(query or "")
    while i < n:
        ch = query[i]
        if ch.isspace():
            flush()
        elif not special and ch in "()":
            flush()
            tokens.append(ch)
        elif not special and ch == "|":
            flush()
            tokens.append("|")
        elif not special and not buf and ch == "!":
            tokens.append("!")
        elif (not special and not buf and ch == "<"
              and i + 1 < n and query[i + 1] == ">"):
            tokens.append("!")
            i += 1
        else:
            buf.append(ch)
            if not special and "".join(buf).lower() in ("regex:", "content:"):
                special = True
        i += 1
    flush()
    return ["|" if t.lower() == "or" else t for t in tokens]


class _Term:
    __slots__ = ("token",)

    def __init__(self, token):
        self.token = token


class _Not:
    __slots__ = ("child",)

    def __init__(self, child):
        self.child = child


class _And:
    __slots__ = ("children",)

    def __init__(self, children):
        self.children = children


class _Or:
    __slots__ = ("children",)

    def __init__(self, children):
        self.children = children


def parse(tokens):
    """Recursive descent: NOT > AND > OR."""
    pos = 0

    def parse_or():
        nonlocal pos
        node = parse_and()
        while pos < len(tokens) and tokens[pos] == "|":
            pos += 1
            rhs = parse_and()
            if isinstance(node, _Or):
                node.children.append(rhs)
            else:
                node = _Or([node, rhs])
        return node

    def parse_and():
        nonlocal pos
        children = [parse_unary()]
        while pos < len(tokens) and tokens[pos] not in (")", "|"):
            children.append(parse_unary())
        return children[0] if len(children) == 1 else _And(children)

    def parse_unary():
        nonlocal pos
        if pos < len(tokens) and tokens[pos] == "!":
            pos += 1
            return _Not(parse_unary())
        return parse_primary()

    def parse_primary():
        nonlocal pos
        if pos >= len(tokens):
            return _Term("")
        tok = tokens[pos]
        pos += 1
        if tok == "(":
            node = parse_or()
            if pos < len(tokens) and tokens[pos] == ")":
                pos += 1
            return node
        if tok in (")", "|", "!"):
            return _Term("")  # stray operator -> harmless no-op
        return _Term(tok)

    if not tokens:
        return None
    return parse_or()


def parse_sort_term(value):
    """'size-descending' -> ('size', False);  'name' -> ('name', True)."""
    v = (value or "").strip().lower()
    ascending = True
    for suffix, asc in (("-ascending", True), ("-asc", True),
                        ("-descending", False), ("-desc", False)):
        if v.endswith(suffix):
            v = v[: -len(suffix)]
            ascending = asc
            break
    return SORT_ALIASES.get(v, "name"), ascending


def _extract_sort(tokens):
    """Pull sort: terms out of the token list."""
    sort_key, ascending, kept = None, True, []
    for tok in tokens:
        if ":" in tok:
            name, _, value = tok.partition(":")
            if name.lower() == "sort":
                sort_key, ascending = parse_sort_term(value)
                continue
        kept.append(tok)
    return sort_key, ascending, kept


# ---------------------------------------------------------------------------
# compilation helpers
# ---------------------------------------------------------------------------

def _like_pattern(term, anchored):
    out = []
    for ch in term:
        if ch == "*":
            out.append("%")
        elif ch == "?":
            out.append("_")
        elif ch in ("%", "_", "\\"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    pat = "".join(out)
    return pat if anchored else "%" + pat + "%"


def _has_wildcard(term):
    return "*" in term or "?" in term


_WILD_CACHE = {}


def _wild_regex(pattern):
    rx = _WILD_CACHE.get(pattern)
    if rx is None:
        parts = []
        for ch in pattern:
            if ch == "*":
                parts.append(".*")
            elif ch == "?":
                parts.append(".")
            else:
                parts.append(re.escape(ch))
        rx = re.compile("".join(parts) + r"\Z")
        _WILD_CACHE[pattern] = rx
    return rx


def _split_macro(token):
    if ":" not in token:
        return None, token
    name, _, value = token.partition(":")
    if name.lower() in MACROS:
        return name.lower(), value
    return None, token


def _cmp_value(op, a, b):
    if op == "eq":
        return a == b
    if op == "gt":
        return a > b
    if op == "lt":
        return a < b
    if op == "ge":
        return a >= b
    if op == "le":
        return a <= b
    if op == "range":
        return a >= b[0] and a <= b[1]
    raise QueryError("invalid operator %r" % op)


def _parse_int_expr(text, what):
    text = (text or "").strip()
    if ".." in text:
        a, b = text.split("..", 1)
        try:
            return ("range", (int(a), int(b)))
        except ValueError:
            raise QueryError("invalid %s: %r" % (what, text))
    for op in (">=", "<=", "==", ">", "<", "="):
        if text.startswith(op):
            try:
                return ({">=": "ge", "<=": "le", "==": "eq", "=": "eq",
                         ">": "gt", "<": "lt"}[op], int(text[len(op):]))
            except ValueError:
                raise QueryError("invalid %s: %r" % (what, text))
    try:
        return ("eq", int(text))
    except ValueError:
        raise QueryError("invalid %s: %r" % (what, text))


def _words(name):
    out, cur = [], []
    for ch in name:
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def _content_match(full_path, terms, opts):
    try:
        with open(full_path, "rb") as fh:
            data = fh.read(opts.content_max_size)
    except OSError:
        return False
    if not opts.match_case:
        data = data.lower()
    for term in terms:
        needle = term.encode("utf-8", "ignore")
        if not opts.match_case:
            needle = needle.lower()
        if needle not in data:
            return False
    return True


# ---------------------------------------------------------------------------
# compiler
# ---------------------------------------------------------------------------

class CompiledQuery:
    def __init__(self):
        self.where = "1"
        self.params = []
        self.order_sql = "name ASC"
        self.predicate = None      # python-path row -> bool
        self.limit = 1000
        self.sort_key = "name"
        self.sort_ascending = True
        self.query = ""


def _order_sql(opts):
    parts = []
    if opts.folders_first:
        parts.append("is_folder DESC")
    key = SORT_SQL.get(opts.sort, "name")
    parts.append("%s %s" % (key, "ASC" if opts.sort_ascending else "DESC"))
    if opts.sort != "name":
        parts.append("name ASC")
    parts.append("id ASC")
    return ", ".join(parts)


def _term_sql(token, opts):
    """Compile one term -> (sql, params, predicate_or_None)."""
    macro, value = _split_macro(token)

    if macro is None:
        if opts.regex:
            return "1", [], _make_regex_predicate(value, opts)
        if opts.match_whole_word or opts.match_diacritics:
            return "1", [], _make_name_predicate(value, opts)
        target = "parent || name" if opts.match_path else "name"
        return ("%s LIKE ? ESCAPE '\\'" % target,
                [_like_pattern(value, _has_wildcard(value))], None)

    if macro == "folder":
        return "is_folder = 1", [], None
    if macro == "file":
        return "is_folder = 0", [], None
    if macro == "dupe":
        dupes = opts.dupes or set()
        return ("1", [], (lambda row, _d=dupes: row["name"].lower() in _d))
    if macro == "type":
        return "1", [], None  # file-type descriptions are not indexed
    if macro in ("path", "wfp"):
        if not value:
            return "1", [], None
        if opts.regex:
            return "1", [], _make_regex_predicate(value, opts, force_path=True)
        if opts.match_whole_word or opts.match_diacritics:
            return "1", [], _make_name_predicate(value, opts, force_path=True)
        anchored = _has_wildcard(value) or macro == "wfp"
        return ("parent || name LIKE ? ESCAPE '\\'",
                [_like_pattern(value, anchored)], None)
    if macro in ("fn", "wfn"):
        if not value:
            return "1", [], None
        if opts.regex:
            return "1", [], _make_regex_predicate(value, opts, force_path=False)
        if opts.match_whole_word or opts.match_diacritics:
            return "1", [], _make_name_predicate(value, opts, force_path=False)
        anchored = _has_wildcard(value) or macro == "wfn"
        return ("name LIKE ? ESCAPE '\\'", [_like_pattern(value, anchored)], None)
    if macro == "stem":
        if not value:
            return "1", [], None
        if opts.regex or opts.match_whole_word or opts.match_diacritics:
            return "1", [], _make_name_predicate(value, opts, stem=True)
        expr = ("CASE WHEN extension = '' THEN name ELSE substr(name, 1,"
                " length(name) - length(extension) - 1) END")
        return ("%s LIKE ? ESCAPE '\\'" % expr,
                [_like_pattern(value, _has_wildcard(value))], None)
    if macro == "ext":
        return _ext_sql(value)
    if macro == "size":
        op, a, b = parse_size_expr(value)
        if op == "range":
            return "size BETWEEN ? AND ?", [a, b], None
        return {"eq": "size = ?", "gt": "size > ?", "lt": "size < ?",
                "ge": "size >= ?", "le": "size <= ?"}[op], [a], None
    if macro in _DATE_COLS:
        lo, hi = parse_date_expr(value)
        col = _DATE_COLS[macro]
        if lo is not None and hi is not None:
            if lo == hi:
                return "%s = ?" % col, [lo], None
            return "%s BETWEEN ? AND ?" % col, [lo, hi], None
        if lo is not None:
            return "%s >= ?" % col, [lo], None
        if hi is not None:
            return "%s <= ?" % col, [hi], None
        return "1", [], None
    if macro == "attrib":
        negate = value.startswith("<>")
        letters = value[2:] if negate else value
        mask = 0
        for ch in letters.upper():
            if ch not in ATTRIB_MAP:
                raise QueryError("invalid attribute: %r" % ch)
            mask |= ATTRIB_MAP[ch]
        if not mask:
            return "1", [], None
        if negate:
            return "NOT ((attributes & ?) = ?)", [mask, mask], None
        return "(attributes & ?) = ?", [mask, mask], None
    if macro == "depth":
        op, a = _parse_int_expr(value, "depth")
        return "1", [], _make_depth_predicate(op, a, opts)
    if macro == "regex":
        if not value:
            return "1", [], None
        return "1", [], _make_regex_predicate(value, opts)
    if macro == "content":
        if not value:
            return "1", [], None

        def content_pred(row, _terms=(value,)):
            if row["is_folder"]:
                return False
            return _content_match(row["parent"] + row["name"], _terms, opts)

        return "1", [], content_pred
    if macro in EXT_MACROS:
        exts = EXT_MACROS[macro]
        placeholders = ", ".join("?" * len(exts))
        sql = "lower(extension) IN (%s)" % placeholders
        params = list(exts)
        if value:
            sub_sql, sub_params, sub_pred = _term_sql(value, opts)
            if sub_pred is not None:
                return "1", [], _make_ext_predicate(exts, sub_pred)
            return "(%s AND %s)" % (sql, sub_sql), params + sub_params, None
        return sql, params, None

    # unknown macro -> literal text search
    if opts.regex:
        return "1", [], _make_regex_predicate(token, opts)
    target = "parent || name" if opts.match_path else "name"
    return ("%s LIKE ? ESCAPE '\\'" % target,
            [_like_pattern(token, _has_wildcard(token))], None)


def _ext_sql(value):
    if not value:
        return "1", [], None
    negate = value.startswith("<>")
    raw = value[2:] if negate else value
    exts = [e.strip().lstrip(".").lower() for e in raw.replace(",", ";").split(";")]
    exts = [e for e in exts if e]
    if not exts:
        return "1", [], None
    placeholders = ", ".join("?" * len(exts))
    if negate:
        return "lower(extension) NOT IN (%s)" % placeholders, list(exts), None
    return "lower(extension) IN (%s)" % placeholders, list(exts), None


# -- python predicates ------------------------------------------------------

def _make_regex_predicate(pattern, opts, force_path=None):
    try:
        rx = re.compile(pattern, 0 if opts.match_case else re.IGNORECASE)
    except re.error as exc:
        raise QueryError("invalid regular expression: %s" % exc)
    use_path = opts.match_path if force_path is None else force_path

    def pred(row):
        s = (row["parent"] + row["name"]) if use_path else row["name"]
        if opts.match_diacritics:
            s = normalize_diacritics(s)
        return rx.search(s) is not None

    return pred


def _make_name_predicate(term, opts, force_path=None, stem=False):
    """Plain-text term evaluated in Python (whole-word / diacritics modes)."""
    use_path = opts.match_path if force_path is None else force_path
    needle = normalize_diacritics(term) if opts.match_diacritics else term
    if not opts.match_case:
        needle = needle.lower()
    wild = _has_wildcard(term)
    rx = _wild_regex(needle) if wild else None

    def text_of(row):
        if stem:
            ext = row["extension"]
            name = row["name"]
            s = name[: len(name) - len(ext) - 1] if ext else name
        else:
            s = (row["parent"] + row["name"]) if use_path else row["name"]
        if opts.match_diacritics:
            s = normalize_diacritics(s)
        if not opts.match_case:
            s = s.lower()
        return s

    def pred(row):
        s = text_of(row)
        if opts.match_whole_word:
            words = _words(s)
            if rx is not None:
                return any(rx.match(w) for w in words)
            return needle in words
        if rx is not None:
            return rx.match(s) is not None
        return needle in s

    return pred


def _make_ext_predicate(exts, inner):
    extset = {e.lower() for e in exts}

    def pred(row):
        return row["extension"].lower() in extset and inner(row)

    return pred


def _make_depth_predicate(op, a, opts):
    roots = list(opts.roots)

    def depth_of(full):
        best = None
        for root in roots:
            if full == root or full == root.rstrip("\\"):
                return 0
            if full.startswith(root):
                d = full.count("\\") - root.count("\\") + 1
                if best is None or d < best:
                    best = d
        return best

    def pred(row):
        d = depth_of(row["parent"] + row["name"])
        if d is None:
            return False
        return _cmp_value(op, d, a)

    return pred


def _node_sql(node, opts):
    """Compile AST -> (sql, params, predicate)."""
    if node is None:
        return "1", [], None
    if isinstance(node, _Term):
        return _term_sql(node.token, opts)
    if isinstance(node, _Not):
        sql, params, pred = _node_sql(node.child, opts)
        if pred is not None:
            child_pred = _node_predicate(node.child, opts)
            return "1", [], (lambda row, _p=child_pred: not _p(row))
        return "NOT (%s)" % sql, params, None
    if isinstance(node, _And):
        sqls, params, preds = [], [], []
        for child in node.children:
            s, p, pred = _node_sql(child, opts)
            sqls.append("(%s)" % s)
            params.extend(p)
            if pred is not None:
                preds.append(pred)
        if preds:
            if len(preds) == 1:
                return " AND ".join(sqls), params, preds[0]
            return (" AND ".join(sqls), params,
                    lambda row, _ps=tuple(preds): all(p(row) for p in _ps))
        return " AND ".join(sqls), params, None
    if isinstance(node, _Or):
        sqls, params, preds = [], [], []
        for child in node.children:
            s, p, pred = _node_sql(child, opts)
            sqls.append("(%s)" % s)
            params.extend(p)
            if pred is not None:
                preds.append(pred)
        if preds:
            # an OR that mixes SQL and predicate children must be fully
            # evaluated in Python
            return "1", [], _node_predicate(node, opts)
        return " OR ".join(sqls), params, None
    raise QueryError("internal: unknown AST node")


def _node_predicate(node, opts):
    """Compile AST -> pure Python predicate (slow path)."""
    if node is None:
        return lambda row: True
    if isinstance(node, _Term):
        _, _, pred = _term_sql(node.token, opts)
        return pred if pred is not None else _sql_term_as_predicate(node.token, opts)
    if isinstance(node, _Not):
        inner = _node_predicate(node.child, opts)
        return lambda row, _p=inner: not _p(row)
    if isinstance(node, _And):
        preds = [_node_predicate(c, opts) for c in node.children]
        return lambda row, _ps=tuple(preds): all(p(row) for p in _ps)
    if isinstance(node, _Or):
        preds = [_node_predicate(c, opts) for c in node.children]
        return lambda row, _ps=tuple(preds): any(p(row) for p in _ps)
    raise QueryError("internal: unknown AST node")


def _sql_term_as_predicate(token, opts):
    """Row-level predicate for terms that normally compile to pure SQL."""
    macro, value = _split_macro(token)

    if macro == "folder":
        return lambda row: bool(row["is_folder"])
    if macro == "file":
        return lambda row: not row["is_folder"]
    if macro == "type":
        return lambda row: True
    if macro == "ext":
        negate = value.startswith("<>")
        raw = value[2:] if negate else value
        exts = {e.strip().lstrip(".").lower()
                for e in raw.replace(",", ";").split(";") if e.strip()}
        if negate:
            return lambda row: row["extension"].lower() not in exts
        return lambda row: row["extension"].lower() in exts
    if macro in EXT_MACROS:
        extset = {e.lower() for e in EXT_MACROS[macro]}
        base = (lambda row: row["extension"].lower() in extset)
        if value:
            inner = _node_predicate(_Term(value), opts)
            return lambda row: base(row) and inner(row)
        return base
    if macro == "size":
        op, a, b = parse_size_expr(value)
        return lambda row: _cmp_value(op, row["size"], b if op == "range" else a)
    if macro in _DATE_COLS:
        lo, hi = parse_date_expr(value)
        col = _DATE_COLS[macro]

        def date_pred(row, _col=col, _lo=lo, _hi=hi):
            v = row[_col]
            if v is None:
                return False
            if _lo is not None and v < _lo:
                return False
            if _hi is not None and v > _hi:
                return False
            return True

        return date_pred
    if macro == "attrib":
        negate = value.startswith("<>")
        letters = value[2:] if negate else value
        mask = 0
        for ch in letters.upper():
            if ch not in ATTRIB_MAP:
                raise QueryError("invalid attribute: %r" % ch)
            mask |= ATTRIB_MAP[ch]
        if not mask:
            return lambda row: True
        if negate:
            return lambda row: (row["attributes"] & mask) != mask
        return lambda row: (row["attributes"] & mask) == mask
    if macro == "dupe":
        dupes = opts.dupes or set()
        return lambda row: row["name"].lower() in dupes
    if macro in ("path", "wfp"):
        anchored = _has_wildcard(value) or macro == "wfp"
        rx = _wild_regex(value) if _has_wildcard(value) else None
        needle = normalize_diacritics(value) if opts.match_diacritics else value
        if not opts.match_case:
            needle = needle.lower()

        def path_pred(row, _rx=rx, _anchored=anchored, _needle=needle):
            s = row["parent"] + row["name"]
            if opts.match_diacritics:
                s = normalize_diacritics(s)
            if not opts.match_case:
                s = s.lower()
            if opts.match_whole_word:
                words = _words(s)
                if _rx is not None:
                    return any(_rx.match(w) for w in words)
                return _needle in words
            if _rx is not None:
                return _rx.match(s) is not None
            if _anchored:
                return s == _needle
            return _needle in s

        return path_pred
    if macro in ("fn", "wfn"):
        anchored = _has_wildcard(value) or macro == "wfn"
        rx = _wild_regex(value) if _has_wildcard(value) else None
        needle = normalize_diacritics(value) if opts.match_diacritics else value
        if not opts.match_case:
            needle = needle.lower()

        def name_pred(row, _rx=rx, _anchored=anchored, _needle=needle):
            s = row["name"]
            if opts.match_diacritics:
                s = normalize_diacritics(s)
            if not opts.match_case:
                s = s.lower()
            if opts.match_whole_word:
                words = _words(s)
                if _rx is not None:
                    return any(_rx.match(w) for w in words)
                return _needle in words
            if _rx is not None:
                return _rx.match(s) is not None
            if _anchored:
                return s == _needle
            return _needle in s

        return name_pred
    if macro == "stem":
        return _make_name_predicate(value, opts, stem=True)

    # plain term
    if opts.regex:
        return _make_regex_predicate(token, opts)
    return _make_name_predicate(token, opts)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def compile_query(query, opts):
    """Parse *query* and compile it against *opts*."""
    compiled = CompiledQuery()
    compiled.limit = opts.max_results
    compiled.query = query or ""
    tokens = tokenize(query or "")
    sort_key, ascending, tokens = _extract_sort(tokens)
    if sort_key is not None:
        opts.sort, opts.sort_ascending = sort_key, ascending
    compiled.sort_key = opts.sort
    compiled.sort_ascending = opts.sort_ascending
    compiled.order_sql = _order_sql(opts)

    ast = parse(tokens)
    sql, params, pred = _node_sql(ast, opts)
    compiled.where = sql
    compiled.params = params
    compiled.predicate = pred
    return compiled


def _needs_dupes(query):
    for tok in tokenize(query or ""):
        if _split_macro(tok)[0] == "dupe":
            return True
    return False


def run_search(index, query, opts, on_batch=None, stop=None):
    """Execute *query*; streams result batches through *on_batch*."""
    result = SearchResult()
    result.query = query or ""
    started = time.time()

    if _needs_dupes(query) and opts.dupes is None:
        opts.dupes = {r[0] for r in index.conn.execute(
            "SELECT lower(name) FROM files GROUP BY lower(name)"
            " HAVING COUNT(*) > 1")}

    compiled = compile_query(query, opts)
    index.set_case_sensitive_like(opts.match_case)

    base_sql = ("SELECT name, parent, is_folder, size, date_modified,"
                " date_created, date_accessed, attributes, extension"
                " FROM files WHERE %s ORDER BY %s")
    batch = []

    def flush(force=False):
        nonlocal batch
        if batch and (force or len(batch) >= 200):
            if on_batch is not None:
                on_batch(list(batch))
            result.rows.extend(batch)
            result.total_size += sum(r["size"] for r in batch if not r["is_folder"])
            batch = []

    if compiled.predicate is not None:
        # Python path: stream the whole index, stop at max_results.
        cur = index.conn.execute(base_sql % (compiled.where, compiled.order_sql),
                                 list(compiled.params))
        while True:
            if stop is not None and stop.is_set():
                break
            rows = cur.fetchmany(400)
            if not rows:
                break
            for r in rows:
                row = dict(zip(_COLS, tuple(r)))
                if compiled.predicate(row):
                    batch.append(row)
                    if len(result.rows) + len(batch) >= opts.max_results:
                        result.truncated = True
                        break
            flush()
            if result.truncated:
                break
    else:
        # SQL fast path.
        sql = base_sql % (compiled.where, compiled.order_sql)
        cur = index.conn.execute(sql + " LIMIT ?",
                                 list(compiled.params) + [opts.max_results + 1])
        rows = cur.fetchall()
        result.truncated = len(rows) > opts.max_results
        for r in rows[: opts.max_results]:
            batch.append(dict(zip(_COLS, tuple(r))))
            flush()
    flush(force=True)
    result.took = time.time() - started
    return result


def combine_query(base, extra):
    """AND two query strings together (used by filters)."""
    base = (base or "").strip()
    extra = (extra or "").strip()
    if base and extra:
        return base + " " + extra
    return base or extra
