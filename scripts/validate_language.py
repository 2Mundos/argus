#!/usr/bin/env python3
"""English-only prose gate for new changes.

Scope (diff-based, so legacy text never fails the gate):
  * Markdown docs: added prose lines.
  * Code: added comment/docstring lines (.py via ``tokenize``; .ts/.tsx/.js/
    .jsx/.mjs/.cjs/.sh/.yaml/.yml/Dockerfile via comment-prefix detection,
    .sql via ``--`` comments). String literals are NOT checked: they hold UI
    copy, fixtures and language data (see
    docs/specs/language-validation.spec.md).
  * Commit messages: subject + body of each new non-merge commit
    (conventional-commit prefix, trailers, URLs, code spans and refs stripped).

Detection is stdlib-only and deterministic (no model downloads, so it runs in
the docs-validation CI job and in git hooks without a venv). It targets
Portuguese, the most likely non-English source, plus generic Latin diacritics
and small Spanish/French/German/Italian stopword sets. ``langdetect``/
``langid``/``lingua`` were evaluated and rejected as the default:
``langdetect`` is non-deterministic and needs long text, ``lingua`` ships
~100MB models, and all three add a dependency to jobs that currently run on
plain ``python3``.

Usage:
  python3 scripts/validate_language.py [--range A..B ...] [--staged]
      [--message-file F] [PATH ...]

  No source flags  -> added worktree lines vs HEAD (tracked + untracked).
  --range          -> commit messages in range + added lines of the diff
                      (repeatable; endpoint blob via ``git show``).
  --staged         -> added staged lines only.
  --message-file   -> check one commit-message file (commit-msg hook).
  PATH ...         -> restrict file checking to these paths (diff modes).

Exit 0 when everything is English, 1 with a violation list otherwise.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# Language evidence
# --------------------------------------------------------------------------

# Words that are fine in English despite carrying diacritics (loanwords and
# common proper nouns). Everything else with a Latin diacritic counts as
# non-English evidence.
LOANWORDS = {
    "café",
    "cafés",
    "naïve",
    "naïveté",
    "résumé",
    "résumés",
    "exposé",
    "exposés",
    "cliché",
    "clichés",
    "fiancé",
    "fiancée",
    "façade",
    "façades",
    "sauté",
    "crème",
    "brûlée",
    "déjà",
    "tête",
    "être",
    "über",
    "doppelgänger",
    "piñata",
    "jalapeño",
    "jalapeños",
    "são",
    "paulo",
    "beyoncé",
    "pokémon",
    "montréal",
    "brasília",
    "maceió",
    "belém",
    "joão",
    "luís",
    "paraná",
    "ceará",
    "pará",
    "paraíba",
    "maranhão",
    "piauí",
    "amapá",
    "goiás",
    "rondônia",
    "florianópolis",
    "zürich",
    "münchen",
    "bogotá",
    "malmö",
}

# Stopwords per language. Words that are common English on their own (`die`,
# `son`, `con`, `plus`, `par`, `sur`, `est`, `den`, `dem`, `num`, `come`)
# are deliberately absent. The short Portuguese words `de`, `da`, `do`,
# `das`, `dos` also occur in English, so they are weak evidence (see
# WEAK_STOPWORDS): they count toward the hit total but a segment needs at
# least one non-weak hit (plus the minimum word count) before it can be
# classified, so English text that happens to pair them ("Do not
# de-duplicate events") cannot fail the gate. A capitalized word next to
# another capitalized word (a proper-noun phrase) supplies no evidence at
# all either, so isolated collisions ("Los Angeles", "Chloë will do it")
# cannot fail the gate.
# (Names above are backtick-quoted so this comment passes the gate itself.)
STOPWORDS = {
    "pt": {
        "para",
        "com",
        "uma",
        "mais",
        "como",
        "porque",
        "sobre",
        "entre",
        "muito",
        "quando",
        "onde",
        "qual",
        "quais",
        "cada",
        "todos",
        "todas",
        "esta",
        "este",
        "estes",
        "estas",
        "esse",
        "essa",
        "esses",
        "essas",
        "isso",
        "aquele",
        "aquela",
        "depois",
        "antes",
        "durante",
        "pelos",
        "pelas",
        "pelo",
        "pela",
        "numa",
        "nisso",
        "disso",
        "desse",
        "desta",
        "deste",
        "foram",
        "seja",
        "sejam",
        "sendo",
        "tendo",
        "fazer",
        "pode",
        "sem",
        "aos",
        "foi",
        "tinha",
        "tem",
        "assinante",
        "assinantes",
        "conta",
        "contas",
        "custo",
        "custos",
        "filtro",
        "filtros",
        "compartilhado",
        "compartilhada",
        "compartilhados",
        "registro",
        "registros",
        "tela",
        "telas",
        "leitura",
        "rascunho",
        "rascunhos",
        "revisao",
        "correcao",
        "manutencao",
        "versao",
        "versoes",
        "operacao",
        "configuracao",
        "migracao",
        # Frequent unaccented spellings of the accented words above.
        "nao",
        "pagina",
        "paginas",
        "relatorio",
        "relatorios",
        "unico",
        "unica",
        "voce",
        "voces",
        "estao",
        "sao",
        "tambem",
        "alem",
        "decisao",
        "evidencia",
        "cenario",
        "de",
        "da",
        "do",
        "das",
        "dos",
        "não",
        "são",
        "está",
        "estão",
        "você",
        "vocês",
        "então",
        "além",
        "até",
        "também",
        "atrás",
    },
    "es": {
        "el",
        "los",
        "las",
        "del",
        "hay",
        "pero",
        "muy",
        "sin",
        "donde",
        "estos",
        "estas",
        "estan",
        "tiene",
        "hace",
    },
    "fr": {
        "le",
        "les",
        "des",
        "une",
        "sont",
        "pas",
        "avec",
        "pour",
        "dans",
        "qui",
        "cette",
        "aux",
        "aussi",
    },
    "de": {
        "der",
        "das",
        "und",
        "nicht",
        "mit",
        "von",
        "ein",
        "eine",
        "ist",
        "sind",
        "auch",
        "als",
        "bei",
        "nach",
        "keine",
        "zwischen",
    },
    "it": {
        "che",
        "della",
        "dello",
        "sono",
        "siamo",
        "questa",
        "questo",
        "senza",
        "dopo",
        "sopra",
        "abbiamo",
        "nella",
    },
}

MIN_STOPWORD_WORDS = 4
MIN_STOPWORD_HITS = 2

# Stopwords that also occur in ordinary English. They count toward the hit
# total for recall, but a segment needs at least one hit outside this set
# before it can be classified, so "Do not de-duplicate events" (which yields
# only `do`, or `do` + `de`) stays English.
WEAK_STOPWORDS = {"de", "da", "do", "das", "dos"}

DIACRITIC_RE = re.compile(r"[àáâãäåçèéêëìíîïñòóôõöùúûüýÿæœç¡¿]", re.IGNORECASE)
WORD_RE = re.compile(r"[A-Za-zÀ-ÿ']+")
URL_RE = re.compile(r"https?://\S+|www\.\S+|[\w.%+-]+@[\w.-]+\.\w+")
CODE_SPAN_RE = re.compile(r"`[^`]*`")
REF_RE = re.compile(r"\(#\d+\)|#\d+")
HEX_RE = re.compile(r"\b[0-9a-f]{7,64}\b")
VERSION_RE = re.compile(r"\bv?\d+\.\d+[\d.]*\b")
DOMAIN_RE = re.compile(r"\b[\w-]+\.(?:com|org|io|dev|net|br|app)\b", re.IGNORECASE)
COMPOUND_RE = re.compile(r"[A-Za-zÀ-ÿ]+(?:-[A-Za-zÀ-ÿ]+)+")
# Latin loan phrases common in English technical prose. Like the diacritic
# loanwords, they are stripped before detection so "de" in "de facto" (or
# "vice versa", "per se") cannot supply a Portuguese hit.
LATIN_PHRASES = [
    "de facto",
    "de jure",
    "per se",
    "ad hoc",
    "et al",
    "vice versa",
    "inter alia",
    "in vivo",
    "in vitro",
    "a priori",
    "a posteriori",
]
LATIN_RE = re.compile(rf"\b(?:{'|'.join(LATIN_PHRASES)})\b\.?", re.IGNORECASE)


def clean_segment(text: str) -> str:
    """Strip URLs, code spans, refs and hashes from a prose segment."""
    text = URL_RE.sub(" ", text)
    text = CODE_SPAN_RE.sub(" ", text)
    text = DOMAIN_RE.sub(" ", text)
    text = LATIN_RE.sub(" ", text)
    text = REF_RE.sub(" ", text)
    text = HEX_RE.sub(" ", text)
    text = VERSION_RE.sub(" ", text)
    return text


def detect_languages(text: str) -> set[str]:
    """Return the set of non-English language keys evidenced in ``text``."""
    text = clean_segment(text)
    raw_words = [w for w in WORD_RE.findall(text) if len(w) >= 2]
    words = [w.lower() for w in raw_words]

    # Acronym-cased tokens (all-caps like "DOS", or inner capitals like
    # "DoS" and "eBay") are identifiers or initialisms, not prose, and
    # supply no evidence -- otherwise "DoS" lowercases into evidence and
    # pairs with "do". Fragments of hyphenated compounds ("de-duplicate",
    # "re-do") supply no evidence either: only whole words count, so an
    # English compound cannot smuggle in a stopword fragment. A capitalized
    # word next to another capitalized word is treated as part of a
    # proper-noun phrase ("Los Angeles", "São Paulo") and likewise supplies
    # no evidence. All other words match case-insensitively, so
    # sentence-initial words ("Não responda ...") still match.
    def is_acronym(w: str) -> bool:
        return w.isupper() or w[1:] != w[1:].lower()

    fragments = {
        frag.lower()
        for compound in COMPOUND_RE.findall(text)
        for frag in compound.split("-")
    }
    evidence = [
        w.lower()
        for i, w in enumerate(raw_words)
        if w.lower() not in fragments
        and not is_acronym(w)
        and not (
            w[0].isupper()
            and (
                (i > 0 and raw_words[i - 1][0].isupper())
                or (i + 1 < len(raw_words) and raw_words[i + 1][0].isupper())
            )
        )
    ]
    if len(words) < MIN_STOPWORD_WORDS and not DIACRITIC_RE.search(text):
        return set()
    found: set[str] = set()
    for lang, stopwords in STOPWORDS.items():
        hits = {w for w in evidence if w in stopwords}
        if (
            len(hits) >= MIN_STOPWORD_HITS
            and hits - WEAK_STOPWORDS
            and len(words) >= MIN_STOPWORD_WORDS
        ):
            found.add(lang)
    diacritic_words = {
        w for w in evidence if DIACRITIC_RE.search(w) and w not in LOANWORDS
    }
    if len(diacritic_words) >= 2:
        found.add("diacritic")
    elif len(diacritic_words) == 1:
        # A single diacritic word is often a proper noun (Chloë), so it needs
        # the same two-distinct-hit stopword evidence as the stopword branch
        # -- one short word such as `do` next to it is not enough -- and at
        # least one of those hits must be strong (not in WEAK_STOPWORDS).
        pt_hits = {w for w in evidence if w in STOPWORDS["pt"]}
        if (
            len(pt_hits) >= MIN_STOPWORD_HITS
            and pt_hits - WEAK_STOPWORDS
            and len(words) >= MIN_STOPWORD_WORDS
        ):
            found.add("pt")
    # Single diacritic word with no other evidence: likely a proper noun
    # (São Paulo, Beyoncé missed by the allowlist) -- pass.
    return found


# --------------------------------------------------------------------------
# Violations
# --------------------------------------------------------------------------


@dataclass
class Violation:
    location: str
    excerpt: str
    languages: set[str] = field(default_factory=set[str])

    def format(self) -> str:
        langs = ",".join(sorted(self.languages))
        excerpt = self.excerpt.strip()
        if len(excerpt) > 160:
            excerpt = excerpt[:157] + "..."
        return f"❌ [{langs}] {self.location}: {excerpt}"


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------


def git(*args: str) -> str:
    proc = subprocess.run(
        # quotePath=false: non-ASCII paths come through as raw UTF-8 instead
        # of C-style quoted ("b/..."), so diff headers, blobs and file lists
        # stay in sync and accented filenames are still checked.
        ["git", "-c", "core.quotePath=false", *args],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def parse_added_lines(diff: str) -> dict[str, set[int]]:
    """Map file path -> added new-file line numbers from a -U0 diff.

    Only '+' lines (added) and ' ' lines (context) advance the new-file
    counter. Removed lines -- including removed content starting with '--'
    (YAML/Markdown front matter, SQL '-- comment') -- and no-newline
    markers never advance it, and added content starting with '++' is still
    recorded. File headers are only honored outside hunks, so content
    lines can never be mistaken for them.
    """
    added: dict[str, set[int]] = {}
    path: str | None = None
    in_hunk = False
    new_line = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = None
            in_hunk = False
        elif not in_hunk and line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                path = None
            else:
                path = target[2:] if target.startswith("b/") else target
                added.setdefault(path, set())
        elif line.startswith("@@ ") and path is not None:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            new_line = int(m.group(1)) if m else 0
            in_hunk = True
        elif in_hunk and path is not None:
            if line.startswith("+"):
                added[path].add(new_line)
                new_line += 1
            elif line.startswith(" "):
                new_line += 1
            # '-' (removed), '\' (no-newline marker) and anything else:
            # never advance the new-file counter.
    return {p: lines for p, lines in added.items() if lines}


def show_blob(rev: str, path: str) -> str | None:
    try:
        return git("show", f"{rev}:{path}")
    except RuntimeError:
        return None


# --------------------------------------------------------------------------
# Comment extraction (code files)
# --------------------------------------------------------------------------

PY_EXTS = {".py"}
C_STYLE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
HASH_COMMENT_FILES = {".sh", ".yaml", ".yml"}
HASH_COMMENT_NAMES = {"Dockerfile"}
SQL_EXTS = {".sql"}
MD_EXTS = {".md", ".mdx"}


def target_kind(path: str) -> str | None:
    name = Path(path).name
    suffix = Path(path).suffix.lower()
    if suffix in MD_EXTS or name == "DESIGN.md":
        return "docs"
    if suffix in PY_EXTS:
        return "py"
    if suffix in C_STYLE_EXTS:
        return "cstyle"
    if suffix in SQL_EXTS:
        return "sql"
    if (
        suffix in HASH_COMMENT_FILES
        or name in HASH_COMMENT_NAMES
        or name.startswith("Dockerfile")
    ):
        return "hash"
    return None


def python_docstring_lines(source: str) -> set[int]:
    """Line spans of real docstrings: the first statement of a module, class,
    or function body when it is a string literal. Other string literals
    (product copy, language data) are out of scope and never included."""
    lines: set[int] = set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return lines
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        value = node.body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            start = node.body[0].lineno
            lines.update(range(start, (node.body[0].end_lineno or start) + 1))
    return lines


def python_comment_lines(source: str) -> set[int]:
    """Line numbers of COMMENT tokens and AST-verified docstrings."""
    return set(python_comment_tokens(source)) | python_docstring_lines(source)


def cstyle_comment_lines(source: str) -> set[int]:
    lines: set[int] = set()
    in_block = False
    for i, raw in enumerate(source.splitlines(), start=1):
        stripped = raw.strip()
        if in_block:
            lines.add(i)
            if "*/" in stripped:
                in_block = False
        elif stripped.startswith("//"):
            lines.add(i)
        elif stripped.startswith("/*"):
            lines.add(i)
            if "/*" in stripped and "*/" not in stripped:
                in_block = True
    return lines


# A trailing comment needs whitespace before its marker, so "https://x" and
# "a--b" never match. Strings containing " // " are rare and need two
# stopword hits to fail, so the false-positive cost is accepted.
INLINE_COMMENT_RES = {
    "cstyle": re.compile(r"\s//\s*(.*)$"),
    "sql": re.compile(r"\s--\s*(.*)$"),
    "hash": re.compile(r"\s#\s*(.*)$"),
}


def inline_comment(line: str, kind: str) -> str | None:
    """Text of a trailing comment on a code line, or None."""
    pattern = INLINE_COMMENT_RES.get(kind)
    match = pattern.search(line) if pattern else None
    return match.group(1) if match else None


def python_comment_tokens(source: str) -> dict[int, str]:
    """Line number -> text of each COMMENT token (own-line or trailing)."""
    comments: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0]] = tok.string
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return comments


def hash_comment_lines(source: str, added: set[int]) -> set[int]:
    return {n for n in added if source_line(source, n).lstrip().startswith("#")}


def sql_comment_lines(source: str) -> set[int]:
    return {
        i
        for i, raw in enumerate(source.splitlines(), start=1)
        if raw.lstrip().startswith("--")
    }


def source_line(source: str, n: int) -> str:
    lines = source.splitlines()
    return lines[n - 1] if 1 <= n <= len(lines) else ""


def clean_comment(text: str, kind: str) -> str:
    text = text.strip()
    if kind == "py":
        if text.startswith("#"):
            text = text[1:]
        text = text.strip('"').strip("'").strip()
    elif kind == "cstyle":
        text = re.sub(r"^/+", "", text)
        text = re.sub(r"^/\*+|\*+/$", "", text).strip()
        text = text.lstrip("*").strip()
    elif kind == "sql":
        text = re.sub(r"^-+", "", text).strip()
    elif kind == "hash":
        text = text.lstrip("#").strip()
        text = re.sub(r"^/+", "", text).strip()
    return text


def clean_markdown_line(text: str) -> str:
    text = re.sub(r"^\s{0,3}#{1,6}\s+", " ", text)  # headings
    text = re.sub(r"^\s{0,3}>\s?", " ", text)  # blockquotes
    text = re.sub(r"^\s*[-*+]\s+\[[ xX]\]\s+", " ", text)  # task lists
    text = re.sub(r"^\s*[-*+]\s+", " ", text)  # bullets
    text = re.sub(r"^\s*\d+[.)]\s+", " ", text)  # ordered lists
    text = re.sub(r"^\s*\|", " ", text).replace("|", " ")  # tables
    text = re.sub(r"<[^>]+>", " ", text)  # HTML tags
    return text


def check_file(path: str, added: set[int], blob: str | None) -> list[Violation]:
    kind = target_kind(path)
    if kind is None or not added:
        return []
    violations: list[Violation] = []
    if kind == "docs":
        if blob is None:
            return []
        lines = blob.splitlines()
        # Fence state must be tracked over the full blob: a fence opened or
        # closed by lines outside the diff is invisible when iterating only
        # the added lines.
        prose: set[int] = set()
        in_fence = False
        for i, raw in enumerate(lines, start=1):
            if raw.strip().startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if not in_fence:
                prose.add(i)
        for n in sorted(added & prose):
            raw = lines[n - 1]
            text = clean_markdown_line(raw)
            langs = detect_languages(text)
            if langs:
                violations.append(Violation(f"{path}:{n}", raw, langs))
        return violations
    if blob is None:
        # File deleted or unreadable: fall back to prefix matching on the
        # diff lines themselves (handled by caller via blob=None -> skip).
        return []
    lines = blob.splitlines()
    texts: dict[int, str] = {}
    if kind == "py":
        # A comment token's own text only, so code or string literals sharing
        # the line are never checked; docstrings are whole lines.
        tokens = python_comment_tokens(blob)
        for n in added & python_docstring_lines(blob):
            texts[n] = clean_comment(source_line(blob, n), kind)
        for n in added & set(tokens):
            texts[n] = clean_comment(tokens[n], kind)
    else:
        if kind == "cstyle":
            comment_lines = cstyle_comment_lines(blob)
        elif kind == "sql":
            comment_lines = sql_comment_lines(blob)
        else:  # hash
            comment_lines = hash_comment_lines(blob, added)
        for n in added:
            line = source_line(blob, n)
            if n in comment_lines:
                texts[n] = clean_comment(line, kind)
            elif (trailing := inline_comment(line, kind)) is not None:
                texts[n] = trailing
    for n in sorted(texts):
        langs = detect_languages(texts[n])
        if langs:
            violations.append(Violation(f"{path}:{n}", lines[n - 1].strip(), langs))
    return violations


# --------------------------------------------------------------------------
# Commit messages
# --------------------------------------------------------------------------

CONVENTIONAL_RE = re.compile(r"^[a-z]+(\([^)]*\))?!?:\s*")
TRAILER_RE = re.compile(
    r"^(refs|fixes|closes|co-authored-by|reviewed-by|signed-off-by|ack|#)\b",
    re.IGNORECASE,
)


SCISSORS_RE = re.compile(r"^# -+ >8 -+$")


def clean_commit_message(raw: str) -> str:
    segments: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if SCISSORS_RE.match(line):
            break  # `git commit -v` appends the diff below this line
        if not line or line.startswith("#"):
            continue
        if TRAILER_RE.match(line):
            continue
        line = CONVENTIONAL_RE.sub("", line)
        segments.append(line)
    return "\n".join(segments)


def commit_subjects(range_: str) -> list[tuple[str, str]]:
    """Return (sha, full message) for non-merge commits in range."""
    out = git("log", "--no-merges", "--format=%H%x00%B%x1e", range_)
    commits = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk or "\x00" not in chunk:
            continue
        sha, message = chunk.split("\x00", 1)
        if message.strip():
            commits.append((sha, message))
    return commits


def check_message(sha: str, raw: str) -> list[Violation]:
    text = clean_commit_message(raw)
    violations: list[Violation] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        langs = detect_languages(line)
        if langs:
            violations.append(Violation(f"commit {sha[:12]}:L{i}", line.strip(), langs))
    return violations


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def split_range(range_: str) -> tuple[str, str, str]:
    """Split a revision range into (head, log_range, diff_range).

    Commit messages use the two-dot range (commits reachable from head but
    not base). The file diff uses the three-dot range
    (diff of merge-base against head): with a two-dot diff, a line the base
    branch removed or rewrote after the PR branched would reappear as an
    added '+' line, and its legacy text would fail the gate even though the
    PR never added it.
    """
    if "..." in range_:
        base, head = range_.split("...", 1)
    else:
        base, head = range_.split("..", 1)
    head = head.split(":")[0]
    return head, f"{base}..{head}", f"{base}...{head}"


def collect_range(
    range_: str, only: set[str]
) -> tuple[list[Violation], list[Violation]]:
    head, log_range, diff_range = split_range(range_)
    commits = commit_subjects(log_range)
    msg_violations: list[Violation] = []
    for sha, message in commits:
        msg_violations.extend(check_message(sha, message))
    # Resolve the head strictly: falling back to HEAD here would let the
    # diff range and the blob source disagree (a push whose head is not the
    # checked-out commit), silently checking the wrong text. An unresolvable
    # head is already a failure the diff would hit, so fail closed.
    try:
        head = git("rev-parse", head).strip()
    except RuntimeError as exc:
        raise RuntimeError(f"cannot resolve head {head!r}: {exc}") from exc
    if not head:
        raise RuntimeError("cannot resolve head: empty revision")
    diff = git("diff", "-U0", "--no-color", "--no-ext-diff", diff_range, "--")
    added = parse_added_lines(diff)
    file_violations: list[Violation] = []
    for path, lines in added.items():
        if only and path not in only:
            continue
        if target_kind(path) is None:
            continue
        blob = show_blob(head, path)
        if blob is None and target_kind(path) == "docs":
            continue
        file_violations.extend(check_file(path, lines, blob))
    return msg_violations, file_violations


def collect_worktree(staged: bool, only: set[str]) -> list[Violation]:
    violations: list[Violation] = []
    if staged:
        diff = git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--")
        added = parse_added_lines(diff)
        for path, lines in added.items():
            if only and path not in only:
                continue
            if target_kind(path) is None:
                continue
            try:
                blob = git("show", f":{path}")
            except RuntimeError:
                blob = None
            if blob is None and target_kind(path) == "docs":
                continue
            violations.extend(check_file(path, lines, blob))
        return violations
    # Worktree: tracked diff vs HEAD plus untracked files (checked in full).
    diff = git("diff", "HEAD", "-U0", "--no-color", "--no-ext-diff", "--")
    added = parse_added_lines(diff)
    for path, lines in added.items():
        if only and path not in only:
            continue
        if target_kind(path) is None:
            continue
        p = ROOT / path
        blob = p.read_text(encoding="utf-8", errors="ignore") if p.is_file() else None
        if blob is None and target_kind(path) == "docs":
            continue
        violations.extend(check_file(path, lines, blob))
    others = git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
    for path in others:
        if not path or (only and path not in only):
            continue
        kind = target_kind(path)
        if kind is None:
            continue
        p = ROOT / path
        if not p.is_file():
            continue
        blob = p.read_text(encoding="utf-8", errors="ignore")
        lines = set(range(1, len(blob.splitlines()) + 1))
        violations.extend(check_file(path, lines, blob))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail on non-English prose in new changes."
    )
    parser.add_argument(
        "--range",
        dest="ranges",
        action="append",
        default=[],
        help="git revision range (repeatable)",
    )
    parser.add_argument(
        "--staged", action="store_true", help="check staged changes only"
    )
    parser.add_argument(
        "--message-file", default=None, help="check one commit-message file"
    )
    parser.add_argument(
        "paths", nargs="*", help="restrict file checking to these paths"
    )
    args = parser.parse_args(argv)

    violations: list[Violation] = []
    range_failed = False
    if args.message_file:
        raw = Path(args.message_file).read_text(encoding="utf-8", errors="ignore")
        violations.extend(check_message("staged", raw))
    elif args.ranges:
        only = set(args.paths)
        for range_ in args.ranges:
            try:
                msg_v, file_v = collect_range(range_, only)
            except RuntimeError as exc:
                # Fail closed: an unreadable range (missing/unfetched base
                # SHA, unresolvable revision) means nothing was checked, so
                # the gate must not pass silently.
                print(f"❌ Cannot check range {range_}: {exc}")
                range_failed = True
                continue
            violations.extend(msg_v)
            violations.extend(file_v)
    else:
        violations.extend(collect_worktree(args.staged, set(args.paths)))

    for v in violations:
        print(v.format())
    if range_failed:
        print(
            "\n❌ One or more ranges could not be read; nothing was checked "
            "for them. Failing closed: fetch the base / fix the range and "
            "re-run."
        )
        return 1
    if violations:
        print(
            f"\n❌ {len(violations)} non-English segment(s) found. "
            "Rewrite them in English; product copy and language data in string "
            "literals are out of scope (see docs/specs/language-validation.spec.md)."
        )
        return 1
    print("✅ All new prose is English.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
