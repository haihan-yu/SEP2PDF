---
name: sep-article-pdf
description: Download a current or archived Stanford Encyclopedia of Philosophy entry and create a faithful, polished, source-attributed PDF. Use when the user supplies an SEP URL, slug, or article title and asks to download, save, print, or convert the entry to PDF; do not use for non-SEP webpages or for summaries and rewrites.
---

# SEP Article PDF

Create one validated PDF containing the entry's scholarly content verbatim. Do not summarize, paraphrase, silently omit unsupported structures, or use the Friends of the SEP PDF preview.

## Input and destination

- Accept an official `plato.stanford.edu` current-entry or archive URL. A bare SEP slug is accepted by the builder.
- If the user supplies only a title, resolve it to the official SEP entry before invoking the builder. Confirm ambiguous search results rather than guessing.
- Save to a user-specified path when given; otherwise save in the current task folder.

## Build

Run:

```bash
python3 <skill-dir>/scripts/build_sep_pdf.py <SEP-URL-or-slug> [--output <pdf>] [--overwrite]
```

The script validates dependencies, downloads only the selected entry and its own notes/assets, compiles with XeLaTeX, checks content coverage and structure counts, renders every page for automated PDF checks, copies the PDF atomically, and removes its temporary workspace on both success and failure.

Network access and macOS Quick Look SVG conversion may require approval. Do not install missing software without the user's authorization. Read [references/content-contract.md](references/content-contract.md) when diagnosing extraction, fidelity, licensing, or layout failures.

## Visual QA and delivery

The builder's automated checks do not replace visual inspection.

1. Create one task-owned QA directory under `/tmp`.
2. Render every final PDF page with `pdftoppm` and inspect all page PNGs in manageable batches. Check the title, contents, section transitions, footnotes, mathematical displays, tables, figures, links, final attribution, page numbers, clipping, overlaps, blank pages, and missing glyphs.
3. If a defect is found, fix the skill or conversion and rebuild; do not hand-edit the final PDF.
4. Delete the QA directory and every other task-owned temporary file before reporting completion. Confirm that only the requested PDF remains in the task folder.

Return a clickable link to the PDF and briefly state the entry revision date and the completed validation.
