from pathlib import Path

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from langchain_core.documents import Document

def load_text(md_path:str |Path):
    md_path=Path(md_path)

    if not md_path.exists():
        raise FileNotFoundError(f"markdown not found: {md_path}")
    
    return md_path.read_text(encoding="utf-8")

load_stitched_text=load_text

def prepend_headers(document:Document):

    header_path=[document.metadata[key] for key in ("h1","h2","h3","h4") if key in document.metadata]

    if not header_path:
        return document

    prefix=">".join(header_path)

    #reassign page_content only, metadata stays untouched
    document.page_content=f"{prefix}\n\n{document.page_content}"

    return document

def chunk_documents(text:str,chunk_size:int=800,chunk_overlap=100):
    #hierarchical chunking : header aware split first then size based split within each section

    headers_to_split_on=[
        ("#","h1"),
        ("##","h2"),
        ("###","h3"),
        ("####","h4"),
    ]

    #split by markdown header structure (meaning based boundaries)
    header_splitter=MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on,strip_headers=True)

    header_sections=header_splitter.split_text(text)

    #cap size within each section (size based boundaries)
    char_splitter=RecursiveCharacterTextSplitter(chunk_size=chunk_size,chunk_overlap=chunk_overlap)
    final_chunks:list[Document]=[]

    for section in header_sections:
        #if the section is still too big split further
        sub_chunks=char_splitter.split_documents([section])
        final_chunks.extend(sub_chunks)

    #prepend header path into content for every final chunk
    final_chunks=[prepend_headers(chunk) for chunk in final_chunks]

    return final_chunks


if __name__ == "__main__":
    md_path=Path("data/processed/aws-overview.md")

    text=load_text(md_path)
    chunks=chunk_documents(text)

    print(f"\nTotal chunks: {len(chunks)}")

    print("\nSample chunk metadata:")
    print(chunks[5].metadata)

    print("\nSample chunk content:")
    print(chunks[5].page_content)