from pathlib import Path

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings


def index_exists(index_path:str | Path)->bool:
    #check if faiss index and metadata pickle files exist on disk
    path=Path(index_path)
    return (path / "index.faiss").exists() and (path / "index.pkl").exists()


def build_index(documents:list[Document],embedding_model:Embeddings,index_path:str | Path)->FAISS:
    path=Path(index_path)
    path.mkdir(parents=True,exist_ok=True)

    #build faiss index directly from documents (from_documents handles batch embedding internally)
    vector_store=FAISS.from_documents(documents=documents,embedding=embedding_model)

    #persist index to disk
    vector_store.save_local(str(path))

    return vector_store


def load_index(index_path:str | Path,embedding_model:Embeddings)->FAISS:
    path=Path(index_path)

    #validate index exists before loading
    if not index_exists(path):
        raise FileNotFoundError(f"faiss index not found at: {path}")

    #allow_dangerous_deserialization is required for faiss pickle deserialization
    #acceptable here because index files are created and managed internally within our pipeline
    vector_store=FAISS.load_local(
        folder_path=str(path),
        embeddings=embedding_model,
        allow_dangerous_deserialization=True,
    )

    return vector_store


if __name__ == "__main__":
    print("indexing module ready: exposes build_index, load_index, and index_exists for use by pipeline.py.")

