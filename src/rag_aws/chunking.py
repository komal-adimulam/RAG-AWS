import re
import uuid
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter


def resolve_data_path(file_path: str | Path = "data/processed/aws-overview.md") -> Path:
    """Resolve file path relative to current working directory or repository root."""
    path = Path(file_path)
    if path.exists():
        return path

    # Search upward for repo root
    current = Path(__file__).resolve().parent
    while current.parent != current:
        candidate = current / file_path
        if candidate.exists():
            return candidate
        current = current.parent

    return path


def load_data(file_path: str | Path = "data/processed/aws-overview.md") -> str:
    """Read processed markdown file."""
    path = resolve_data_path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Markdown file not found: {path}")
    return path.read_text(encoding="utf-8")


# Alias for backward compatibility with pipeline.py
load_stitched_text = load_data


def generate_hierarchical_chunks(
    markdown_text: str,
    source: str = "aws-overview.pdf",
    initial_page: int = 11,
) -> tuple[list[Document], list[Document]]:
    """Generate two-tier hierarchical chunks (Parent/Child pattern) with capped parent sizes
    and sequential page tracking.

    Returns:
        tuple[list[Document], list[Document]]: (parent_chunks, child_chunks)
    """
    # 1. Split document on "###" (category) and "####" (service) headers
    md_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ("###", "Header 3"),
            ("####", "Header 4"),
        ],
        strip_headers=True,
    )
    header_docs = md_splitter.split_text(markdown_text)

    # 2. Cap parent chunks at ~2,000 characters
    parent_splitter = RecursiveCharacterTextSplitter(
        chunk_size=2000,
        chunk_overlap=100,
    )

    raw_parents: list[Document] = []
    for doc in header_docs:
        if len(doc.page_content) > 2000:
            raw_parents.extend(parent_splitter.split_documents([doc]))
        else:
            raw_parents.append(doc)

    # 3. Page tracking with running state and cleaning comments
    current_page = initial_page
    parent_chunks: list[Document] = []

    for parent in raw_parents:
        # Find all page markers in the text before stripping
        markers = re.findall(r"<!--\s*PAGE_(\d+)\s*-->", parent.page_content)
        if markers:
            # Active page for this chunk is the first encountered marker,
            # and current_page advances to the last marker for subsequent chunks.
            chunk_page = int(markers[0])
            current_page = int(markers[-1])
        else:
            chunk_page = current_page

        # Strip all <!-- PAGE_X --> HTML comments from content
        cleaned_content = re.sub(r"<!--\s*PAGE_.*?-->\n?", "", parent.page_content).strip()

        parent.page_content = cleaned_content
        parent.metadata["id"] = str(uuid.uuid4())
        parent.metadata["page"] = chunk_page
        if "source" not in parent.metadata:
            parent.metadata["source"] = source

        parent_chunks.append(parent)

    # 4. Generate Child Chunks within each Parent Chunk
    child_splitter = RecursiveCharacterTextSplitter(
        chunk_size=400,
        chunk_overlap=50,
    )

    child_chunks: list[Document] = []
    for parent in parent_chunks:
        sub_docs = child_splitter.split_documents([parent])
        for child in sub_docs:
            child.metadata["parent_id"] = parent.metadata["id"]
            child.metadata["id"] = str(uuid.uuid4())
            # Inherit source and page metadata from parent
            child.metadata["page"] = parent.metadata["page"]
            child.metadata["source"] = parent.metadata.get("source", source)
            # Ensure text content is clean
            child.page_content = re.sub(r"<!--\s*PAGE_.*?-->\n?", "", child.page_content).strip()
            child_chunks.append(child)

    return parent_chunks, child_chunks


def chunk_document(
    file_path: str | Path = "data/processed/aws-overview.md",
    source: str = "aws-overview.pdf",
    initial_page: int = 11,
) -> tuple[list[Document], list[Document]]:
    """Process processed markdown file and return (parent_chunks, child_chunks)."""
    text = load_data(file_path)
    return generate_hierarchical_chunks(
        markdown_text=text,
        source=source,
        initial_page=initial_page,
    )


def chunk_documents(
    text_or_path: str | Path,
    source: str = "aws-overview.pdf",
) -> list[Document]:
    """Provide child chunks for vector indexing and BM25 retrieval."""
    if isinstance(text_or_path, Path) or (
        isinstance(text_or_path, str) and (Path(text_or_path).exists() or "\n" not in text_or_path)
    ):
        path = Path(text_or_path)
        if path.exists():
            _, child_chunks = chunk_document(file_path=path, source=source)
            return child_chunks

    _, child_chunks = generate_hierarchical_chunks(markdown_text=str(text_or_path), source=source)
    return child_chunks


def main() -> tuple[list[Document], list[Document]]:
    parent_chunks, child_chunks = chunk_document()

    print(f"Total parent chunks generated: {len(parent_chunks)}")
    print(f"Total child chunks generated: {len(child_chunks)}")

    if child_chunks:
        sample = child_chunks[0]
        print("\n--- Sample Child Chunk ---")
        print(f"page_content:\n{sample.page_content}\n")
        print("metadata:")
        print(f"  id: {sample.metadata.get('id')}")
        print(f"  parent_id: {sample.metadata.get('parent_id')}")
        print(f"  page: {sample.metadata.get('page')}")
        print(f"  full metadata: {sample.metadata}")

    return parent_chunks, child_chunks


if __name__ == "__main__":
    main()

