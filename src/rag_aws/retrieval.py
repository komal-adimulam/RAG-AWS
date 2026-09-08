from langchain_community.retrievers import BM25Retriever
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from sentence_transformers import CrossEncoder

try:
    from rag_aws.embedding import embed_query_text
except ImportError:
    from embedding import embed_query_text

RERANKER_MODEL_NAME="BAAI/bge-reranker-base"
_reranker_model:CrossEncoder | None=None


def get_reranker_model(model_name:str=RERANKER_MODEL_NAME)->CrossEncoder:
    #lazy load cross-encoder model so import does not trigger download
    global _reranker_model
    if _reranker_model is None:
        _reranker_model=CrossEncoder(model_name)
    return _reranker_model


def reciprocal_rank_fusion(ranked_lists:list[list[Document]],k:int=60)->list[Document]:
    #track fused rrf scores and deduplicate by page_content
    rrf_scores:dict[str,float]={}
    doc_map:dict[str,Document]={}

    for doc_list in ranked_lists:
        for rank,doc in enumerate(doc_list,start=1):
            content=doc.page_content
            if content not in doc_map:
                doc_map[content]=doc
                rrf_scores[content]=0.0
            #standard rrf formula sum(1 / (k + rank))
            rrf_scores[content]+=1.0 / (k + rank)

    #sort documents by fused rrf score descending
    sorted_contents=sorted(rrf_scores.keys(),key=lambda c:rrf_scores[c],reverse=True)
    return [doc_map[c] for c in sorted_contents]


def rerank(query:str,documents:list[Document],top_n:int=5)->list[Document]:
    if not documents:
        return []

    #score query-document pairs with cross-encoder
    reranker=get_reranker_model()
    pairs=[(query,doc.page_content) for doc in documents]
    scores=reranker.predict(pairs)

    #sort documents by reranker score descending
    scored_docs=sorted(zip(documents,scores),key=lambda x:x[1],reverse=True)
    return [doc for doc,_ in scored_docs[:top_n]]


def build_bm25_retriever(documents:list[Document],k:int=15)->BM25Retriever:
    #build bm25 retriever from full chunk document list
    retriever=BM25Retriever.from_documents(documents=documents)
    retriever.k=k
    return retriever


def _matches_filter(metadata:dict,filter_dict:dict[str,str])->bool:
    #check if document metadata matches all key-value constraints in filter_dict
    for key,expected_val in filter_dict.items():
        if key in ("service","h3"):
            #check if service appears in h2, h3, or h4 (e.g. S3 is in h2 while EC2 is in h3)
            headers=[metadata.get(k,"") for k in ("h2","h3","h4") if k in metadata]
            if not any(expected_val.lower() in h.lower() for h in headers):
                return False
            continue

        actual_val=metadata.get(key)
        if actual_val is None:
            return False
        #case-insensitive substring match for flexible metadata querying
        if isinstance(actual_val,str) and isinstance(expected_val,str):
            if expected_val.lower() not in actual_val.lower():
                return False
        elif actual_val!=expected_val:
            return False
    return True


def retrieve(
    query:str,
    faiss_store:FAISS,
    bm25_retriever:BM25Retriever,
    dense_k:int=10,
    bm25_k:int=10,
    rerank_top_n:int=5,
    filter_dict:dict[str,str] | None=None,
)->list[Document]:
    #1. apply bge query instruction prefix for dense search
    prefixed_query=str(embed_query_text(query))

    #2. retrieve top dense_k candidates from faiss (with metadata filter if provided)
    search_kwargs={"k":dense_k}
    if filter_dict:
        search_kwargs["filter"]=lambda meta:_matches_filter(meta,filter_dict)
        search_kwargs["fetch_k"]=max(dense_k * 3, 30)

    dense_retriever=faiss_store.as_retriever(search_kwargs=search_kwargs)
    dense_docs=dense_retriever.invoke(prefixed_query)

    #3. retrieve top bm25_k candidates using original unprefixed query
    if filter_dict:
        bm25_retriever.k=max(bm25_k * 3, 30)
        raw_bm25=bm25_retriever.invoke(query)
        bm25_docs=[doc for doc in raw_bm25 if _matches_filter(doc.metadata,filter_dict)][:bm25_k]
    else:
        bm25_retriever.k=bm25_k
        bm25_docs=bm25_retriever.invoke(query)

    #4. fuse both candidate lists using reciprocal rank fusion
    fused_docs=reciprocal_rank_fusion([dense_docs,bm25_docs])

    #5. rerank fused candidates with cross-encoder for final precision
    final_docs=rerank(query=query,documents=fused_docs,top_n=rerank_top_n)

    return final_docs


if __name__ == "__main__":
    print("retrieval module ready: exposes retrieve, build_bm25_retriever, reciprocal_rank_fusion, and rerank for use by pipeline.py.")

