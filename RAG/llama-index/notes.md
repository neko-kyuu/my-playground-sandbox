
LlamaIndex 官方通过 **LlamaHub** 提供了专门针对 Obsidian 的加载器（`ObsidianReader`），但它主要负责“**读取**”和“**解析**”。

至于“**清洗**”（比如处理双向链接 `[[Link]]`、去除 Frontmatter、处理 Callouts），通常需要你结合 **加载器** + **自定义转换逻辑** 来完成。

以下是针对 Obsidian 文件的处理方案，从基础加载到深度清洗：

### 1. 基础加载：使用 `ObsidianReader`

这是最简单的第一步。它能识别 Obsidian 的 Vault（库）结构，读取 `.md` 文件。

你需要先安装插件：
```bash
pip install llama-index-readers-obsidian
```

基本用法：
```python
from llama_index.readers.obsidian import ObsidianReader

# input_dir 指向你的 Obsidian Vault 根目录
documents = ObsidianReader(input_dir="/path/to/my/obsidian/vault").load_data()

# 它会自动处理一部分基础工作，比如读取文件内容
```

---

### 2. 深度清洗：Obsidian 特有语法的处理

Obsidian 文件是 Markdown，但它有很多“非标准”语法。直接入库会导致 Embedding 效果变差（比如 `[[...]]` 会被当成普通字符）。

建议在 `IngestionPipeline` 中加入一个自定义清洗步骤，专门处理以下 Obsidian 特性：

#### 需要清洗/转换的常见内容：

1.  **双向链接 (`[[Note Name]]` 或 `[[Note Name|Alias]]`)**
    *   **问题**：LLM 读不懂 `[[ ]]`，它只认识自然语言。
    *   **处理**：用正则把 `[[Note Name]]` 替换为 `Note Name`，把 `[[Note Name|Alias]]` 替换为 `Alias`。
2.  **YAML Frontmatter (文件开头的 `---` 区域)**
    *   **问题**：包含 `date`, `tags`, `aliases`。如果作为文本嵌入，会增加噪音。
    *   **处理**：提取为 Metadata，然后从正文（Content）中删除。
3.  **Dataview 查询代码块**
    *   **问题**：Obsidian 用户常写 ````dataview table ...````。
    *   **处理**：直接删除，这些代码对语义理解无用。

---

### 3. 实战代码：构建 Obsidian 专用清洗管道

这是一个生产级别的清洗示例，专门针对 Obsidian 语法优化：

```python
import re
from llama_index.core import Document
from llama_index.core.ingestion import IngestionPipeline
from llama_index.core.node_parser import MarkdownNodeParser
from llama_index.core.schema import TransformComponent

# --- 自定义 Obsidian 清洗器 ---
class ObsidianCleaner(TransformComponent):
    def __call__(self, nodes, **kwargs):
        for node in nodes:
            text = node.text
            
            # 1. 清洗双向链接 [[Link]] -> Link
            # 匹配 [[Text]]
            text = re.sub(r'\[\[([^\|\]]+)\]\]', r'\1', text)
            # 匹配 [[Link|Alias]] -> Alias
            text = re.sub(r'\[\[[^\|\]]+\|([^\]]+)\]\]', r'\1', text)
            
            # 2. 清洗嵌入引用 ![[Image.png]] -> (移除或保留文件名)
            # 这里选择简单移除图片引用，或者你可以改成 "[图片: Image.png]"
            text = re.sub(r'!\[\[(.*?)\]\]', '', text)
            
            # 3. 移除 Dataview 代码块 (可选)
            text = re.sub(r'```dataview[\s\S]*?```', '', text)

            # 4. 简单的 Frontmatter 移除 (如果 Loader 没处理好)
            # 注意：ObsidianReader 通常会把 Frontmatter 放到 metadata 里，
            # 但有时候正文里还留着。这里做一个兜底。
            if text.startswith("---"):
                try:
                    # 找到第二个 --- 的位置并截断
                    parts = text.split("---", 2)
                    if len(parts) > 2:
                        text = parts[2].strip()
                except:
                    pass

            node.text = text
        return nodes

# --- 使用流程 ---

# 1. 加载
from llama_index.readers.obsidian import ObsidianReader
loader = ObsidianReader(input_dir="./my_vault")
docs = loader.load_data()

# 2. 构建管道
pipeline = IngestionPipeline(
    transformations=[
        ObsidianCleaner(),              # 先清洗特有语法
        MarkdownNodeParser(),           # 再按 Markdown 结构切分 (H1, H2...)
        # embed_model                   # 最后嵌入
    ]
)

# 3. 运行并获取处理后的 Nodes
nodes = pipeline.run(documents=docs)

# 4. 检查清洗结果 (验证)
print(nodes[0].text) 
# 现在的文本里应该是干净的自然语言，没有 [[ ]] 了
```

### 4. 进阶技巧：利用 Frontmatter 做元数据过滤

Obsidian 的强大之处在于插件和元数据。`ObsidianReader` 会尝试解析 YAML Frontmatter。

你可以利用这一点，在检索时做**混合检索（Hybrid Search）**：

*   **场景**：你只想搜 `#工作` 标签下的笔记。
*   **实现**：
    `ObsidianReader` 加载后，查看 `doc.metadata`，通常会包含 `tags` 或 `file_path`。
    入库 Chroma 时，这些 metadata 会自动存入。
    
    ```python
    # 检索时过滤
    from llama_index.core.vector_stores import MetadataFilter, MetadataFilters

    filters = MetadataFilters(
        filters=[
            MetadataFilter(key="tags", value="工作", operator="CONTAINS"),
        ]
    )
    
    query_engine = index.as_query_engine(filters=filters)
    ```

### 总结

1.  **加载**：用 `llama_index.readers.obsidian`。
2.  **清洗**：LlamaIndex **没有内置** 去除 `[[...]]` 的功能，必须自己写一个简单的 Regex `TransformComponent`（如上代码）。
3.  **切分**：推荐使用 `MarkdownNodeParser` 而不是普通的 `SentenceSplitter`，因为它能理解 Obsidian 的标题层级结构。