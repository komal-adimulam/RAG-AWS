import os
from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()

#configurable gemini model
DEFAULT_MODEL="gemini-3.1-flash-lite"

SYSTEM_PROMPT="""You are a helpful and precise assistant answering questions about AWS.
Answer the question ONLY using the provided context below.
Do not put source citations inline in your sentences. Instead, write a clean, well-structured answer, and at the very end of your response, list all unique sources under a "Sources:" heading.
If the provided context does not contain enough information to answer the question, respond with exactly:
"I don't have enough information to answer that."
Do not rely on any outside or prior knowledge about AWS."""


def get_llm(model:str=DEFAULT_MODEL,temperature:float | None=None)->ChatGoogleGenerativeAI:
    #validate api key is present in environment
    api_key=os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not found in environment variables. Please set it in your .env file.")

    kwargs:dict[str,object]={"model":model,"google_api_key":api_key}
    if temperature is not None:
        kwargs["temperature"]=temperature

    return ChatGoogleGenerativeAI(**kwargs)


def format_context(documents:list[Document])->str:
    #format each chunk with citation reference from header metadata
    formatted_chunks:list[str]=[]

    for doc in documents:
        headers=[doc.metadata[key] for key in ("h1","h2","h3","h4") if key in doc.metadata]
        citation=f"[Source: {' > '.join(headers)}]" if headers else "[Source: Document]"
        formatted_chunks.append(f"{citation}\n{doc.page_content}")

    return "\n\n---\n\n".join(formatted_chunks)


def build_prompt(query:str,documents:list[Document])->str:
    #combine system prompt, retrieved context chunks, and user query
    context=format_context(documents)
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Context:\n"
        f"{context}\n\n"
        f"Question:\n"
        f"{query}\n\n"
        f"Answer:"
    )


def generate_answer(query:str,documents:list[Document],llm:ChatGoogleGenerativeAI | None=None)->str:
    #use provided llm instance or initialize one
    if llm is None:
        llm=get_llm()

    prompt=build_prompt(query=query,documents=documents)
    response=llm.invoke(prompt)

    #extract clean text from content parts, ignoring signature metadata
    if isinstance(response.content, str):
        return response.content
    elif isinstance(response.content, list):
        return "".join(
            part["text"] for part in response.content
            if isinstance(part, dict) and "text" in part
        )

    return str(response.content)


if __name__ == "__main__":
    print("generation module ready: exposes get_llm, generate_answer, build_prompt, and format_context for use by pipeline.py.")

