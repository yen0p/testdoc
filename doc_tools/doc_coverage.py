#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Documentation coverage (C): functions + structs + enums + global variables (headers).
- Scans .c/.h
- Functions: counts definitions (must open '{')
- Headers: counts struct/enum with bodies, and global variables (extern/static/etc.)
- An item is "documented" if a Doxygen block (/** ... */ or /*! ... */) or lines (/// or //!) appear
  above it with no real code in between, within a lookback window.
- Outputs:
  - Console: one line per file
  - MkDocs Markdown page with tables grouped by subfolder (no compact view)

Usage:
  python3 tools/doc_coverage.py --src src include --out mkdocs/docs/status.md

Options:
  --lookback N        number of lines to look above items for docs (default 12)
  --no-globals        ignore global variables in coverage
  --no-structs        ignore struct coverage
  --no-enums          ignore enum coverage
  --only-headers      analyze only headers for non-function items
"""

import re
import argparse
from pathlib import Path
from typing import List, Tuple, Dict, Iterable

# ----------------- Defaults -----------------
DEFAULT_SRC_DIRS = ["src", "include"]
DEFAULT_EXTS = (".c", ".h")
LOOKBACK_LINES = 12

# ----------------- Regexes -----------------
# Function definitions (must open a block '{'); tolerant to qualifiers/attributes/line breaks.
FUNC_DEF_RE = re.compile(
    r"""
    ^                                   # line start
    (?:[A-Za-z_][\w\s\*\(\),]*?)        # return type / qualifiers
    \b([A-Za-z_]\w*)\s*                 # function name (group 1)
    \(
        (?:[^;{}]|\n)*?                 # params (avoid ';' and '{' to skip prototypes)
    \)
    (?:\s*__attribute__\s*\(\([^)]+\)\))*  # optional attributes
    \s*\{                               # body starts
    """,
    re.MULTILINE | re.VERBOSE
)

# struct with body (either 'typedef struct {...} Name;' or 'struct Name {...};')
TYPEDEF_STRUCT_RE = re.compile(
    r"""typedef\s+struct\s*(?:[A-Za-z_]\w*\s*)?\{[\s\S]*?\}\s*([A-Za-z_]\w*)\s*;""",
    re.MULTILINE | re.VERBOSE
)
STRUCT_NAMED_RE = re.compile(
    r"""^\s*struct\s+([A-Za-z_]\w*)\s*\{[\s\S]*?\}\s*;""",
    re.MULTILINE | re.VERBOSE
)

# enum with body (either 'typedef enum {...} Name;' or 'enum Name {...};')
TYPEDEF_ENUM_RE = re.compile(
    r"""typedef\s+enum\s*(?:[A-Za-z_]\w*\s*)?\{[\s\S]*?\}\s*([A-Za-z_]\w*)\s*;""",
    re.MULTILINE | re.VERBOSE
)
ENUM_NAMED_RE = re.compile(
    r"""^\s*enum\s+([A-Za-z_]\w*)\s*\{[\s\S]*?\}\s*;""",
    re.MULTILINE | re.VERBOSE
)

# Doxygen blocks /** ... */ or /*! ... */, and line-docs /// / //!
DOXY_BLOCK_RE = re.compile(r"/\*\*[\s\S]*?\*/|/\*![\s\S]*?\*/", re.MULTILINE)
DOXY_LINE_RE  = re.compile(r"^\s*(///|//!)(.*)$", re.MULTILINE)

# Heuristic for single-line global variable declarations in headers
VAR_LINE_RE = re.compile(
    r"""
    ^\s*
    (?:extern\s+|static\s+)?                 
    (?:const\s+|volatile\s+|unsigned\s+|signed\s+|long\s+|short\s+)*   
    [A-Za-z_]\w*                             
    (?:\s+|\s*\*+\s*)                        
    ([A-Za-z_]\w*)                           
    (?:\s*(\[[^\]]*\]|\*+)   )*              
    \s*;                                     
    \s*$
    """,
    re.MULTILINE | re.VERBOSE
)

# ----------------- Helpers -----------------
def find_source_files(roots: List[str], exts=DEFAULT_EXTS) -> List[Path]:
    files: List[Path] = []
    for root in roots:
        p = Path(root)
        if not p.exists():
            continue
        for f in p.rglob("*"):
            if f.is_file() and f.suffix in exts:
                files.append(f)
    return sorted(files)

def split_lines_with_spans(text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int,int]] = []
    start = 0
    for line in text.splitlines(keepends=True):
        end = start + len(line)
        spans.append((start, end))
        start = end
    if not text.endswith("\n"):
        spans.append((len(text), len(text)))
    return spans

def offset_to_line(spans: List[Tuple[int,int]], offset: int) -> int:
    lo, hi = 0, len(spans)-1
    while lo <= hi:
        mid = (lo + hi) // 2
        s, e = spans[mid]
        if s <= offset < e:
            return mid
        if offset < s:
            hi = mid - 1
        else:
            lo = mid + 1
    return max(0, min(len(spans)-1, lo))

def strip_non_code(s: str) -> str:
    """Remove what we allow between doc and item: whitespace, comments, preproc, attributes."""
    s = re.sub(r"//[^\n]*", "", s)                  # // ...
    s = re.sub(r"/\*[\s\S]*?\*/", "", s)            # /* ... */
    s = re.sub(r"#[A-Za-z].*?(?=\n|$)", "", s)      # #define, #if, ...
    s = re.sub(r"__attribute__\s*\(\([^)]+\)\)", "", s)  # __attribute__((...))
    s = re.sub(r"[ \t\r\n]+", "", s)                # whitespace
    return s

def nearest_doc_covers(header: str) -> bool:
    """True if the last doc block/lines in 'header' is followed by no real code."""
    blocks = list(DOXY_BLOCK_RE.finditer(header))
    last_block_span = blocks[-1].span() if blocks else None

    line_spans = [m.span() for m in DOXY_LINE_RE.finditer(header)]
    last_lines_span = (line_spans[0][0], line_spans[-1][1]) if line_spans else None

    candidates = []
    if last_block_span: candidates.append(last_block_span)
    if last_lines_span: candidates.append(last_lines_span)
    if not candidates: return False

    _, doc_end = max(candidates, key=lambda s: s[1])
    between = header[doc_end:]
    return strip_non_code(between) == ""

def has_doxygen_above(text: str, item_start: int, spans: List[Tuple[int,int]], lookback_lines: int) -> bool:
    line_idx = offset_to_line(spans, item_start)
    from_line = max(0, line_idx - lookback_lines)
    start_offset = spans[from_line][0]
    header = text[start_offset:item_start]
    return nearest_doc_covers(header)

# Item finders
def find_function_defs(text: str) -> List[re.Match]:
    return list(FUNC_DEF_RE.finditer(text))

def find_struct_defs(text: str) -> List[re.Match]:
    matches = list(TYPEDEF_STRUCT_RE.finditer(text)) + list(STRUCT_NAMED_RE.finditer(text))
    return sorted(matches, key=lambda m: m.start())

def find_enum_defs(text: str) -> List[re.Match]:
    matches = list(TYPEDEF_ENUM_RE.finditer(text)) + list(ENUM_NAMED_RE.finditer(text))
    return sorted(matches, key=lambda m: m.start())

def find_header_globals(text: str) -> List[re.Match]:
    matches = []
    for m in VAR_LINE_RE.finditer(text):
        line = m.group(0)
        if '(' in line:                      # function prototype
            continue
        if line.strip().startswith("#"):
            continue
        if line.strip().startswith("typedef"):
            continue
        matches.append(m)
    return matches

# ----------------- Analysis per file -----------------
def analyze_file(path: Path, lookback: int, include_structs=True, include_enums=True, include_globals=True) -> Dict:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text = path.read_text(errors="ignore")
    spans = split_lines_with_spans(text)

    ext = path.suffix.lower()

    functions = find_function_defs(text) if ext in (".c", ".h") else []
    fn_total = len(functions)
    fn_doc = sum(1 for m in functions if has_doxygen_above(text, m.start(), spans, lookback))

    st_total = st_doc = 0
    en_total = en_doc = 0
    gv_total = gv_doc = 0

    if include_structs:
        structs = find_struct_defs(text)
        st_total = len(structs)
        st_doc = sum(1 for m in structs if has_doxygen_above(text, m.start(), spans, lookback))

    if include_enums:
        enums = find_enum_defs(text)
        en_total = len(enums)
        en_doc = sum(1 for m in enums if has_doxygen_above(text, m.start(), spans, lookback))

    if include_globals and ext == ".h":
        globals_m = find_header_globals(text)
        gv_total = len(globals_m)
        gv_doc = sum(1 for m in globals_m if has_doxygen_above(text, m.start(), spans, lookback))

    total_items = fn_total + st_total + en_total + gv_total
    documented_items = fn_doc + st_doc + en_doc + gv_doc
    percent = (documented_items / total_items * 100.0) if total_items else 100.0

    return {
        "file": str(path).replace("\\", "/"),
        "dir": str(Path(path).parent).replace("\\", "/"),
        "functions": {"total": fn_total, "doc": fn_doc},
        "structs":   {"total": st_total, "doc": st_doc},
        "enums":     {"total": en_total, "doc": en_doc},
        "globals":   {"total": gv_total, "doc": gv_doc},
        "total": total_items,
        "doc": documented_items,
        "percent": percent,
    }

# ----------------- Grouping -----------------
def group_by_subfolder(results: List[Dict], roots: Iterable[str]) -> Dict[str, List[Dict]]:
    """
    Group by subfolder relative to the best-matching --src root.
    - 'src/ui/menu.c' -> group 'src/ui'
    - 'include/radio.h' -> group 'include'
    If no root matches, group by the file's parent folder.
    """
    norm_roots = [str(Path(r).as_posix()).rstrip("/") for r in roots]
    groups: Dict[str, List[Dict]] = {}
    for r in results:
        fpath = r["file"]
        parent = Path(fpath).parent.as_posix()
        group = parent  # fallback
        for root in norm_roots:
            if Path(fpath).as_posix().startswith(root + "/"):
                rel = Path(fpath).as_posix()[len(root) + 1:]  # after 'root/'
                rel_parent = Path(rel).parent.as_posix()
                group = root if rel_parent == "." else f"{root}/{rel_parent}"
                break
        groups.setdefault(group, []).append(r)
    return groups

# ----------------- Rendering -----------------
def progress_bar_html(pct: float, width=240) -> str:
    val = f"{pct:.2f}"
    return f'<progress value="{val}" max="100" style="width: {width}px; height: 14px;"></progress> {val}%'

def render_markdown(results: List[Dict], out_path: Path, lookback: int, roots: List[str], title="Documentation coverage") -> None:
    # Global sums
    sums = {
        "functions": {"total": 0, "doc": 0},
        "structs": {"total": 0, "doc": 0},
        "enums": {"total": 0, "doc": 0},
        "globals": {"total": 0, "doc": 0},
        "all": {"total": 0, "doc": 0},
    }
    for r in results:
        for k in ("functions", "structs", "enums", "globals"):
            sums[k]["total"] += r[k]["total"]
            sums[k]["doc"]   += r[k]["doc"]
        sums["all"]["total"] += r["total"]
        sums["all"]["doc"]   += r["doc"]

    def pct(d, t): return (d / t * 100.0) if t else 100.0

    lines: List[str] = []
    lines.append(f"# {title}\n")
    lines.append(
        "Counting rule: an item is **documented** if a Doxygen block "
        "`/** ... */` or `/*! ... */` *or* `///`/`//!` lines appear **directly above** "
        f"its definition/declaration, with no real code in between, within **{lookback}** lines.\n"
    )

    # Summary (global + per category)
    lines.append("## Global summary\n")
    gpct = pct(sums["all"]["doc"], sums["all"]["total"])
    lines.append(f"- Total items (all categories): **{sums['all']['total']}**  ")
    lines.append(f"- Documented: **{sums['all']['doc']}**  ")
    lines.append(f"- Global coverage: **{gpct:.2f}%**\n")
    lines.append(progress_bar_html(gpct) + "\n")

    lines.append("### By category\n")
    for k, label in (
        ("functions","Functions"),
        ("structs","Structs"),
        ("enums","Enums"),
        ("globals","Globals variables")
    ):
        tp = pct(sums[k]["doc"], sums[k]["total"])
        lines.append(f"- **{label}**: {sums[k]['doc']}/{sums[k]['total']} ({tp:.2f}%)")
    lines.append("")

    lines.append("### By folder\n")
    # Grouped tables by subfolder (no compact view)
    groups = group_by_subfolder(results, roots)
    for grp in sorted(groups.keys()):
        group_list = groups[grp]
        # order inside group: ascending by percent, then by total desc
        group_list = sorted(group_list, key=lambda x: (x["percent"], -x["total"]))

        if grp==".":
            grp="/"
        # Group header + small summary
        g_tot = sum(x["total"] for x in group_list)
        g_doc = sum(x["doc"] for x in group_list)
        g_pct = pct(g_doc, g_tot)
        lines.append(f"#### {grp or '/'}  \n")
        lines.append(progress_bar_html(g_pct) + "\n")

        # Table for this group
        lines.append("| File | Total | Doc | % | Fn (doc/total) | Structs | Enums | Globals variables |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in group_list:
            lines.append(
                f"| `{r['file']}` | {r['total']} | {r['doc']} | {r['percent']:.2f}% | "
                f"{r['functions']['doc']}/{r['functions']['total']} | "
                f"{r['structs']['doc']}/{r['structs']['total']} | "
                f"{r['enums']['doc']}/{r['enums']['total']} | "
                f"{r['globals']['doc']}/{r['globals']['total']} |"
            )
        lines.append("")  # spacer between groups

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

# ----------------- CLI -----------------
def main():
    ap = argparse.ArgumentParser(description="Compute documentation coverage for C code (functions/structs/enums/global variables) and render a MkDocs page.")
    ap.add_argument("--src", nargs="+", default=DEFAULT_SRC_DIRS, help="Directories to scan (default: src include)")
    ap.add_argument("--out", default="mkdocs/docs/status.md", help="Output Markdown page for MkDocs")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_LINES, help=f"Lines to look above items for docs (default: {LOOKBACK_LINES})")
    ap.add_argument("--no-globals", action="store_true", help="Do not include global variables coverage")
    ap.add_argument("--no-structs", action="store_true", help="Do not include struct coverage")
    ap.add_argument("--no-enums", action="store_true", help="Do not include enum coverage")
    ap.add_argument("--only-headers", action="store_true", help="Only analyze headers for non-function items")
    args = ap.parse_args()

    include_structs = not args.no_structs
    include_enums   = not args.no_enums
    include_globals = not args.no_globals

    files = find_source_files(args.src)
    results = []
    for p in files:
        if args.only_headers and p.suffix.lower() != ".h":
            continue
        r = analyze_file(p, args.lookback, include_structs, include_enums, include_globals)
        results.append(r)

    # Console per-file (kept for CI logs)
    for r in sorted(results, key=lambda x: x["file"]):
        print(
            f"{r['file']} : {r['doc']}/{r['total']} ({r['percent']:.2f}%)  "
            f"[fn {r['functions']['doc']}/{r['functions']['total']}, "
            f"struct {r['structs']['doc']}/{r['structs']['total']}, "
            f"enum {r['enums']['doc']}/{r['enums']['total']}, "
            f"globals {r['globals']['doc']}/{r['globals']['total']}]"
        )

    # Global summary
    tot = sum(r["total"] for r in results)
    doc = sum(r["doc"] for r in results)
    pct = (doc / tot * 100.0) if tot else 100.0
    print(f"[global] {doc}/{tot} ({pct:.2f}%)")

    # MkDocs page (grouped tables)
    render_markdown(results, Path(args.out), args.lookback, args.src)

if __name__ == "__main__":
    main()
