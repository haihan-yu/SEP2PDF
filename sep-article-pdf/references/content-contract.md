# SEP PDF content contract

Use this reference only when diagnosing extraction, fidelity, licensing, or rendering.

## Included material

The generated PDF contains, when present:

- entry title, all credited authors, publication and revision history;
- the full preamble and main text;
- every note referenced from the included content, placed as a footnote;
- bibliography, other internet resources, related entries, and acknowledgments;
- figures, figure labels, tables, display and inline mathematics, and entry-local MathJax macros;
- the canonical URL, access date, author copyright, SEP attribution, and ISSN.

The wording is source-derived. Formatting-only normalization includes whitespace reflow, HTML entity decoding, conversion of relative links to absolute links, PDF-specific alternatives encoded by SEP comments, and LaTeX escaping.

## Excluded material

Exclude site navigation, search, donation banners, site footer, the browser-oriented entry TOC, scripts and styles, and the `#academic-tools` promotional block. Do not crawl or embed linked SEP articles.

Honor SEP `pdf include` comments and omit content bracketed by `pdf exclude begin` / `pdf exclude end`. Treat malformed directives as errors.

## Failure policy

Never silently drop a selected note, image, table, math block, or required article region. HTTP failures, unsupported assets, malformed table spans, unresolved notes, unsafe URLs, missing dependencies, LaTeX errors, missing glyphs, failed coverage thresholds, and invalid PDFs stop the build before delivery.

## Fidelity checks

The builder records an ephemeral manifest of selected text blocks and structure counts. It requires:

- every selected block to enter the conversion pipeline;
- equal source/processed counts for notes, tables, figures, and math fragments;
- at least 99.5 percent normalized source-token coverage in PDF-extracted text;
- at least 95 percent coverage for each source prose block with five or more tokens;
- no missing glyphs, undefined controls, unresolved references, missing assets, fatal overfull boxes, or zero-byte/blank rendered pages.

Math is checked structurally because PDF text extraction does not reproduce TeX notation reliably.

## Attribution and use

SEP states that individual users may read, download, copy, print, search, or link to the full text of an entry, subject to its terms and non-commercial distribution limits. Preserve the entry's author copyright, canonical URL, revision history, and SEP attribution in every PDF. See <https://plato.stanford.edu/info.html#c>.

## Temporary files

All HTML, note pages, MathJax configuration, assets, TeX sources, auxiliary files, extracted text, manifests, logs, and rendered PNGs are build intermediates. Remove the task-owned temporary tree in a `finally` path on success and failure. Visual-QA PNGs created after the builder returns must likewise be deleted before the task finishes.
