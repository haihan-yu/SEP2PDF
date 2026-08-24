#!/usr/bin/env python3

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bs4 import BeautifulSoup

import build_sep_pdf as sep


ENTRY_HTML = b"""<!doctype html><html><head>
<meta name="citation_author" content="Example, Ada">
<meta name="DCTERMS.modified" content="2026-08-20">
</head><body><div id="article"><div id="article-content"><div id="aueditable">
<h1>Example Entry</h1><div id="pubinfo">First published 2020; substantive revision Thu Aug 20, 2026</div>
<div id="preamble"><p>Exact preamble text.</p></div>
<div id="toc"><p>Browser TOC</p></div>
<div id="main-text"><h2 id="One">1. First</h2><p>Exact body text with \(x+y\).</p>
<!--pdf exclude begin--><p>Browser only</p><!--pdf exclude end-->
<!--pdf include <p>PDF only</p> pdf include-->
<p>A note<sup>[<a href="notes.html#note-1">1</a>]</sup>.</p>
<div class="figure"><img src="figure.svg" alt="Figure 1"><p class="center"><span class="figlabel">Figure 1</span></p></div>
<table class="cell-center inner-boxTH"><tr><th rowspan="2">A</th><th colspan="2">B</th></tr><tr><td>C</td><td>D</td></tr></table>
</div>
<div id="bibliography"><h2>Bibliography</h2><p>One source.</p></div>
<div id="academic-tools"><h2>Academic Tools</h2><p>Exclude me.</p></div>
<div id="other-internet-resources"><h2>Other Internet Resources</h2><p>One link.</p></div>
<div id="related-entries"><h2>Related Entries</h2><p>Another entry.</p></div>
</div></div><div id="article-copyright"><p>Copyright 2026 by Ada Example.</p></div></div></body></html>"""


