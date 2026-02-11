from dotenv import load_dotenv
import os

import chromadb
from chromadb import EmbeddingFunction, Documents, Embeddings
from openai import OpenAI

load_dotenv() # 这行代码会自动读取 .env 文件并注入环境变量

DMX_API_KEY = os.getenv("DMX_API_KEY")
DMX_BASE_URL = os.getenv("DMX_BASE_URL", "https://www.dmxapi.cn/v1/")
DMX_EMBEDDING_MODEL = os.getenv("DMX_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
DB_PATH = "/Users/nekokyuu/vscode/playground-sandbox/test-static-vault/dmx_chroma_db"

# 2. 定义嵌入函数 (必须和存入时完全一致)
class DMXEmbeddingFunction(EmbeddingFunction):
    def __init__(self):
        self.client = OpenAI(api_key=DMX_API_KEY, base_url=DMX_BASE_URL)

    def __call__(self, input: Documents) -> Embeddings:
        response = self.client.embeddings.create(
            model=DMX_EMBEDDING_MODEL,
            input=input
        )
        return [item.embedding for item in response.data]

# 3. 连接数据库
# 注意：这里我们只读取，所以用 get_collection
try:
    client = chromadb.PersistentClient(path=DB_PATH)
    dmx_ef = DMXEmbeddingFunction()
    collection = client.get_collection(name="notes_api", embedding_function=dmx_ef)
    print(f"成功连接数据库，当前包含 {collection.count()} 条笔记数据。")
except Exception as e:
    print(f"连接数据库失败: {e}")
    exit()

# 4. 交互式查询循环
while True:
    query_text = input("\n🔍 请输入你想查询的问题 (输入 'q' 退出): ")
    if query_text.lower() == 'q':
        break

    THRESHOLD = 1.25  # 自己按效果调：越小越严格（更相似才保留）

    results = collection.query(
        query_texts=[query_text],
        n_results=3,
        include=["documents", "metadatas", "distances"]
    )
    
    print("\n--- 📚 找到的相关笔记(按 distance 阈值过滤) ---")
    
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]
    
    kept = 0
    for doc_content, meta, dist in zip(docs, metas, dists):
        # dist 越小越相似；只保留 dist < THRESHOLD 的
        if dist is None or dist >= THRESHOLD:
            continue
    
        kept += 1
        source = (meta or {}).get("source", "未知来源")
        print(f"TOP {kept} [来源: {source}] (distance={dist:.4f})")
        print("内容:")
        print(doc_content)  # 完整输出
        print("-" * 30)
    
    if kept == 0:
        print(f"没有找到 distance < {THRESHOLD} 的结果（可适当调大 THRESHOLD 或增大 n_results）。")