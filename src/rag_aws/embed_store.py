import os
import psycopg2
from dotenv import load_dotenv
from pgvector.psycopg2 import register_vector
from langchain_community.embeddings import HuggingFaceBgeEmbeddings
from rag_aws.chunking import chunk_document  # Imports your existing chunking module

# Load environment variables from .env file
load_dotenv()

# PostgreSQL Connection parameters from environment variables
DB_PARAMS = {
    "dbname": os.getenv("POSTGRES_DB", "rag_hierarchical"),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD"),
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": os.getenv("POSTGRES_PORT", "5432")
}


def store_hierarchical_chunks(parent_chunks, child_chunks):
    # 1. Load BAAI BGE-Large embedding model locally (1024 dimensions)
    print("Loading BAAI/bge-large-en-v1.5 model...")
    embeddings_model = HuggingFaceBgeEmbeddings(
        model_name="BAAI/bge-large-en-v1.5",
        model_kwargs={"device": "cpu"},  # Change to "cuda" if using an NVIDIA GPU
        encode_kwargs={"normalize_embeddings": True}  # Enforce normalized vectors for cosine similarity
    )

    # 2. Connect to PostgreSQL
    print(f"Connecting to database '{DB_PARAMS['dbname']}'...")
    conn = psycopg2.connect(**DB_PARAMS)
    register_vector(conn)
    cursor = conn.cursor()

    try:
        # 3. Schema Safeguard: Add category and service columns if they don't exist yet
        cursor.execute(
            """
            ALTER TABLE parent_chunks 
            ADD COLUMN IF NOT EXISTS category TEXT,
            ADD COLUMN IF NOT EXISTS service TEXT;
            """
        )

        # 4. Idempotency: Truncate tables on every run to prevent duplicate/corrupted chunks
        # CASCADE handles the foreign key dependency between child_chunks and parent_chunks automatically
        print("Clearing previous data from parent_chunks and child_chunks...")
        cursor.execute("TRUNCATE TABLE child_chunks, parent_chunks RESTART IDENTITY CASCADE;")

        # 5. Insert Parent Chunks
        print(f"Storing {len(parent_chunks)} parent chunks...")
        for parent in parent_chunks:
            # Flexible resolution for Category (H2 '##' or H3 '###')
            category = (
                parent.metadata.get("Category")
                or parent.metadata.get("Header 2")
                or parent.metadata.get("Header 3")
                or parent.metadata.get("Subcategory")
                or parent.metadata.get("category")
            )

            # Flexible resolution for Service (H4 '####')
            service = (
                parent.metadata.get("Service")
                or parent.metadata.get("Header 4")
                or parent.metadata.get("service")
            )

            cursor.execute(
                """
                INSERT INTO parent_chunks (id, text_content, category, service, source, page)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (
                    parent.metadata["id"],
                    parent.page_content,
                    category,
                    service,
                    parent.metadata.get("source", "aws-overview.md"),
                    parent.metadata.get("page")
                )
            )

        # 6. Generate 1024-dimensional embeddings for Child Chunks
        print(f"Generating embeddings for {len(child_chunks)} child chunks...")
        child_texts = [child.page_content for child in child_chunks]
        child_embeddings = embeddings_model.embed_documents(child_texts)

        # 7. Insert Child Chunks
        print("Storing child chunks with vector embeddings...")
        for child, embedding in zip(child_chunks, child_embeddings):
            cursor.execute(
                """
                INSERT INTO child_chunks (id, parent_id, text_content, embedding, source, page)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (
                    child.metadata["id"],
                    child.metadata["parent_id"],
                    child.page_content,
                    embedding,
                    child.metadata.get("source", "aws-overview.md"),
                    child.metadata.get("page")
                )
            )

        # 8. Commit transaction
        conn.commit()
        print("Successfully stored fresh parent and child chunks in PostgreSQL!")

    except Exception as e:
        conn.rollback()
        print(f"Error storing chunks in database: {e}")
        raise e
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    markdown_path = "data/processed/aws-overview.md"
    
    # 1. Generate parent and child chunks
    parent_chunks, child_chunks = chunk_document(markdown_path)
    
    # 2. Embed and store chunks in PostgreSQL
    store_hierarchical_chunks(parent_chunks, child_chunks)