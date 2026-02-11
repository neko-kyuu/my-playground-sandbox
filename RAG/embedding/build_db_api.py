from dotenv import load_dotenv
import os

import chromadb
from chromadb import Documents, EmbeddingFunction, Embeddings
from openai import OpenAI

load_dotenv() # 这行代码会自动读取 .env 文件并注入环境变量

DMX_API_KEY = os.getenv("DMX_API_KEY")
DMX_BASE_URL = os.getenv("DMX_BASE_URL", "https://www.dmxapi.cn/v1/")
DMX_EMBEDDING_MODEL = os.getenv("DMX_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")

if not DMX_API_KEY:
    raise ValueError('缺少环境变量 DMX_API_KEY，请先 export DMX_API_KEY="sk-..."。')


# 2. 定义自定义嵌入函数
# 这是让 ChromaDB 使用外部 API 的关键步骤
class DMXEmbeddingFunction(EmbeddingFunction):
    def __init__(self):
        self.client = OpenAI(
            api_key=DMX_API_KEY,
            base_url=DMX_BASE_URL,
        )

    def __call__(self, input: Documents) -> Embeddings:
        # Chroma 会把一批文本(input)传进来，我们需要返回对应的向量列表
        response = self.client.embeddings.create(
            model=DMX_EMBEDDING_MODEL,
            input=input,
        )
        # 从响应中提取向量数据
        return [item.embedding for item in response.data]


# 3. 初始化 ChromaDB
client = chromadb.PersistentClient(path="/Users/nekokyuu/vscode/playground-sandbox/test-static-vault/dmx_chroma_db")

# 使用我们刚刚定义的 DMX 嵌入函数
dmx_ef = DMXEmbeddingFunction()

# 获取或创建集合
collection = client.get_or_create_collection(name="notes_api", embedding_function=dmx_ef)

# 4. 读取 Obsidian 笔记
VAULT_PATH = "/Users/nekokyuu/vscode/playground-sandbox/test-static-vault/markdown-notes"
documents = []
ids = []
metadatas = []

print("正在读取笔记并调用 DMXAPI 进行向量化...")

for filename in os.listdir(VAULT_PATH):
    if filename.endswith(".md"):
        file_path = os.path.join(VAULT_PATH, filename)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            if len(content.strip()) == 0:
                continue

            documents.append(content)
            ids.append(filename)
            metadatas.append({"source": filename})

        except Exception as e:
            print(f"跳过文件 {filename}: {e}")

# 5. 写入数据库
if documents:
    # 注意：API 通常有速率限制，如果文件几百上千个，最好分批写入
    # 这里为了演示简单，一次性写入（Chroma 默认也会分批处理）
    collection.upsert(
        documents=documents,
        metadatas=metadatas,
        ids=ids,
    )
    print(f"成功！已通过 API 将 {len(documents)} 篇笔记存入本地数据库。")
else:
    print("未找到 Markdown 文件。")
