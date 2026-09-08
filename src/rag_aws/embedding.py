from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings

MODEL_NAME="BAAI/bge-large-en-v1.5"
EMBEDDING_DIMENSION=1024

#bge query instruction recommended for retrieval
BGE_QUERY_INSTRUCTION="Represent this sentence for searching relevant passages: "
QUERY_INSTRUCTION=BGE_QUERY_INSTRUCTION


def get_embedding_model(model_name:str=MODEL_NAME)->HuggingFaceEmbeddings:
    #load huggingface embedding model with normalized embeddings for cosine similarity
    encode_kwargs={"normalize_embeddings":True}
    return HuggingFaceEmbeddings(
        model_name=model_name,
        encode_kwargs=encode_kwargs,
    )


def embed_query_text(query:str,embedding_model:HuggingFaceEmbeddings | None=None)->list[float] | str:
    #prepend bge query instruction before embedding
    instructed_query=f"{BGE_QUERY_INSTRUCTION}{query}"
    if embedding_model is None:
        return instructed_query
    return embedding_model.embed_query(instructed_query)


def embed_documents(documents:list[Document],embedding_model:HuggingFaceEmbeddings)->list[list[float]]:
    #extract text content from each document
    texts=[doc.page_content for doc in documents]

    #batch embed all document texts
    embeddings=embedding_model.embed_documents(texts)

    return embeddings


if __name__ == "__main__":
    #demonstrate loading the model without embedding any documents
    embedding_model=get_embedding_model()

    print(f"\nModel successfully loaded: {MODEL_NAME}")
    print(f"Embedding dimensions: {EMBEDDING_DIMENSION}")

