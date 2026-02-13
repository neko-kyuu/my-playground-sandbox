# ingest.py
from __future__ import annotations

import os
import argparse
import hashlib
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv
import chromadb

from llama_index.core import VectorStoreIndex, SimpleDirectoryReader
from llama_index.core import StorageContext
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.openai_like import OpenAILikeEmbedding


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def chroma_get_one_meta(collection, source: str) -> Optional[Dict[str, Any]]:
    """
    从 Chroma 中取这个 source 的任意一条记录的 metadata（用来判断 hash 是否一致）
    """
    got = collection.get(
        where={"source": source},
        include=["metadatas"],
        limit=1,
    )
    metas = got.get("metadatas") or []
    if not metas:
        return None
    return metas[0] or None


def iter_all_sources(collection, batch_size: int = 2000) -> set[str]:
    """
    扫全库收集所有 source（用于 --prune）
    """
    offset = 0
    sources: set[str] = set()
    while True:
        got = collection.get(
            include=["metadatas"],
            limit=batch_size,
            offset=offset,
        )
        metas = got.get("metadatas") or []
        if not metas:
            break
        for m in metas:
            if not m:
                continue
            s = m.get("source")
            if s:
                sources.add(s)
        offset += len(metas)
        if len(metas) < batch_size:
            break
    return sources


def main():
    load_dotenv(dotenv_path="/Users/nekokyuu/vscode/playground-sandbox/RAG/.env")

    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default=os.getenv("VAULT_PATH", "./markdown-notes"))
    parser.add_argument("--db", default=os.getenv("DB_PATH", "./llama_chroma_db"))
    parser.add_argument("--collection", default=os.getenv("CHROMA_COLLECTION", "quickstart"))
    parser.add_argument("--reset", action="store_true", help="删除并重建 collection（最省心，避免重复）")
    parser.add_argument("--prune", action="store_true", help="删除库里已不存在于 vault 的文件数据")
    args = parser.parse_args()

    DMX_API_KEY = os.getenv("DMX_API_KEY")
    DMX_BASE_URL = os.getenv("DMX_BASE_URL", "https://www.dmxapi.cn/v1/")
    DMX_EMBEDDING_MODEL = os.getenv("DMX_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")

    if not DMX_API_KEY:
        raise ValueError("Missing DMX_API_KEY in environment")

    # 1) Chroma 持久化
    chroma_client = chromadb.PersistentClient(path=args.db)

    if args.reset:
        try:
            chroma_client.delete_collection(args.collection)
        except Exception:
            pass

    chroma_collection = chroma_client.get_or_create_collection(args.collection)

    # 2) Embedding 模型
    embed_model = OpenAILikeEmbedding(
        model_name=DMX_EMBEDDING_MODEL,
        api_base=DMX_BASE_URL,
        api_key=DMX_API_KEY,
        embed_batch_size=int(os.getenv("EMBED_BATCH_SIZE", "10")),
    )

    # 3) 读文件
    reader = SimpleDirectoryReader(
        args.vault,
        recursive=True,
        filename_as_id=True,
        # 可选：只收录 md
        # required_exts=[".md"],
    )
    documents = reader.load_data()

    # 4) 增量：按 source(file_path) + file_hash 判断是否需要重建
    to_index = []
    current_sources: set[str] = set()

    for doc in documents:
        meta = doc.metadata or {}
        file_path = meta.get("file_path") or meta.get("filepath") or meta.get("path")
        if not file_path:
            # 兜底：没有 file_path 就用 doc_id（但不如 file_path 稳）
            file_path = doc.doc_id or meta.get("file_name") or "unknown"

        source = os.path.abspath(file_path)
        current_sources.add(source)

        # 计算 hash（优先读真实文件）
        try:
            file_hash = sha256_file(source)
        except Exception:
            file_hash = hashlib.sha256((doc.text or "").encode("utf-8")).hexdigest()

        # 把 source/hash 写进 metadata，后续可用 where 过滤/删除
        doc.metadata = dict(meta)
        doc.metadata["source"] = source
        doc.metadata["file_hash"] = file_hash

        # 查库里是否已有该 source
        old_meta = chroma_get_one_meta(chroma_collection, source)
        old_hash = (old_meta or {}).get("file_hash")

        if old_meta is None:
            # 新文件
            to_index.append(doc)
        elif old_hash != file_hash:
            # 文件更新：删掉该 source 的所有旧 chunks，再重建
            chroma_collection.delete(where={"source": source})
            to_index.append(doc)
        else:
            # 未变化：跳过
            pass

    # 5) prune：删掉 vault 中不存在的 source
    if args.prune:
        existing_sources = iter_all_sources(chroma_collection)
        removed = sorted(existing_sources - current_sources)
        for s in removed:
            chroma_collection.delete(where={"source": s})
        if removed:
            print(f"Pruned {len(removed)} removed files from collection.")

    # 6) 写入（只对 to_index 做 embedding + upsert）
    if to_index:
        vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
        storage_context = StorageContext.from_defaults(vector_store=vector_store)

        _ = VectorStoreIndex.from_documents(
            to_index,
            storage_context=storage_context,
            embed_model=embed_model,
            show_progress=True,
        )
        print(f"Indexed {len(to_index)} updated/new documents.")
    else:
        print("No changes detected. Nothing indexed.")

    print(f"Done. collection='{args.collection}', db='{args.db}'")


if __name__ == "__main__":
    main()