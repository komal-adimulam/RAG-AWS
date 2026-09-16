import re
from pathlib import Path

import pymupdf4llm
from langchain_core.documents import Document


def clean_page_text(text: str) -> str:
    """Remove recurring headers, footers, copyright notices, and standalone page numbers."""
    # Remove copyright notices (both exact string and flexible regex match)
    text = text.replace(
        "Copyright © 2026 Amazon Web Services, Inc. and/or its affiliates. All rights reserved.",
        "",
    )
    text = re.sub(
        r"Copyright\s+©\s+\d{4}\s+Amazon Web Services,\s*Inc\.\s+and/or\s+its\s+affiliates\.\s*All\s+rights\s+reserved\.?",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Remove recurring headers and footers (line-level to preserve markdown headings like '## **Overview...**')
    text = re.sub(r"^[ \t]*Overview of Amazon Web Services[ \t]*$\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*AWS Whitepaper[ \t]*$\n?", "", text, flags=re.MULTILINE)

    # Remove standalone page numbers (lines consisting only of digits)
    text = re.sub(r"^[ \t]*\d+[ \t]*$\n?", "", text, flags=re.MULTILINE)

    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return text


def ingest_pdf(
    pdf_path: str | Path,
    output_path: str | Path | None = None,
    toc_end_page: int = 10,
) -> Document:
    """Extract and prepare PDF text for hierarchical chunking.

    - Skips Table of Contents / front-matter pages (first 10 pages).
    - Cleans recurring headers, footers, copyright notices, and page numbers.
    - Injects hidden page markers: <!-- PAGE_{page_number} -->.
    - Returns a single LangChain Document with metadata {"source": pdf_path.name}.
    """
    pdf_path = Path(pdf_path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF file, got {pdf_path.suffix}")

    # Extract PDF page by page
    pages = pymupdf4llm.to_markdown(str(pdf_path), page_chunks=True)

    combined_parts: list[str] = []

    for page_number, page in enumerate(pages, start=1):
        # Skip Table of Contents and front matter pages
        if page_number <= toc_end_page:
            continue

        cleaned_text = clean_page_text(page.get("text", ""))
        if not cleaned_text:
            continue

        # Inject hidden HTML comment marker before appending page text
        combined_parts.append(f"\n\n<!-- PAGE_{page_number} -->\n\n{cleaned_text}")

    combined_markdown = "".join(combined_parts).strip()

    # Optionally write combined markdown to file for inspection
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(combined_markdown, encoding="utf-8")
        print(f"Combined markdown saved to: {output_path}")

    print(f"Processed {len(pages) - toc_end_page} content pages (skipped first {toc_end_page} TOC/cover pages).")

    # Return a single LangChain Document
    return Document(
        page_content=combined_markdown,
        metadata={"source": pdf_path.name},
    )


if __name__ == "__main__":
    # Support common filenames in data/raw
    raw_dir = Path("data/raw")
    candidates = [
        raw_dir / "aws-overview (1).pdf",
        raw_dir / "aws-overview(1).pdf",
        raw_dir / "aws-overview.pdf",
    ]
    pdf_path = next((p for p in candidates if p.exists()), candidates[0])
    output_path = Path("data/processed/aws-overview.md")

    document = ingest_pdf(pdf_path, output_path)

    print("\nDocument metadata:")
    print(document.metadata)

    print(f"\nTotal characters: {len(document.page_content)}")
    print("\nFirst 1000 characters of combined content:")
    print(document.page_content[:1000])