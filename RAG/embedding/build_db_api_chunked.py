from dotenv import load_dotenv
import os
import chromadb
from chromadb import EmbeddingFunction, Documents, Embeddings
from openai import OpenAI

load_dotenv(dotenv_path="/Users/nekokyuu/vscode/playground-sandbox/RAG/.env")

DMX_API_KEY = os.getenv("DMX_API_KEY")
DMX_BASE_URL = os.getenv("DMX_BASE_URL", "https://www.dmxapi.cn/v1/")
DMX_EMBEDDING_MODEL = os.getenv("DMX_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")

VAULT_PATH = "/Users/nekokyuu/vscode/playground-sandbox/test-static-vault/markdown-notes"
DB_PATH = "/Users/nekokyuu/vscode/playground-sandbox/test-static-vault/dmx_chroma_db"

if not DMX_API_KEY:
    raise ValueError('缺少环境变量 DMX_API_KEY，请先 export DMX_API_KEY="sk-..."。')

# --- 1. 定义嵌入函数 (保持不变) ---
class DMXEmbeddingFunction(EmbeddingFunction):
    def __init__(self):
        self.client = OpenAI(api_key=DMX_API_KEY, base_url=DMX_BASE_URL)
    def __call__(self, input: Documents) -> Embeddings:
        # 注意：这里加了 try-except 防止空列表报错
        if not input: return []
        response = self.client.embeddings.create(
            model=DMX_EMBEDDING_MODEL,
            input=input,
        )
        return [item.embedding for item in response.data]

# --- 2. 新增：切分函数 (Chunking) ---
def split_text(text, chunk_size=500, overlap=50):
    """
    简单的滑动窗口切分。
    chunk_size: 每块的字符数
    overlap: 上一块和下一块重叠的字符数（保持上下文连贯）
    """
    if len(text) <= chunk_size:
        return [text]
    
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        # 截取片段
        chunk = text[start:end]
        chunks.append(chunk)
        # 移动窗口，退回 overlap 的距离
        start += (chunk_size - overlap)
    return chunks

# --- 3. 主流程 ---
client = chromadb.PersistentClient(path=DB_PATH)
# 为了避免旧数据干扰，我们先删除旧集合（如果存在），重新创建
try:
    client.delete_collection("notes_api")
    print("已清理旧数据...")
except:
    pass

dmx_ef = DMXEmbeddingFunction()
collection = client.create_collection(name="notes_api", embedding_function=dmx_ef)

documents = []
ids = []
metadatas = []
chunk_counter = 0 # 用于生成唯一的 ID

print(f"正在读取笔记并进行切分 (Chunking)...")

for filename in os.listdir(VAULT_PATH):
    if filename.endswith(".md"):
        file_path = os.path.join(VAULT_PATH, filename)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            if len(content.strip()) == 0: continue

            # === 关键变化：不再直接存 content，而是先切分 ===
            chunks = split_text(content)
            
            for i, chunk in enumerate(chunks):
                documents.append(chunk)
                # ID 必须唯一，我们用 文件名+片段序号
                ids.append(f"{filename}_{i}") 
                # 元数据里记录来源，方便查询时知道出自哪里
                metadatas.append({"source": filename, "chunk_index": i})
                chunk_counter += 1
            
        except Exception as e:
            print(f"跳过文件 {filename}: {e}")

# --- 4. 批量写入 ---
if documents:
    print(f"共生成 {len(documents)} 个片段，正在通过 API 向量化并存入...")
    # Chroma 建议分批写入，这里为了简单每 100 个写入一次
    batch_size = 100
    for i in range(0, len(documents), batch_size):
        end = min(i + batch_size, len(documents))
        collection.upsert(
            documents=documents[i:end],
            metadatas=metadatas[i:end],
            ids=ids[i:end]
        )
        print(f"已处理 {end}/{len(documents)}...")
    
    print("重建完成！现在不仅能搜到笔记，还能精确定位到段落了。")
else:
    print("未找到有效内容。")