class SEPTests(unittest.TestCase):
    def test_url_validation_and_slug(self):
        self.assertEqual(
            sep.normalize_sep_url("comte"),
            "https://plato.stanford.edu/entries/comte/",
        )
        self.assertEqual(
            sep.normalize_sep_url("https://plato.stanford.edu/archives/spr2025/entries/comte/index.html"),
            "https://plato.stanford.edu/archives/spr2025/entries/comte/",
        )
        with self.assertRaises(sep.BuildError):
            sep.normalize_sep_url("https://example.com/entries/comte/")
        with self.assertRaises(sep.BuildError):
            sep.normalize_sep_url("Auguste Comte")

    def test_metadata_scope_and_pdf_directives(self):
        soup = BeautifulSoup(ENTRY_HTML, "html.parser")
        metadata = sep.extract_metadata(soup, "https://plato.stanford.edu/entries/example/")
        self.assertEqual(metadata.title, "Example Entry")
        self.assertEqual(metadata.authors, ["Example, Ada"])
        self.assertEqual(metadata.revision_date, "2026-08-20")
        preamble, regions = sep.selected_regions(soup)
        combined = " ".join(region.get_text(" ", strip=True) for region in regions)
        self.assertIn("Exact body text", combined)
        self.assertIn("PDF only", combined)
        self.assertNotIn("Browser only", combined)
        self.assertNotIn("Academic Tools", combined)
        self.assertNotIn("Browser TOC", combined)
        self.assertEqual(preamble.get_text(" ", strip=True), "Exact preamble text.")

    def test_manifest_accounts_for_structures(self):
        soup = BeautifulSoup(ENTRY_HTML, "html.parser")
        preamble, regions = sep.selected_regions(soup)
        manifest = sep.source_manifest(preamble, regions)
        self.assertEqual(manifest.notes, 1)
        self.assertEqual(manifest.tables, 1)
        self.assertEqual(manifest.figures, 1)
        self.assertEqual(manifest.images, 1)
        self.assertEqual(manifest.math_fragments, 1)
        self.assertTrue(any("Exact body text" in block for block in manifest.blocks))

    def test_safe_mathjax_parser(self):
        script = r'''window.MathJax = {
          TeX: { Macros: {
            amp: "\\mathbin{\\&}",
            pair: ["(#1,#2)", 2],
            opt: ["#1+#2", 2, "d"],
          } }
        };'''
        macros = sep.parse_mathjax_macros(script)
        self.assertEqual(macros["amp"], r"\mathbin{\&}")
        self.assertEqual(macros["pair"], ["(#1,#2)", 2])
        rendered = sep.render_mathjax_macros(macros)
        self.assertIn(r"\renewcommand{\pair}[2]{(#1,#2)}", rendered)
        self.assertIn(r"\renewcommand{\opt}[2][d]{#1+#2}", rendered)
        with self.assertRaises(sep.BuildError):
            sep.parse_mathjax_macros("window.MathJax={Macros:{bad: function(){}}}")
        self.assertEqual(sep.normalize_math_markup(r"\(n\gt 0\)"), r"\(n> 0\)")
        aligned = BeautifulSoup("<div></div>", "html.parser").div
        aligned.string = r"""\[\begin{align}
\tag{1}
A &= B \\

C &= D
\end{align}\]"""
        with tempfile.TemporaryDirectory(prefix="sep-unit-") as directory:
            converter = sep.SEPConverter(
                SimpleNamespace(), Path(directory), "https://plato.stanford.edu/entries/example/", {"pandoc": shutil.which("pandoc")}
            )
            aligned_latex = converter.convert_container(aligned)
        self.assertIn(r"\begin{align*}", aligned_latex)
        self.assertIn(r"\tag{1}", aligned_latex)
        self.assertNotIn(r"\[", aligned_latex)
        self.assertNotIn(r"\textbackslash", aligned_latex)

    def test_note_resolution_and_table_spans(self):
        commands = {"pandoc": shutil.which("pandoc")}
        self.assertTrue(commands["pandoc"])
        with tempfile.TemporaryDirectory(prefix="sep-unit-") as directory:
            workspace = Path(directory)
            converter = sep.SEPConverter(
                SimpleNamespace(), workspace, "https://plato.stanford.edu/entries/example/", commands
            )
            soup = BeautifulSoup(ENTRY_HTML, "html.parser")
            preamble, regions = sep.selected_regions(soup)
            manifest = sep.source_manifest(preamble, regions)
            note_response = SimpleNamespace(
                content=b'<div id="aueditable"><div id="note-1"><p><a href="index.html#ref-1">1.</a> Exact note text with \\(n\\gt 0\\).</p></div></div>',
                text="",
                url="https://plato.stanford.edu/entries/example/notes.html",
            )
            with mock.patch.object(sep, "fetch", return_value=note_response):
                converter.resolve_notes([preamble] + regions, manifest)
            self.assertEqual(converter.processed.notes, 1)
            self.assertTrue(any("Exact note text" in block for block in manifest.blocks))
            self.assertIn("SEPNOTETOKEN", str(regions[0]))
            note_latex = " ".join(converter.replacements.values())
            self.assertNotIn(r"\gt", note_latex)
            self.assertIn("n> 0", note_latex)
            self.assertEqual(manifest.math_fragments, 2)
            self.assertEqual(converter.processed.math_fragments, 1)
            self.assertFalse(any("SEPMATHTOKEN" in block for block in manifest.blocks))

            table = regions[0].find("table")
            table_latex = converter.table_to_latex(sep.clone_tag(table))
            self.assertIn(r"\SetCell[r=2", table_latex)
            self.assertIn("c=2", table_latex)
            self.assertIn(r"\begin{tblr}", table_latex)

    def test_tokens_may_span_separate_regions_but_all_are_required(self):
        with tempfile.TemporaryDirectory(prefix="sep-unit-") as directory:
            converter = sep.SEPConverter(
                SimpleNamespace(), Path(directory), "https://plato.stanford.edu/entries/example/", {"pandoc": shutil.which("pandoc")}
            )
            first = converter.token("note", r"\footnote{One}")
            second = converter.token("heading", r"\section*{Two}")
            self.assertIn(r"\footnote{One}", converter.replace_tokens(first))
            self.assertIn(r"\section*{Two}", converter.replace_tokens(second))
            converter.assert_all_tokens_used()
            third = converter.token("table", "table")
            with self.assertRaises(sep.BuildError):
                converter.assert_all_tokens_used()

    def test_filename_collision_is_non_destructive(self):
        with tempfile.TemporaryDirectory(prefix="sep-unit-") as directory:
            path = Path(directory) / sep.safe_filename("Example: Entry", "2026-08-20")
            self.assertEqual(path.name, "Example Entry - SEP (2026-08-20).pdf")
            path.write_bytes(b"old")
            self.assertEqual(sep.unique_output_path(path, False).name, "Example Entry - SEP (2026-08-20) (2).pdf")
            self.assertEqual(sep.unique_output_path(path, True), path)

    def test_token_coverage(self):
        pdf = sep.collections.Counter(["one", "two", "two", "three"])
        self.assertEqual(sep.counter_coverage(["one", "two", "three"], pdf), 1.0)
        self.assertAlmostEqual(sep.counter_coverage(["one", "two", "four"], pdf), 2 / 3)
        self.assertEqual(sep.normalized_tokens("scien‐\ntific method"), ["scientific", "method"])
        self.assertEqual(sep.normalized_tokens("source-backed"), ["sourcebacked"])
        self.assertEqual(
            sep.normalized_tokens("God’s end isn't different from Godʼs end"),
            ["god's", "end", "isn't", "different", "from", "god's", "end"],
        )
        ordinal = BeautifulSoup("<p>the 8<sup>ième</sup> letter</p>", "html.parser").p
        self.assertEqual(sep.block_text(ordinal), "the 8ième letter")


if __name__ == "__main__":
    unittest.main()
