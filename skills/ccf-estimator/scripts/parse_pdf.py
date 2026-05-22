#!/usr/bin/env python3
"""
CCF PDF Parser — Extracts text from bid documents, plans, specs, proposals.
Usage: python parse_pdf.py --file <path> [--pages all|1-5|3]
"""

import argparse
import json
import sys
from pathlib import Path

import pdfplumber


def extract_text(filepath, pages=None):
    """Extract text from PDF, optionally specific pages.
    pages: None=all, "1-5"=range, "3"=single page (1-indexed)
    """
    path = Path(filepath)
    if not path.exists():
        return {"error": f"File not found: {filepath}"}

    pdf = pdfplumber.open(str(path))
    total_pages = len(pdf.pages)

    # Parse page range
    if pages is None or pages == "all":
        page_indices = range(total_pages)
    elif "-" in str(pages):
        start, end = str(pages).split("-")
        page_indices = range(int(start) - 1, min(int(end), total_pages))
    else:
        idx = int(pages) - 1
        page_indices = [idx] if 0 <= idx < total_pages else []

    result = {
        "file": str(path.name),
        "total_pages": total_pages,
        "pages": [],
    }

    for i in page_indices:
        page = pdf.pages[i]
        text = page.extract_text() or ""
        tables = page.extract_tables() or []

        page_data = {
            "page_number": i + 1,
            "text": text,
            "tables": tables if tables else None,
            "width": float(page.width),
            "height": float(page.height),
        }
        result["pages"].append(page_data)

    pdf.close()
    return result


def extract_scope_sections(text):
    """Try to identify common bid document sections in extracted text."""
    sections = {}
    current_section = "header"
    current_lines = []

    scope_keywords = [
        "scope of work", "painting", "wallcovering", "wall covering",
        "finish schedule", "color schedule", "paint schedule",
        "division 09", "section 09", "09 91", "09 96", "09 72",
        "coating", "primer", "finish", "substrate",
    ]

    for line in text.split("\n"):
        line_lower = line.strip().lower()

        # Detect section breaks
        if any(kw in line_lower for kw in ["scope of work", "general requirements", "part 1", "part 2", "part 3"]):
            if current_lines:
                sections[current_section] = "\n".join(current_lines)
            current_section = line.strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_section] = "\n".join(current_lines)

    # Flag painting-relevant lines
    painting_lines = []
    for line in text.split("\n"):
        if any(kw in line.lower() for kw in scope_keywords):
            painting_lines.append(line.strip())

    return {
        "sections": sections,
        "painting_relevant_lines": painting_lines,
    }


def main():
    parser = argparse.ArgumentParser(description="CCF PDF Parser")
    parser.add_argument("--file", required=True, help="Path to PDF file")
    parser.add_argument("--pages", default="all", help="Pages to extract: all, 1-5, 3")
    parser.add_argument("--mode", default="text", choices=["text", "scope"],
                        help="text=raw extraction, scope=try to find painting scope sections")
    args = parser.parse_args()

    result = extract_text(args.file, args.pages)

    if "error" in result:
        print(json.dumps(result), file=sys.stderr)
        sys.exit(1)

    if args.mode == "scope":
        full_text = "\n".join(p["text"] for p in result["pages"])
        scope = extract_scope_sections(full_text)
        result["scope_analysis"] = scope

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
