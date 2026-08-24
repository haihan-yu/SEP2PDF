#!/usr/bin/env python3
"""Build a faithful, polished PDF from one Stanford Encyclopedia entry."""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urljoin, urlparse, urlunparse

try:
    import requests
    from bs4 import BeautifulSoup, Comment, NavigableString, Tag
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError as exc:  # pragma: no cover - exercised by dependency preflight
    raise SystemExit(
        "Missing Python dependency. Install requests and beautifulsoup4 after user approval."
    ) from exc


SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSET_ROOT = SKILL_ROOT / "assets"
ALLOWED_HOST = "plato.stanford.edu"
USER_AGENT = (
    "sep-article-pdf/1.0 (personal scholarly PDF conversion; "
    "one entry per request; https://plato.stanford.edu/info.html#c)"
)
REQUIRED_COMMANDS = ("pandoc", "xelatex", "latexmk", "pdftotext", "pdfinfo", "pdftoppm")
BLOCK_TAGS = ("h2", "h3", "h4", "p", "li", "dt", "dd", "th", "td")
SELECTED_REGION_IDS = (
    "main-text",
    "bibliography",
    "other-internet-resources",
    "related-entries",
    "acknowledgments",
)
MATH_PATTERN = re.compile(r"\\\((?:.|\n)*?\\\)|\\\[(?:.|\n)*?\\\]", re.DOTALL)


class BuildError(RuntimeError):
    """A user-actionable conversion failure."""


@dataclasses.dataclass
class ArticleMetadata:
    title: str
    authors: List[str]
    publication_history: str
    revision_date: str
    canonical_url: str
    copyright_html: str


@dataclasses.dataclass
class Manifest:
    blocks: List[str]
    notes: int
    tables: int
    figures: int
    images: int
    math_fragments: int


@dataclasses.dataclass
class ProcessedCounts:
    notes: int = 0
    tables: int = 0
    figures: int = 0
    images: int = 0
    math_fragments: int = 0


@dataclasses.dataclass
class QAResult:
    pages: int
    overall_coverage: float
    minimum_block_coverage: float
    rendered_pages: int


def require_commands(commands: Sequence[str] = REQUIRED_COMMANDS) -> Dict[str, str]:
    resolved: Dict[str, str] = {}
    missing: List[str] = []
    for command in commands:
        path = shutil.which(command)
        if path:
            resolved[command] = path
        else:
            missing.append(command)
    if missing:
        raise BuildError("Missing required command(s): " + ", ".join(missing))
    return resolved


def normalize_sep_url(value: str) -> str:
    candidate = value.strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", candidate):
        candidate = f"https://{ALLOWED_HOST}/entries/{candidate.lower()}/"
    elif "://" not in candidate:
        raise BuildError(
            "Provide an official SEP URL or a bare entry slug. Resolve article titles before running the builder."
        )

    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() != ALLOWED_HOST:
        raise BuildError(f"Only official {ALLOWED_HOST} entry URLs are accepted.")
    if parsed.username or parsed.password or parsed.port:
        raise BuildError("Credentials and custom ports are not allowed in SEP URLs.")

    path = re.sub(r"/index\.html?$", "/", parsed.path, flags=re.IGNORECASE)
    current = re.fullmatch(r"/entries/[A-Za-z0-9-]+/?", path)
    archived = re.fullmatch(r"/archives/[A-Za-z0-9-]+/entries/[A-Za-z0-9-]+/?", path)
    if not (current or archived):
        raise BuildError("The URL must identify one current or archived SEP entry root.")
    if not path.endswith("/"):
        path += "/"
    return urlunparse(("https", ALLOWED_HOST, path, "", "", ""))


