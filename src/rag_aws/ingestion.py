from pathlib import Path

import pymupdf4llm
from langchain_core.documents import Document


def ingest_pdf(pdf_path:str | Path, output_path:str | Path):
    "extract a pdf page by page using pymupdf4llm"

    pdf_path=Path(pdf_path)
    output_path=Path(output_path)

    #validate input
    if not pdf_path.exists():
        raise FileNotFoundError(f"pdf not found : {pdf_path}")

    if pdf_path.suffix.lower()!=".pdf":
        raise ValueError(f"Expected a pdf file got {pdf_path.suffix}")

    #create output directory
    output_path.parent.mkdir(parents=True,exist_ok=True)

    #extract pdf page by page
    pages=pymupdf4llm.to_markdown(str(pdf_path),page_chunks=True)

    documents:list[Document]=[]
    combined_parts:list[str]=[]

    for page_number,page in enumerate(pages,start=1):
        markdown=page["text"]

        #create langchain document
        document=Document(page_content=markdown,metadata={"source":pdf_path.name,"page":page_number})

        documents.append(document)

        #add page marker for combined file (debugging only, not read back by chunking)
        combined_parts.append(f"<!-- page: {page_number} -->\n{markdown}")

    #save one combined markdown file for manual inspection
    output_path.write_text("\n\n".join(combined_parts),encoding="utf-8")

    print(f"Processed {len(documents)} pages.")
    print(f"Combined markdown saved to: {output_path}")

    return documents


if __name__ == "__main__":
    pdf_path = Path("data/raw/aws-overview.pdf")
    output_path = Path("data/processed/aws-overview.md")

    documents = ingest_pdf(pdf_path, output_path)

    print("\nFirst page metadata:")
    print(documents[0].metadata)

    print("\nFirst page content:")
    print(documents[0].page_content[:1000])

    