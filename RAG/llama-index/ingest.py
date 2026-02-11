# ingest.py
from dotenv import load_dotenv
import os
import argparse

import chromadb
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader
from llama_index.core import StorageContext
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.openai_like import OpenAILikeEmbedding


def main():
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default=os.getenv("VAULT_PATH", "./markdown-notes"))
    parser.add_argument("--db", default=os.getenv("DB_PATH", "./llama_chroma_db"))
    parser.add_argument("--collection", default=os.getenv("CHROMA_COLLECTION", "quickstart"))
    parser.add_argument("--reset", action="store_true", help="Delete & recreate collection to avoid duplicates")
    args = parser.parse_args()

    dmx_api_key = os.getenv("DMX_API_KEY")
    dmx_base_url = os.getenv("DMX_BASE_URL", "https://www.dmxapi.cn/v1/")
    dmx_embedding_model = os.getenv("DMX_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")

    if not dmx_api_key:
        raise ValueError("Missing DMX_API_KEY in environment")

    # 1) Chroma 持久化客户端
    chroma_client = chromadb.PersistentClient(path=args.db)

    # 2) 避免重复写入：最稳妥方式是 reset（删库重建）
    if args.reset:
        try:
            chroma_client.delete_collection(args.collection)
        except Exception:
            pass

    chroma_collection = chroma_client.get_or_create_collection(args.collection)

    # 3) Embedding 模型
    embed_model = OpenAILikeEmbedding(
        model_name=dmx_embedding_model,
        api_base=dmx_base_url,
        api_key=dmx_api_key,
        embed_batch_size=int(os.getenv("EMBED_BATCH_SIZE", "10")),
    )

    # 4) 读取文档（建议 filename_as_id=True，至少文档级别ID稳定一些）
    documents = SimpleDirectoryReader(
        args.vault,
        recursive=True,
        filename_as_id=True,
    ).load_data()

    # 5) 写入向量库
    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    _ = VectorStoreIndex.from_documents(
        documents,
        storage_context=storage_context,
        embed_model=embed_model,
        show_progress=True,
    )

    print(f"Done. Indexed {len(documents)} documents into collection='{args.collection}' at db='{args.db}'.")


if __name__ == "__main__":
    main()