def validate_download_url(url: str, *, official_only: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BuildError(f"Unsafe or unsupported download URL: {url}")
    host = parsed.hostname.lower()
    if official_only and host != ALLOWED_HOST:
        raise BuildError(f"Expected an official SEP resource, got: {url}")
    if host == "localhost" or host.endswith(".localhost"):
        raise BuildError(f"Local network resources are not allowed: {url}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise BuildError(f"Private or local network resources are not allowed: {url}")


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET",)),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    return session


def fetch(session: requests.Session, url: str, *, official_only: bool = False) -> requests.Response:
    validate_download_url(url, official_only=official_only)
    try:
        response = session.get(url, timeout=(10, 45), allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise BuildError(f"Could not download {url}: {exc}") from exc
    validate_download_url(response.url, official_only=official_only)
    return response


def squash_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def tex_escape(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def tex_pdf_string(text: str) -> str:
    return tex_escape(squash_space(text)).replace("\n", " ")


def normalize_math_markup(text: str) -> str:
    def normalize_fragment(match: re.Match) -> str:
        fragment = match.group(0)
        fragment = re.sub(r"\\gt\b", ">", fragment)
        fragment = re.sub(r"\\lt\b", "<", fragment)
        # Pandoc treats TeX math containing a blank line as ordinary text and
        # escapes every control sequence. SEP uses blank lines between rows in
        # some align environments, where they carry no mathematical meaning.
        fragment = re.sub(r"\n[ \t]*\n+", "\n", fragment)
        # SEP places align environments inside \[...\]. LaTeX forbids that
        # nesting, and converting to aligned would make equation tags illegal.
        # Remove only the redundant outer display delimiters.
        align = re.fullmatch(
            r"\\\[\s*(\\begin\{align(?P<star>\*?)\}.*?\\end\{align(?P=star)\})\s*\\\]",
            fragment,
            flags=re.DOTALL,
        )
        if align:
            fragment = align.group(1)
            fragment = re.sub(r"\\begin\{align\}", r"\\begin{align*}", fragment, count=1)
            fragment = re.sub(r"\\end\{align\}", r"\\end{align*}", fragment, count=1)
        return fragment

    return MATH_PATTERN.sub(normalize_fragment, text)


def strip_math(text: str) -> str:
    return MATH_PATTERN.sub(" ", text)


def normalized_tokens(text: str) -> List[str]:
    # Poppler exposes TeX's discretionary hyphens as U+2010 at line endings.
    # Canonicalize every alphabetic hyphen in both source and PDF text so an
    # authorial compound and a line-break choice compare identically.
    text = re.sub(
        r"(?<=[^\W\d_])[-\u00ad\u2010\u2011]\s*(?:\n\s*)?(?=[^\W\d_])",
        "",
        text,
        flags=re.UNICODE,
    )
    # Source HTML and PDF text extraction can choose different apostrophe
    # glyphs for the same possessive or contraction. Treat those typographic
    # variants as format-only differences before comparing exact tokens.
    text = text.translate(
        {
            ord("\u2018"): "'",
            ord("\u2019"): "'",
            ord("\u02bc"): "'",
            ord("\uff07"): "'",
        }
    )
    text = strip_math(unicodedata.normalize("NFKD", text)).casefold()
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl")
    return re.findall(r"[^\W_]+(?:'[^\W_]+)?", text, flags=re.UNICODE)


def counter_coverage(source_tokens: Sequence[str], pdf_counter: collections.Counter) -> float:
    if not source_tokens:
        return 1.0
    source_counter = collections.Counter(source_tokens)
    matched = sum(min(count, pdf_counter[token]) for token, count in source_counter.items())
    return matched / sum(source_counter.values())


def safe_filename(title: str, revision_date: str) -> str:
    cleaned = unicodedata.normalize("NFC", title)
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", cleaned)
    cleaned = squash_space(cleaned).strip(". ") or "SEP Article"
    cleaned = cleaned[:150].rstrip()
    return f"{cleaned} - SEP ({revision_date}).pdf"


def unique_output_path(path: Path, overwrite: bool) -> Path:
    if overwrite or not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise BuildError(f"Could not choose a unique output filename beside {path}")


def parse_revision_date(soup: BeautifulSoup, pubinfo: str) -> str:
    modified = soup.find("meta", attrs={"name": "DCTERMS.modified"})
    if modified and modified.get("content"):
        value = squash_space(str(modified["content"]))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
    match = re.search(
        r"substantive revision\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
        r"(\d{1,2}),?\s+(\d{4})",
        pubinfo,
        flags=re.IGNORECASE,
    )
    if match:
        parsed = dt.datetime.strptime(f"{match.group(1)} {match.group(2)}", "%d %Y")
        # Month is recovered from the full match separately.
        month_match = re.search(
            r"substantive revision\s+\w+\s+(\w+)\s+\d{1,2},?\s+\d{4}", pubinfo, re.I
        )
        if month_match:
            parsed = dt.datetime.strptime(
                f"{month_match.group(1)} {match.group(1)} {match.group(2)}", "%b %d %Y"
            )
        return parsed.date().isoformat()
    published = soup.find("meta", attrs={"name": "DCTERMS.issued"})
    if published and published.get("content"):
        return squash_space(str(published["content"]))[:10]
    return dt.date.today().isoformat()


def extract_metadata(soup: BeautifulSoup, canonical_url: str) -> ArticleMetadata:
    editable = soup.select_one("#aueditable")
    title_tag = editable.select_one("h1") if editable else None
    preamble = editable.select_one("#preamble") if editable else None
    main_text = editable.select_one("#main-text") if editable else None
    if not editable or not title_tag or not preamble or not main_text:
        raise BuildError("This page does not expose the required SEP article regions.")

    title = squash_space(title_tag.get_text(" ", strip=True))
    pubinfo_tag = editable.select_one("#pubinfo")
    publication_history = squash_space(pubinfo_tag.get_text(" ", strip=True)) if pubinfo_tag else ""
    authors = [
        squash_space(str(meta.get("content", "")))
        for meta in soup.find_all("meta", attrs={"name": "citation_author"})
        if squash_space(str(meta.get("content", "")))
    ]
    if not authors:
        creator = soup.find("meta", attrs={"name": "DC.creator"})
        if creator and creator.get("content"):
            authors = [squash_space(str(creator["content"]))]
    if not authors:
        copyright_tag = soup.select_one("#article-copyright")
        if copyright_tag:
            names = [squash_space(link.get_text(" ", strip=True)) for link in copyright_tag.find_all("a")]
            authors = [name for name in names if name and "Copyright" not in name and "info" not in name.lower()]
    if not authors:
        authors = ["SEP contributing author"]

    copyright_tag = soup.select_one("#article-copyright")
    copyright_html = str(copyright_tag) if copyright_tag else ""
    return ArticleMetadata(
        title=title,
        authors=authors,
        publication_history=publication_history,
        revision_date=parse_revision_date(soup, publication_history),
        canonical_url=canonical_url,
        copyright_html=copyright_html,
    )


def apply_pdf_directives(container: Tag) -> None:
    parents: List[Tag] = [container] + list(container.find_all(True))
    for parent in parents:
        excluding = False
        for child in list(parent.contents):
            if isinstance(child, Comment):
                value = str(child).strip()
                if value == "pdf exclude begin":
                    if excluding:
                        raise BuildError("Nested SEP pdf-exclude directives are malformed.")
                    excluding = True
                    child.extract()
                    continue
                if value == "pdf exclude end":
                    if not excluding:
                        raise BuildError("SEP pdf-exclude end has no matching begin marker.")
                    excluding = False
                    child.extract()
                    continue
                match = re.fullmatch(r"pdf include(.*)pdf include", value, flags=re.DOTALL)
                if match:
                    fragment = BeautifulSoup(match.group(1), "html.parser")
                    replacement_nodes = list(fragment.contents)
                    for node in reversed(replacement_nodes):
                        child.insert_after(node)
                    child.extract()
                    continue
                child.extract()
                continue
            if excluding:
                child.extract()
        if excluding:
            raise BuildError("SEP pdf-exclude begin has no matching end marker.")


def clone_tag(tag: Tag) -> Tag:
    """Clone a subtree without retaining BeautifulSoup's parent/document graph."""
    fragment = BeautifulSoup(str(tag), "html.parser")
    clone = fragment.find(tag.name)
    if clone is None:
        raise BuildError(f"Could not isolate SEP <{tag.name}> region.")
    return clone


def selected_regions(soup: BeautifulSoup) -> Tuple[Tag, List[Tag]]:
    editable = soup.select_one("#aueditable")
    if not editable:
        raise BuildError("Missing SEP #aueditable article container.")
    preamble_original = editable.select_one("#preamble")
    main_original = editable.select_one("#main-text")
    if not preamble_original or not main_original:
        raise BuildError("Missing SEP preamble or main text.")
    preamble = clone_tag(preamble_original)
    regions: List[Tag] = []
    for region_id in SELECTED_REGION_IDS:
        region = editable.select_one(f"#{region_id}")
        if region:
            regions.append(clone_tag(region))
    for region in [preamble] + regions:
        apply_pdf_directives(region)
        for unwanted in region.select("script,style,#academic-tools"):
            unwanted.decompose()
    return preamble, regions


def block_text(tag: Tag) -> str:
    clone = clone_tag(tag)
    for note_ref in clone.select("sup"):
        if note_ref.find("a", href=re.compile(r"notes?\.html?#", re.I)):
            note_ref.decompose()
    for line_break in clone.find_all("br"):
        line_break.replace_with(NavigableString(" "))
    if clone.name == "li":
        for nested_list in clone.find_all(["ul", "ol", "dl"], recursive=False):
            nested_list.decompose()
    # An explicit separator invents spaces at inline markup boundaries (for
    # example SEP's 8<sup>ième</sup>). Preserve the document's text nodes.
    return squash_space(clone.get_text("", strip=False))


def collect_blocks(containers: Sequence[Tag]) -> List[str]:
    blocks: List[str] = []
    for container in containers:
        for tag in container.find_all(BLOCK_TAGS):
            if tag.name == "li" and tag.find(["p", "li", "dt", "dd"], recursive=False):
                continue
            if tag.name in ("th", "td") and tag.find(["p", "li", "dt", "dd"], recursive=False):
                continue
            value = block_text(tag)
            if normalized_tokens(value):
                blocks.append(value)
    return blocks


def count_math(containers: Sequence[Tag]) -> int:
    return sum(len(MATH_PATTERN.findall(str(container))) for container in containers)


def source_manifest(preamble: Tag, regions: Sequence[Tag]) -> Manifest:
    containers = [preamble] + list(regions)
    images = sum(len(container.find_all("img")) for container in containers)
    tables = sum(len(container.find_all("table")) for container in containers)
    figures = sum(
        len(
            [
                tag
                for tag in container.find_all(True, class_=lambda classes: classes and "figure" in classes)
                if tag.find(["img", "table"])
            ]
        )
        for container in containers
    )
    notes = sum(
        len(container.select('sup a[href*="notes.html#"], sup a[href*="note.html#"]'))
        for container in containers
    )
    return Manifest(
        blocks=collect_blocks(containers),
        notes=notes,
        tables=tables,
        figures=figures,
        images=images,
        math_fragments=count_math(containers),
    )


class JSSubsetParser:
    """Parse the JSON-like subset used by SEP MathJax macro objects."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def error(self, message: str) -> BuildError:
        return BuildError(f"Unsafe or unsupported MathJax macro syntax at offset {self.pos}: {message}")

    def skip(self) -> None:
        while self.pos < len(self.text):
            if self.text[self.pos].isspace():
                self.pos += 1
            elif self.text.startswith("//", self.pos):
                end = self.text.find("\n", self.pos + 2)
                self.pos = len(self.text) if end < 0 else end + 1
            elif self.text.startswith("/*", self.pos):
                end = self.text.find("*/", self.pos + 2)
                if end < 0:
                    raise self.error("unterminated comment")
                self.pos = end + 2
            else:
                break

    def parse(self) -> Any:
        self.skip()
        value = self.value()
        self.skip()
        if self.pos != len(self.text):
            raise self.error("trailing data")
        return value

    def value(self) -> Any:
        self.skip()
        if self.pos >= len(self.text):
            raise self.error("expected value")
        char = self.text[self.pos]
        if char == "{":
            return self.object()
        if char == "[":
            return self.array()
        if char in ("'", '"'):
            return self.string()
        number = re.match(r"-?(?:\d+(?:\.\d*)?|\.\d+)", self.text[self.pos :])
        if number:
            raw = number.group(0)
            self.pos += len(raw)
            return float(raw) if "." in raw else int(raw)
        identifier = self.identifier()
        literals = {"true": True, "false": False, "null": None}
        if identifier in literals:
            return literals[identifier]
        raise self.error(f"unsupported value {identifier!r}")

    def identifier(self) -> str:
        match = re.match(r"[A-Za-z_$][A-Za-z0-9_$-]*", self.text[self.pos :])
        if not match:
            raise self.error("expected identifier")
        value = match.group(0)
        self.pos += len(value)
        return value

    def string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        result: List[str] = []
        escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v"}
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if char == quote:
                return "".join(result)
            if char != "\\":
                result.append(char)
                continue
            if self.pos >= len(self.text):
                raise self.error("unterminated escape")
            escaped = self.text[self.pos]
            self.pos += 1
            if escaped in escapes:
                result.append(escapes[escaped])
            elif escaped == "u":
                raw = self.text[self.pos : self.pos + 4]
                if not re.fullmatch(r"[0-9A-Fa-f]{4}", raw):
                    raise self.error("invalid Unicode escape")
                result.append(chr(int(raw, 16)))
                self.pos += 4
            elif escaped == "x":
                raw = self.text[self.pos : self.pos + 2]
                if not re.fullmatch(r"[0-9A-Fa-f]{2}", raw):
                    raise self.error("invalid hexadecimal escape")
                result.append(chr(int(raw, 16)))
                self.pos += 2
            else:
                result.append(escaped)
        raise self.error("unterminated string")

    def object(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        self.pos += 1
        while True:
            self.skip()
            if self.pos < len(self.text) and self.text[self.pos] == "}":
                self.pos += 1
                return result
            key = self.string() if self.text[self.pos] in ("'", '"') else self.identifier()
            self.skip()
            if self.pos >= len(self.text) or self.text[self.pos] != ":":
                raise self.error("expected colon")
            self.pos += 1
            result[key] = self.value()
            self.skip()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
                continue
            if self.pos < len(self.text) and self.text[self.pos] == "}":
                self.pos += 1
                return result
            raise self.error("expected comma or closing brace")

    def array(self) -> List[Any]:
        result: List[Any] = []
        self.pos += 1
        while True:
            self.skip()
            if self.pos < len(self.text) and self.text[self.pos] == "]":
                self.pos += 1
                return result
            result.append(self.value())
            self.skip()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
                continue
            if self.pos < len(self.text) and self.text[self.pos] == "]":
                self.pos += 1
                return result
            raise self.error("expected comma or closing bracket")


def balanced_object(text: str, start: int) -> str:
    if start >= len(text) or text[start] != "{":
        raise BuildError("MathJax Macros value is not an object.")
    depth = 0
    quote: Optional[str] = None
    escaped = False
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        else:
            if char in ("'", '"'):
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        index += 1
    raise BuildError("Unterminated MathJax Macros object.")


def parse_mathjax_macros(script_text: str) -> Dict[str, Any]:
    match = re.search(r"\bMacros\s*:\s*\{", script_text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"\bmacros\s*:\s*\{", script_text, flags=re.IGNORECASE)
    if not match:
        return {}
    start = script_text.find("{", match.start())
    value = JSSubsetParser(balanced_object(script_text, start)).parse()
    if not isinstance(value, dict):
        raise BuildError("MathJax Macros must be an object.")
    return value


def decode_mathjax_unicode(value: str) -> str:
    def replace(match: re.Match) -> str:
        return chr(int(match.group(1), 16))

    return re.sub(r"\\unicode\{x([0-9A-Fa-f]+)\}", replace, value)


def render_mathjax_macros(macros: Mapping[str, Any]) -> str:
    lines: List[str] = [r"\makeatletter"]
    for name in sorted(macros):
        if not re.fullmatch(r"[A-Za-z@]+", name):
            raise BuildError(f"Unsupported MathJax macro name: {name!r}")
        value = macros[name]
        nargs = 0
        default: Optional[str] = None
        if isinstance(value, str):
            replacement = value
        elif isinstance(value, list) and value and isinstance(value[0], str):
            replacement = value[0]
            if len(value) >= 2:
                if not isinstance(value[1], int) or not 0 <= value[1] <= 9:
                    raise BuildError(f"Unsupported argument count for MathJax macro {name}")
                nargs = value[1]
            if len(value) >= 3:
                if not isinstance(value[2], str):
                    raise BuildError(f"Unsupported optional argument for MathJax macro {name}")
                default = value[2]
        else:
            raise BuildError(f"Unsupported MathJax macro definition for {name}")
        replacement = decode_mathjax_unicode(replacement)
        args = f"[{nargs}]" if nargs else ""
        if default is not None:
            args += f"[{default}]"
        lines.append(f"\\providecommand{{\\{name}}}{args}{{{replacement}}}")
        lines.append(f"\\renewcommand{{\\{name}}}{args}{{{replacement}}}")
    lines.append(r"\makeatother")
    return "\n".join(lines) if macros else ""


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: Optional[str] = None,
    env: Optional[MutableMapping[str, str]] = None,
) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
    except OSError as exc:
        raise BuildError(f"Could not run {command[0]}: {exc}") from exc
    if completed.returncode != 0:
        detail = "\n".join((completed.stdout + "\n" + completed.stderr).splitlines()[-35:])
        raise BuildError(f"Command failed ({' '.join(command)}):\n{detail}")
    return completed


class SEPConverter:
    def __init__(
        self,
        session: requests.Session,
        workspace: Path,
        base_url: str,
        commands: Mapping[str, str],
    ):
        self.session = session
        self.workspace = workspace
        self.base_url = base_url
        self.commands = commands
        self.asset_dir = workspace / "assets"
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.replacements: Dict[str, str] = {}
        self.used_tokens: set = set()
        self.token_index = 0
        self.processed = ProcessedCounts()

    def token(self, kind: str, latex: str) -> str:
        self.token_index += 1
        token = f"SEP{kind.upper()}TOKEN{self.token_index:06d}X"
        self.replacements[token] = latex
        return token

    def pandoc(self, html_fragment: str, *, inline: bool = False) -> str:
        command = [
            self.commands["pandoc"],
            "--from=html+tex_math_single_backslash",
            "--to=latex",
            "--wrap=preserve",
        ]
        completed = run_command(command, cwd=self.workspace, input_text=html_fragment)
        latex = completed.stdout.strip()
        if inline:
            latex = re.sub(r"\s*\n\s*", " ", latex)
            latex = re.sub(r"^\\(?:begin|end)\{[^}]+\}\s*", "", latex)
        return latex

    def replace_tokens(self, latex: str) -> str:
        replacements = sorted(self.replacements.items(), key=lambda pair: -len(pair[0]))
        # Replacements can contain earlier placeholders, for example a note or
        # table whose LaTeX contains protected mathematics. Resolve to a fixed
        # point while retaining the disappearance checks below.
        for _ in range(len(replacements) + 1):
            changed = False
            for token, replacement in replacements:
                if token in latex:
                    latex = latex.replace(token, replacement)
                    self.used_tokens.add(token)
                    changed = True
            if not changed:
                break
        leftovers = re.findall(r"SEP[A-Z]+TOKEN\d+X", latex)
        if leftovers:
            raise BuildError("Unresolved conversion placeholders: " + ", ".join(leftovers[:5]))
        return latex

    def assert_all_tokens_used(self) -> None:
        unused = sorted(set(self.replacements) - self.used_tokens)
        if unused:
            raise BuildError("Conversion placeholder disappeared: " + ", ".join(unused[:5]))

    def normalize_dom(self, container: Tag) -> None:
        for text_node in list(container.find_all(string=True)):
            if isinstance(text_node, Comment):
                text_node.extract()
                continue
            normalized = MATH_PATTERN.sub(
                lambda match: self.token("math", normalize_math_markup(match.group(0))),
                str(text_node),
            )
            if normalized != str(text_node):
                text_node.replace_with(NavigableString(normalized))
        for anchor in container.find_all("a"):
            href = anchor.get("href")
            if href and not href.startswith("#") and not href.lower().startswith(("mailto:", "tel:")):
                anchor["href"] = urljoin(self.base_url, href)
            anchor.attrs.pop("target", None)
        for element in container.select("div.indent"):
            element.name = "blockquote"
            element.attrs = {}

    def resolve_notes(self, containers: Sequence[Tag], manifest: Manifest) -> None:
        references: List[Tuple[Tag, str, str]] = []
        for container in containers:
            for anchor in container.select('sup a[href*="notes.html#"], sup a[href*="note.html#"]'):
                href = str(anchor.get("href", ""))
                note_url, fragment = href.split("#", 1)
                references.append((anchor.find_parent("sup"), urljoin(self.base_url, note_url), fragment))
        if len(references) != manifest.notes:
            raise BuildError("Note reference count changed before note resolution.")

        pages: Dict[str, BeautifulSoup] = {}
        note_blocks: List[str] = []
        for sup, note_url, fragment in references:
            if note_url not in pages:
                response = fetch(self.session, note_url, official_only=True)
                pages[note_url] = BeautifulSoup(response.content, "html.parser")
            note = pages[note_url].find(id=fragment)
            if not note:
                raise BuildError(f"Unresolved SEP note {fragment} at {note_url}")
            note_copy = clone_tag(note)
            for back_link in list(note_copy.find_all("a", href=re.compile(r"index\.html?#|\./?#", re.I))):
                if re.fullmatch(r"\d+\.?", squash_space(back_link.get_text(" ", strip=True))):
                    back_link.decompose()
                    break
            raw_note_html = "".join(str(child) for child in note_copy.contents)
            note_math = len(MATH_PATTERN.findall(raw_note_html))
            raw_note_text = squash_space(note_copy.get_text(" ", strip=True))
            if not raw_note_text:
                raise BuildError(f"SEP note {fragment} is empty.")
            note_text = squash_space(strip_math(raw_note_text))
            if note_text:
                note_blocks.append(note_text)
            manifest.math_fragments += note_math
            self.processed.math_fragments += note_math
            # Notes are fetched from a separate page after the entry DOM has
            # been selected. Normalize their math and links through the same
            # path as the main article before handing the fragment to Pandoc.
            self.normalize_dom(note_copy)
            note_html = "".join(str(child) for child in note_copy.contents)
            note_latex = self.pandoc(note_html)
            note_latex = re.sub(r"^\s*\d+\.\s*", "", note_latex)
            token = self.token("note", f"\\footnote{{{note_latex}}}")
            sup.replace_with(NavigableString(token))
            self.processed.notes += 1
        manifest.blocks.extend(note_blocks)

    def local_mathjax_macros(self, soup: BeautifulSoup) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        script_texts: List[str] = []
        for script in soup.find_all("script"):
            src = script.get("src")
            if src and unquote(urlparse(src).path).endswith("local.js"):
                response = fetch(self.session, urljoin(self.base_url, src), official_only=True)
                script_texts.append(response.text)
            elif script.string and re.search(r"MathJax", script.string, re.I):
                script_texts.append(script.string)
        for script_text in script_texts:
            merged.update(parse_mathjax_macros(script_text))
        return merged

    def download_asset(self, src: str) -> str:
        url = urljoin(self.base_url, src)
        response = fetch(self.session, url, official_only=False)
        path = urlparse(response.url).path
        raw_name = unquote(Path(path).name) or "image"
        suffix = Path(raw_name).suffix.lower()
        if not suffix:
            mime = response.headers.get("Content-Type", "").split(";", 1)[0]
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/svg+xml": ".svg", "application/pdf": ".pdf"}.get(mime, "")
        if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf"):
            raise BuildError(f"Unsupported SEP image type for {url}")
        digest = hashlib.sha256(response.content).hexdigest()[:12]
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(raw_name).stem).strip("-.") or "image"
        source_path = self.asset_dir / f"{stem}-{digest}{suffix}"
        source_path.write_bytes(response.content)
        if source_path.stat().st_size == 0:
            raise BuildError(f"Downloaded image is empty: {url}")

        converted = source_path
        if suffix == ".svg":
            converted = self.convert_svg(source_path)
        elif suffix == ".gif":
            converted = self.convert_with_quicklook(source_path, ".png")
        self.processed.images += 1
        return f"assets/{converted.name}"

    def convert_svg(self, source: Path) -> Path:
        output = source.with_suffix(".pdf")
        inkscape = shutil.which("inkscape")
        rsvg = shutil.which("rsvg-convert")
        if inkscape:
            run_command(
                [inkscape, str(source), "--export-type=pdf", f"--export-filename={output}"],
                cwd=self.workspace,
            )
        elif rsvg:
            run_command([rsvg, "-f", "pdf", "-o", str(output), str(source)], cwd=self.workspace)
        elif sys.platform == "darwin" and shutil.which("qlmanage"):
            return self.convert_with_quicklook(source, ".png")
        else:
            raise BuildError(
                "SVG figure requires Inkscape, rsvg-convert, or macOS qlmanage; figure was not omitted."
            )
        if not output.exists() or output.stat().st_size == 0:
            raise BuildError(f"SVG conversion did not produce {output.name}")
        return output

    def convert_with_quicklook(self, source: Path, suffix: str) -> Path:
        qlmanage = shutil.which("qlmanage")
        if not qlmanage:
            raise BuildError(f"No converter is available for {source.suffix} images.")
        preview_dir = self.workspace / f"preview-{uuid.uuid4().hex}"
        preview_dir.mkdir()
        run_command([qlmanage, "-t", "-s", "3000", "-o", str(preview_dir), str(source)], cwd=self.workspace)
        candidates = sorted(preview_dir.glob("*.png"))
        if not candidates:
            raise BuildError(f"Quick Look did not render {source.name}")
        output = source.with_suffix(suffix)
        shutil.move(str(candidates[0]), str(output))
        shutil.rmtree(preview_dir)
        if output.stat().st_size == 0:
            raise BuildError(f"Quick Look produced an empty image for {source.name}")
        return output

    def render_image(self, image: Tag) -> str:
        src = image.get("src")
        if not src:
            raise BuildError("SEP image has no src attribute.")
        local_path = self.download_asset(str(src))
        alt = squash_space(str(image.get("alt", "")))
        alt_note = f"\n\\sepfigurelabel{{{tex_escape(alt)}}}" if alt and not re.fullmatch(r"Figure\s+\d+", alt, re.I) else ""
        return (
            "\\includegraphics[max width=\\linewidth,max height=0.78\\textheight]"
            f"{{{local_path}}}{alt_note}"
        )

    def table_to_latex(self, table: Tag) -> str:
        rows = table.find_all("tr")
        if not rows:
            raise BuildError("SEP table contains no rows.")
        occupied: Dict[Tuple[int, int], bool] = {}
        origins: Dict[Tuple[int, int], Tuple[Tag, int, int]] = {}
        max_columns = 0
        for row_index, row in enumerate(rows):
            column = 0
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                cells = row.find_all(["th", "td"])
            for cell in cells:
                while occupied.get((row_index, column)):
                    column += 1
                try:
                    rowspan = int(cell.get("rowspan", 1))
                    colspan = int(cell.get("colspan", 1))
                except (TypeError, ValueError) as exc:
                    raise BuildError("Non-numeric table rowspan or colspan.") from exc
                if rowspan < 1 or colspan < 1 or rowspan > 100 or colspan > 100:
                    raise BuildError("Invalid or unreasonable table span.")
                for rr in range(row_index, row_index + rowspan):
                    for cc in range(column, column + colspan):
                        if occupied.get((rr, cc)):
                            raise BuildError("Overlapping SEP table spans.")
                        occupied[(rr, cc)] = True
                origins[(row_index, column)] = (cell, rowspan, colspan)
                column += colspan
                max_columns = max(max_columns, column)
        if max_columns == 0:
            raise BuildError("SEP table contains no cells.")

        grid_lines: List[str] = []
        table_classes = set(table.get("class", []))
        centered = any("center" in name for name in table_classes)
        for row_index in range(len(rows)):
            rendered: List[str] = []
            for column in range(max_columns):
                origin = origins.get((row_index, column))
                if not origin:
                    rendered.append("")
                    continue
                cell, rowspan, colspan = origin
                content_html = "".join(str(child) for child in cell.contents)
                content = self.pandoc(content_html, inline=True)
                if cell.name == "th":
                    content = f"\\textbf{{{content}}}"
                options: List[str] = []
                if rowspan > 1:
                    options.append(f"r={rowspan}")
                if colspan > 1:
                    options.append(f"c={colspan}")
                alignment = str(cell.get("align", ""))
                cell_classes = set(cell.get("class", []))
                cell_style = ""
                if "center" in cell_classes or alignment == "center" or centered:
                    cell_style = "c"
                elif alignment == "right":
                    cell_style = "r"
                if options or cell_style:
                    span_options = f"[{','.join(options)}]" if options else ""
                    content = f"\\SetCell{span_options}{{{cell_style}}} {content}"
                rendered.append(content)
            grid_lines.append(" & ".join(rendered) + r" \\")

        grid_style = "hlines,vlines," if any("box" in name for name in table_classes) else ""
        colspec = f"*{{{max_columns}}}{{X[l,m]}}"
        latex = (
            "\\begin{center}\n"
            f"\\begin{{tblr}}{{width=\\linewidth,colspec={{{colspec}}},{grid_style}"
            "cells={font=\\small},rowsep=3pt,colsep=4pt}\n"
            + "\n".join(grid_lines)
            + "\n\\end{tblr}\n\\end{center}"
        )
        self.processed.tables += 1
        return latex

    def process_figures(self, container: Tag) -> None:
        figures = [
            element
            for element in container.find_all(True, class_=lambda classes: classes and "figure" in classes)
            if element.find(["img", "table"])
        ]
        for figure in figures:
            if not figure.parent:
                continue
            parts: List[str] = []
            for image in figure.find_all("img"):
                parts.append(self.render_image(image))
            for table in figure.find_all("table"):
                parts.append(self.table_to_latex(table))
            label_tag = figure.select_one(".figlabel")
            label = squash_space(label_tag.get_text(" ", strip=True)) if label_tag else ""
            if not label:
                centered = figure.select_one("p.center")
                label = squash_space(centered.get_text(" ", strip=True)) if centered else ""
            label_latex = f"\n\\sepfigurelabel{{{tex_escape(label)}}}" if label else ""
            latex = (
                "\\begin{figure}[htbp]\n\\centering\n"
                + "\n\\par\medskip\n".join(parts)
                + label_latex
                + "\n\\end{figure}"
            )
            figure.replace_with(NavigableString(self.token("figure", latex)))
            self.processed.figures += 1

    def process_standalone_assets(self, container: Tag) -> None:
        for image in list(container.find_all("img")):
            latex = "\\begin{center}\n" + self.render_image(image) + "\n\\end{center}"
            image.replace_with(NavigableString(self.token("image", latex)))
        for table in list(container.find_all("table")):
            table.replace_with(NavigableString(self.token("table", self.table_to_latex(table))))

    def process_centered_paragraphs(self, container: Tag) -> None:
        for paragraph in list(container.select("p.center")):
            latex = self.pandoc("".join(str(child) for child in paragraph.contents))
            paragraph.replace_with(
                NavigableString(self.token("center", f"\\begin{{center}}\n{latex}\n\\end{{center}}"))
            )

    def process_headings(self, container: Tag) -> None:
        level_map = {"h2": ("section", "section"), "h3": ("subsection", "subsection"), "h4": ("subsubsection", "subsubsection")}
        for heading in list(container.find_all(["h2", "h3", "h4"])):
            heading_copy = clone_tag(heading)
            targets: List[str] = []
            if heading.get("id"):
                targets.append(str(heading["id"]))
            for anchor in heading_copy.find_all("a"):
                target = anchor.get("id") or anchor.get("name")
                if target:
                    targets.append(str(target))
                    anchor.unwrap()
            title_latex = self.pandoc("".join(str(child) for child in heading_copy.contents), inline=True)
            command, toc_level = level_map[heading.name]
            target_latex = "\n".join(
                f"\\hypertarget{{{target}}}{{}}\\label{{{target}}}"
                for target in targets
                if re.fullmatch(r"[A-Za-z0-9_.:-]+", target)
            )
            latex = (
                "\\phantomsection\n"
                + (target_latex + "\n" if target_latex else "")
                + f"\\{command}*{{{title_latex}}}\n"
                + f"\\addcontentsline{{toc}}{{{toc_level}}}{{{title_latex}}}"
            )
            heading.replace_with(NavigableString(self.token("heading", latex)))

    def convert_container(self, container: Tag) -> str:
        self.normalize_dom(container)
        self.process_figures(container)
        self.process_standalone_assets(container)
        self.process_centered_paragraphs(container)
        self.process_headings(container)
        latex = self.pandoc("".join(str(child) for child in container.contents))
        return self.replace_tokens(latex)


def copy_template(workspace: Path) -> Path:
    template = ASSET_ROOT / "sep-article.tex"
    style = ASSET_ROOT / "sep-paper.sty"
    license_file = ASSET_ROOT / "LICENSE-latex-paper.md"
    for required in (template, style, license_file):
        if not required.exists():
            raise BuildError(f"Skill asset is missing: {required.name}")
    shutil.copy2(style, workspace / style.name)
    shutil.copy2(license_file, workspace / license_file.name)
    destination = workspace / "article.tex"
    shutil.copy2(template, destination)
    return destination


def fill_template(path: Path, values: Mapping[str, str]) -> None:
    source = path.read_text(encoding="utf-8")
    for key, value in values.items():
        marker = f"@@{key}@@"
        if marker not in source:
            raise BuildError(f"LaTeX template marker is missing: {marker}")
        source = source.replace(marker, value)
    leftovers = re.findall(r"@@[A-Z0-9_]+@@", source)
    if leftovers:
        raise BuildError("Unfilled LaTeX template markers: " + ", ".join(leftovers))
    path.write_text(source, encoding="utf-8")


def validate_structure(manifest: Manifest, processed: ProcessedCounts) -> None:
    expected = {
        "notes": manifest.notes,
        "tables": manifest.tables,
        "figures": manifest.figures,
        "images": manifest.images,
        "math_fragments": manifest.math_fragments,
    }
    actual = dataclasses.asdict(processed)
    mismatches = [f"{key}: expected {expected[key]}, processed {actual[key]}" for key in expected if expected[key] != actual[key]]
    if mismatches:
        raise BuildError("Structure accounting failed: " + "; ".join(mismatches))


def compile_pdf(workspace: Path, commands: Mapping[str, str]) -> Tuple[Path, str]:
    env = os.environ.copy()
    env["TEXINPUTS"] = str(workspace) + os.pathsep + env.get("TEXINPUTS", "")
    command = [
        commands["latexmk"],
        "-xelatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "article.tex",
    ]
    try:
        run_command(command, cwd=workspace, env=env)
    except BuildError as exc:
        source = workspace / "article.tex"
        match = re.search(r"article\.tex:(\d+)", str(exc))
        if match and source.exists():
            line_number = int(match.group(1))
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(0, line_number - 4)
            end = min(len(lines), line_number + 3)
            context = "\n".join(f"{index + 1}: {lines[index]}" for index in range(start, end))
            raise BuildError(f"{exc}\nGenerated LaTeX near the failure:\n{context}") from exc
        raise
    pdf = workspace / "article.pdf"
    log = workspace / "article.log"
    if not pdf.exists() or pdf.stat().st_size < 1000:
        raise BuildError("XeLaTeX did not produce a valid-sized PDF.")
    if not log.exists():
        raise BuildError("XeLaTeX did not produce a log for validation.")
    log_text = log.read_text(encoding="utf-8", errors="replace")
    fatal_patterns = (
        r"Undefined control sequence",
        r"LaTeX Error:",
        r"Package .* Error:",
        r"Missing character: There is no",
        r"There were undefined references",
        r"Reference .* undefined",
    )
    found = [pattern for pattern in fatal_patterns if re.search(pattern, log_text, flags=re.I)]
    if found:
        warning_lines = [
            squash_space(line)
            for line in log_text.splitlines()
            if any(re.search(pattern, line, flags=re.I) for pattern in found)
        ]
        detail = " | ".join(warning_lines[:20])
        raise BuildError(
            "LaTeX log contains fatal validation warnings: "
            + ", ".join(found)
            + (f". Details: {detail}" if detail else "")
        )
    overfull = [float(value) for value in re.findall(r"Overfull \\hbox \(([0-9.]+)pt too wide\)", log_text)]
    if overfull and max(overfull) > 12.0:
        contexts = re.findall(
            r"Overfull \\hbox \([0-9.]+pt too wide\).*?(?=\n\n|\Z)",
            log_text,
            flags=re.DOTALL,
        )
        detail = squash_space(contexts[-1])[:500] if contexts else "See the LaTeX log."
        raise BuildError(f"LaTeX produced a fatal overfull box ({max(overfull):.2f}pt): {detail}")
    return pdf, log_text


def validate_pdf(
    pdf: Path,
    manifest: Manifest,
    workspace: Path,
    commands: Mapping[str, str],
) -> QAResult:
    info = run_command([commands["pdfinfo"], str(pdf)], cwd=workspace).stdout
    match = re.search(r"^Pages:\s+(\d+)", info, flags=re.MULTILINE)
    if not match or int(match.group(1)) < 1:
        raise BuildError("pdfinfo could not confirm a non-empty PDF.")
    pages = int(match.group(1))

    extracted = workspace / "article.txt"
    run_command([commands["pdftotext"], "-layout", str(pdf), str(extracted)], cwd=workspace)
    pdf_text = extracted.read_text(encoding="utf-8", errors="replace")
    pdf_counter = collections.Counter(normalized_tokens(pdf_text))
    source_tokens = [token for block in manifest.blocks for token in normalized_tokens(block)]
    overall = counter_coverage(source_tokens, pdf_counter)
    block_coverages = [
        counter_coverage(tokens, pdf_counter)
        for block in manifest.blocks
        if len((tokens := normalized_tokens(block))) >= 5
    ]
    minimum = min(block_coverages) if block_coverages else 1.0
    if overall < 0.995:
        source_counter = collections.Counter(source_tokens)
        missing_counter = source_counter - pdf_counter
        missing = ", ".join(
            f"{token} ({count})" for token, count in missing_counter.most_common(20)
        )
        worst = sorted(
            (
                (counter_coverage(normalized_tokens(block), pdf_counter), squash_space(block))
                for block in manifest.blocks
                if len(normalized_tokens(block)) >= 5
            ),
            key=lambda item: item[0],
        )[:3]
        worst_text = " | ".join(f"{coverage:.1%}: {block[:180]}" for coverage, block in worst)
        raise BuildError(
            f"PDF text coverage is {overall:.2%}; required coverage is 99.50%. "
            f"Most frequent missing tokens: {missing or 'none'}. Worst blocks: {worst_text or 'none'}."
        )
    if minimum < 0.95:
        failing = [
            (counter_coverage(normalized_tokens(block), pdf_counter), block)
            for block in manifest.blocks
            if len(normalized_tokens(block)) >= 5
            and counter_coverage(normalized_tokens(block), pdf_counter) < 0.95
        ]
        example = failing[0][1][:180] if failing else "unknown block"
        raise BuildError(f"A source block has only {minimum:.2%} PDF coverage: {example}")

    render_dir = workspace / "rendered-pages"
    render_dir.mkdir()
    prefix = render_dir / "page"
    run_command(
        [commands["pdftoppm"], "-png", "-r", "72", str(pdf), str(prefix)],
        cwd=workspace,
    )
    rendered = sorted(render_dir.glob("page-*.png"))
    if len(rendered) != pages:
        raise BuildError(f"Rendered {len(rendered)} pages but pdfinfo reports {pages}.")
    for page in rendered:
        if page.stat().st_size < 1000:
            raise BuildError(f"Rendered page is empty or corrupt: {page.name}")
        with page.open("rb") as stream:
            header = stream.read(24)
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            raise BuildError(f"Rendered page is not a valid PNG: {page.name}")
        width, height = struct.unpack(">II", header[16:24])
        if width < 100 or height < 100:
            raise BuildError(f"Rendered page has invalid dimensions: {page.name}")
    return QAResult(pages=pages, overall_coverage=overall, minimum_block_coverage=minimum, rendered_pages=len(rendered))


def resolve_output_path(argument: Optional[str], metadata: ArticleMetadata, overwrite: bool) -> Path:
    default_name = safe_filename(metadata.title, metadata.revision_date)
    if argument:
        requested = Path(argument).expanduser().resolve()
        if requested.exists() and requested.is_dir():
            requested = requested / default_name
        elif requested.suffix.lower() != ".pdf":
            requested = requested.with_suffix(".pdf")
    else:
        requested = Path.cwd() / default_name
    requested.parent.mkdir(parents=True, exist_ok=True)
    return unique_output_path(requested, overwrite)


def atomic_deliver(source: Path, destination: Path) -> None:
    temporary = destination.parent / f".{destination.name}.sep-tmp-{uuid.uuid4().hex}"
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size != source.stat().st_size:
            raise BuildError("Atomic output copy changed the PDF size.")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def build(url_or_slug: str, output: Optional[str], overwrite: bool) -> Tuple[Path, ArticleMetadata, QAResult, Manifest]:
    commands = require_commands()
    source_url = normalize_sep_url(url_or_slug)
    workspace = Path(tempfile.mkdtemp(prefix="sep-pdf-run-"))
    destination: Optional[Path] = None
    try:
        session = make_session()
        response = fetch(session, source_url, official_only=True)
        canonical_url = normalize_sep_url(response.url)
        soup = BeautifulSoup(response.content, "html.parser")
        metadata = extract_metadata(soup, canonical_url)
        preamble, regions = selected_regions(soup)
        manifest = source_manifest(preamble, regions)

        converter = SEPConverter(session, workspace, canonical_url, commands)
        converter.resolve_notes([preamble] + regions, manifest)
        math_macros = render_mathjax_macros(converter.local_mathjax_macros(soup))

        converter.processed.math_fragments += count_math([preamble] + regions)
        preamble_latex = converter.convert_container(preamble)
        body_latex = "\n\n".join(converter.convert_container(region) for region in regions)
        converter.assert_all_tokens_used()
        validate_structure(manifest, converter.processed)

        copyright_latex = converter.pandoc(metadata.copyright_html) if metadata.copyright_html else tex_escape(
            "See the canonical SEP entry for author copyright information."
        )
        article_tex = copy_template(workspace)
        authors_display = " \\and ".join(tex_escape(author) for author in metadata.authors)
        fill_template(
            article_tex,
            {
                "MATH_MACROS": math_macros,
                "PDF_TITLE": tex_pdf_string(metadata.title),
                "PDF_AUTHORS": tex_pdf_string(", ".join(metadata.authors)),
                "CANONICAL_URL_RAW": metadata.canonical_url,
                "REVISION_DATE": metadata.revision_date,
                "TITLE": tex_escape(metadata.title),
                "AUTHORS": authors_display,
                "PUBLICATION_HISTORY": tex_escape(metadata.publication_history),
                "ACCESS_DATE": dt.date.today().isoformat(),
                "PREAMBLE": preamble_latex,
                "ARTICLE_BODY": body_latex,
                "COPYRIGHT": copyright_latex,
            },
        )
        pdf, _ = compile_pdf(workspace, commands)
        qa = validate_pdf(pdf, manifest, workspace, commands)
        destination = resolve_output_path(output, metadata, overwrite)
        atomic_deliver(pdf, destination)
        return destination, metadata, qa, manifest
    finally:
        shutil.rmtree(workspace, ignore_errors=False)
        if workspace.exists():
            raise BuildError(f"Temporary workspace cleanup failed: {workspace}")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download one Stanford Encyclopedia of Philosophy entry and build a verified PDF."
    )
    parser.add_argument("entry", help="Official SEP entry URL or bare entry slug")
    parser.add_argument("--output", help="PDF path or existing output directory; defaults to the current folder")
    parser.add_argument("--overwrite", action="store_true", help="Replace the requested output path after validation")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        destination, metadata, qa, manifest = build(args.entry, args.output, args.overwrite)
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    payload = {
        "status": "ok",
        "pdf": str(destination),
        "title": metadata.title,
        "revision_date": metadata.revision_date,
        "pages": qa.pages,
        "overall_text_coverage": round(qa.overall_coverage, 6),
        "minimum_block_coverage": round(qa.minimum_block_coverage, 6),
        "notes": manifest.notes,
        "tables": manifest.tables,
        "figures": manifest.figures,
        "images": manifest.images,
        "math_fragments": manifest.math_fragments,
        "temporary_workspace_removed": True,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
