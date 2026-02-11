# query.py
from __future__ import annotations

import os
import argparse
from dotenv import load_dotenv
import chromadb

from llama_index.core import VectorStoreIndex
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.openai_like import OpenAILikeEmbedding
from llama_index.llms.openai_like import OpenAILike


def main():
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("q", help="question")
    parser.add_argument("--db", default=os.getenv("DB_PATH", "./llama_chroma_db"))
    parser.add_argument("--collection", default=os.getenv("CHROMA_COLLECTION", "quickstart"))
    parser.add_argument("--top_k", type=int, default=int(os.getenv("TOP_K", "5")))
    parser.add_argument("--rag", action="store_true", help="用 LLM 合成答案（可选）")
    args = parser.parse_args()

    DMX_API_KEY = os.getenv("DMX_API_KEY")
    DMX_BASE_URL = os.getenv("DMX_BASE_URL", "https://www.dmxapi.cn/v1/")
    DMX_EMBEDDING_MODEL = os.getenv("DMX_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")

    if not DMX_API_KEY:
        raise ValueError("Missing DMX_API_KEY in environment")

    # 1) 连接已存在 ChromaDB
    chroma_client = chromadb.PersistentClient(path=args.db)
    chroma_collection = chroma_client.get_or_create_collection(args.collection)

    # 2) embedding（必须和建库一致/同维度）
    embed_model = OpenAILikeEmbedding(
        model_name=DMX_EMBEDDING_MODEL,
        api_base=DMX_BASE_URL,
        api_key=DMX_API_KEY,
    )

    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    index = VectorStoreIndex.from_vector_store(vector_store=vector_store, embed_model=embed_model)

    if not args.rag:
        retriever = index.as_retriever(similarity_top_k=args.top_k)
        nodes = retriever.retrieve(args.q)
        for i, r in enumerate(nodes, 1):
            n = r.node
            meta = n.metadata or {}
            print(f"\n[{i}] score={getattr(r, 'score', None)} source={meta.get('source')}")
            print(n.get_text()[:1000])
        return

    # RAG：需要设置 DMX_CHAT_MODEL
    DMX_CHAT_MODEL = os.getenv("DMX_CHAT_MODEL")
    if not DMX_CHAT_MODEL:
        raise ValueError("To use --rag, set DMX_CHAT_MODEL in env (e.g. Qwen/Qwen2.5-72B-Instruct)")

    llm = OpenAILike(
        model=DMX_CHAT_MODEL,
        api_base=DMX_BASE_URL,
        api_key=DMX_API_KEY,
        context_window=128000,
        is_chat_model=True
    )

    query_engine = index.as_query_engine(similarity_top_k=args.top_k, llm=llm)
    resp = query_engine.query(args.q)
    print(resp)


if __name__ == "__main__":
    main()