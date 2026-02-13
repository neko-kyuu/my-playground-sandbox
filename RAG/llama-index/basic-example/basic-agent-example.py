from dotenv import load_dotenv
import os
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader

import chromadb
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.core import StorageContext

from llama_index.embeddings.openai_like import OpenAILikeEmbedding

load_dotenv(dotenv_path="/Users/nekokyuu/vscode/playground-sandbox/RAG/.env")

DMX_API_KEY = os.getenv("DMX_API_KEY")
DMX_BASE_URL = os.getenv("DMX_BASE_URL", "https://www.dmxapi.cn/v1/")
DMX_EMBEDDING_MODEL = os.getenv("DMX_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")

VAULT_PATH = "/Users/nekokyuu/vscode/playground-sandbox/test-static-vault/markdown-notes"
DB_PATH = "/Users/nekokyuu/vscode/playground-sandbox/test-static-vault/llama_chroma_db"

# 0. 初始化 ChromaDB
chroma_client = chromadb.PersistentClient(path=DB_PATH)
chroma_collection = chroma_client.get_or_create_collection("quickstart")

# 0. embedding模型

embed_model = OpenAILikeEmbedding(
    model_name=DMX_EMBEDDING_MODEL,
    api_base=DMX_BASE_URL,
    api_key=DMX_API_KEY,
    embed_batch_size=10,
)

# 1. 加载数据 (LlamaIndex 的强项：极简的数据加载)
# 它可以自动处理文件夹下的 PDF, TXT, Markdown 等多种格式
documents = SimpleDirectoryReader(VAULT_PATH).load_data()

# 2. 构建索引
vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
storage_context = StorageContext.from_defaults(vector_store=vector_store)
index = VectorStoreIndex.from_documents(
    documents, storage_context=storage_context, embed_model=embed_model
)

# 3. 创建查询引擎
query_engine = index.as_query_engine()

# 4. 提问
response = query_engine.query("维洛伯爵是谁？")
display(Markdown(f"<b>{response}</b>"))
