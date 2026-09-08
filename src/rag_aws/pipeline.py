import time
from pathlib import Path

from langchain_community.retrievers import BM25Retriever
from langchain_community.vectorstores import FAISS
from langchain_google_genai import ChatGoogleGenerativeAI

try:
    from rag_aws.config import INDEX_PATH, PROCESSED_MD_PATH, RAW_PDF_PATH
except ImportError:
    try:
        from config import INDEX_PATH, PROCESSED_MD_PATH, RAW_PDF_PATH
    except ImportError:
        #default paths (these should move to config.py)
        RAW_PDF_PATH=Path("data/raw/aws-overview.pdf")
        PROCESSED_MD_PATH=Path("data/processed/aws-overview.md")
        INDEX_PATH=Path("data/vector_store/faiss_index")

try:
    from rag_aws.ingestion import ingest_pdf
    from rag_aws.chunking import load_stitched_text, chunk_documents
    from rag_aws.embedding import get_embedding_model
    from rag_aws.indexing import build_index, load_index, index_exists
    from rag_aws.retrieval import build_bm25_retriever, retrieve
    from rag_aws.generation import get_llm, generate_answer
except ImportError:
    from ingestion import ingest_pdf
    from chunking import load_stitched_text, chunk_documents
    from embedding import get_embedding_model
    from indexing import build_index, load_index, index_exists
    from retrieval import build_bm25_retriever, retrieve
    from generation import get_llm, generate_answer


def setup_pipeline()->tuple[FAISS,BM25Retriever]:
    #check if faiss index already exists on disk
    if not index_exists(INDEX_PATH):
        print("Index not found. Running full ingestion, chunking, and index build...")
        print("Ingesting PDF...")
        ingest_pdf(RAW_PDF_PATH,PROCESSED_MD_PATH)

        print("Chunking documents...")
        stitched_text=load_stitched_text(PROCESSED_MD_PATH)
        chunks=chunk_documents(stitched_text)

        print("Loading embedding model...")
        embedding_model=get_embedding_model()

        print("Building FAISS index (this may take a moment)...")
        faiss_store=build_index(documents=chunks,embedding_model=embedding_model,index_path=INDEX_PATH)
    else:
        print("Existing index found. Loading FAISS index from disk...")
        print("Reconstructing chunks for BM25 retriever...")
        ingest_pdf(RAW_PDF_PATH,PROCESSED_MD_PATH)
        stitched_text=load_stitched_text(PROCESSED_MD_PATH)
        chunks=chunk_documents(stitched_text)

        print("Loading embedding model for FAISS vectorstore...")
        embedding_model=get_embedding_model()
        faiss_store=load_index(index_path=INDEX_PATH,embedding_model=embedding_model)

    print("Building BM25 retriever...")
    bm25_retriever=build_bm25_retriever(documents=chunks)

    print("Pipeline setup complete.")
    return faiss_store,bm25_retriever


#keyword mappings for automatic metadata filtering (option 1)
SERVICE_KEYWORDS:dict[str,str]={
    r"\b(ec2|elastic compute cloud)\b":"EC2",
    r"\b(s3|simple storage service)\b":"S3",
    r"\b(glacier)\b":"Glacier",
    r"\b(ebs|elastic block store)\b":"EBS",
    r"\b(dynamodb|dynamo)\b":"DynamoDB",
    r"\b(rds|relational database service)\b":"RDS",
    r"\b(redshift)\b":"Redshift",
    r"\b(elasticache|elasti\s*cache)\b":"ElastiCache",
    r"\b(vpc|virtual private cloud)\b":"VPC",
    r"\b(iam|identity and access management)\b":"IAM",
    r"\b(cloudwatch)\b":"CloudWatch",
    r"\b(cloudformation)\b":"CloudFormation",
    r"\b(elastic beanstalk|beanstalk)\b":"Elastic Beanstalk",
    r"\b(auto\s*scaling|autoscaling)\b":"Auto Scaling",
    r"\b(route\s*53)\b":"Route 53",
    r"\b(cloudfront)\b":"CloudFront",
    r"\b(storage gateway)\b":"Storage Gateway",
    r"\b(cloudhsm)\b":"CloudHSM",
    r"\b(kinesis)\b":"Kinesis",
    r"\b(emr|elastic mapreduce)\b":"EMR",
    r"\b(sqs|simple queue service)\b":"SQS",
    r"\b(sns|simple notification service)\b":"SNS",
    r"\b(ses|simple email service)\b":"SES",
}

CATEGORY_KEYWORDS:dict[str,str]={
    r"\b(compute)\b":"Compute",
    r"\b(database|databases)\b":"Database",
    r"\b(storage)\b":"Storage",
    r"\b(networking|network)\b":"Networking",
    r"\b(security)\b":"Security",
    r"\b(deployment|deploy)\b":"Deployment",
}


def parse_query_and_filter(raw_input:str)->tuple[str,dict[str,str] | None]:
    import re

    #1. check for explicit manual tags if user wants to force a filter (e.g. [category: Compute])
    cat_match=re.match(r"^\[(?:filter|category|h2):\s*([^\]]+)\]\s*(.*)$",raw_input,re.IGNORECASE)
    if cat_match:
        category,clean_query=cat_match.groups()
        return clean_query.strip(),{"h2":category.strip()}

    svc_match=re.match(r"^\[(?:service|h3):\s*([^\]]+)\]\s*(.*)$",raw_input,re.IGNORECASE)
    if svc_match:
        service,clean_query=svc_match.groups()
        return clean_query.strip(),{"h3":service.strip()}

    #2. automatic keyword detection (option 1: no brackets required)
    matched_services:list[str]=[]
    for pattern,service in SERVICE_KEYWORDS.items():
        if re.search(pattern,raw_input,re.IGNORECASE):
            if service not in matched_services:
                matched_services.append(service)

    #if exactly one specific service is mentioned, filter directly to that service
    if len(matched_services)==1:
        return raw_input,{"h3":matched_services[0]}
    elif len(matched_services)>1:
        #multi-service comparison query -> search across entire document
        return raw_input,None

    #if no specific service mentioned, check for broader category keywords
    matched_categories:list[str]=[]
    for pattern,category in CATEGORY_KEYWORDS.items():
        if re.search(pattern,raw_input,re.IGNORECASE):
            if category not in matched_categories:
                matched_categories.append(category)

    if len(matched_categories)==1:
        return raw_input,{"h2":matched_categories[0]}

    #no filter detected -> search entire document
    return raw_input,None


def answer_query(
    query:str,
    faiss_store:FAISS,
    bm25_retriever:BM25Retriever,
    llm:ChatGoogleGenerativeAI | None=None,
    filter_dict:dict[str,str] | None=None,
)->str:
    #retrieve top reranked chunks using hybrid search (with optional metadata filter)
    relevant_chunks=retrieve(
        query=query,
        faiss_store=faiss_store,
        bm25_retriever=bm25_retriever,
        filter_dict=filter_dict,
    )

    if not relevant_chunks:
        return f"No relevant information found matching filter: {filter_dict}"

    #generate answer grounded in retrieved context
    answer=generate_answer(query=query,documents=relevant_chunks,llm=llm)

    return answer


def run_cli()->None:
    #setup pipeline components once
    faiss_store,bm25_retriever=setup_pipeline()

    #initialize llm once
    llm=get_llm()

    print("\n" + "="*50)
    print("RAG Pipeline Ready. Ask questions below.")
    print("Type 'quit' or 'escape' to exit.")
    print("="*50 + "\n")

    #interactive query loop
    while True:
        try:
            user_query=input("\nQuestion: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting...")
            break

        #skip empty inputs
        if not user_query:
            continue

        #exit conditions
        if user_query.lower() in ("quit", "escape", "exit"):
            print("Exiting pipeline.")
            break

        print("\nRetrieving context and generating answer...")
        start_time=time.perf_counter()
        answer=answer_query(
            query=user_query,
            faiss_store=faiss_store,
            bm25_retriever=bm25_retriever,
            llm=llm,
        )
        elapsed=time.perf_counter() - start_time
        print(f"\n--- Answer ---\n{answer}")
        print(f"\n[Time taken: {elapsed:.2f}s]\n")


if __name__ == "__main__":
    run_cli()