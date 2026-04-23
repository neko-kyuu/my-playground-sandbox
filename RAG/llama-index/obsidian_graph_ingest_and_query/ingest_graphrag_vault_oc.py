# ingest_graphrag_vault_oc.py
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Set

from llama_index.core import SimpleDirectoryReader

from ingest_graphrag import (
    LoadedVaultDocuments,
    ObsidianGraphRAGIngestor,
    normalize_frontmatter_key,
    sanitize_metadata_value,
)


# -------- Vault 文件发现 / 加载 --------
SKIP_DIR_NAMES = {"99-已废弃", "z_assets", "z_template"}
SKIP_FILE_NAMES = {".gitkeep", ".gitignore", "AGENTS.md", "CLAUDE.md"}


def should_skip_dir(dir_name: str) -> bool:
    return dir_name.startswith(".") or dir_name in SKIP_DIR_NAMES


def should_skip_file(file_name: str) -> bool:
    return file_name in SKIP_FILE_NAMES


def should_skip_path(path: str, vault_path: str) -> bool:
    abs_vault_path = os.path.abspath(vault_path)
    abs_path = os.path.abspath(path)

    try:
        if os.path.commonpath([abs_vault_path, abs_path]) != abs_vault_path:
            return False
    except ValueError:
        return False

    rel_path = os.path.relpath(abs_path, abs_vault_path)
    parts = rel_path.split(os.sep)
    if not parts:
        return False

    for part in parts[:-1]:
        if should_skip_dir(part):
            return True

    return should_skip_file(parts[-1])


def discover_vault_markdown_files(vault_path: str) -> List[str]:
    abs_vault_path = os.path.abspath(vault_path)
    markdown_files: List[str] = []

    for root, dirnames, filenames in os.walk(abs_vault_path):
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]

        for filename in filenames:
            if should_skip_file(filename):
                continue
            if not filename.lower().endswith(".md"):
                continue
            markdown_files.append(os.path.abspath(os.path.join(root, filename)))

    return sorted(markdown_files)


def load_vault_oc_docs(input_files: List[str]):
    if not input_files:
        return []
    return SimpleDirectoryReader(input_files=input_files, filename_as_id=True).load_data()


# -------- Vault OC 元数据规则 --------
VAULT_OC_STAGE_RULES = [
    ("01-studio/", "studio"),
    ("02-brainstorm/", "brainstorm"),
    ("03-research/", "research"),
    ("04-AI汇总/", "ai_log"),
]
VAULT_OC_TYPE_RULES = [
    ("01-studio/00-角色/", "character"),
    ("01-studio/01-手稿/", "draft"),
    ("01-studio/02-寰宇/", "place"),
    ("01-studio/03-时间线/", "timeline"),
    ("01-studio/04-势力/", "faction"),
    ("01-studio/05-百科/", "concept"),
    ("01-studio/06-书中书/", "fiction"),
    ("02-brainstorm/", "brainstorm"),
    ("03-research/", "source"),
    ("04-AI汇总/", "ai_task"),
]


def get_vault_relative_path(path: str, vault_path: str) -> Optional[str]:
    abs_vault_path = os.path.abspath(vault_path)
    abs_path = os.path.abspath(path)

    try:
        if os.path.commonpath([abs_vault_path, abs_path]) != abs_vault_path:
            return None
    except ValueError:
        return None

    return os.path.relpath(abs_path, abs_vault_path).replace("\\", "/")


def get_frontmatter_value(frontmatter: Dict[str, Any], key: str) -> Any:
    target_key = normalize_frontmatter_key(key)
    for raw_key, value in (frontmatter or {}).items():
        if normalize_frontmatter_key(raw_key) == target_key:
            return value
    return None


def infer_vault_oc_stage(rel_path: str) -> Optional[str]:
    normalized_path = (rel_path or "").replace("\\", "/")
    for prefix, stage in VAULT_OC_STAGE_RULES:
        if normalized_path.startswith(prefix):
            return stage
    return None


def infer_vault_oc_type(rel_path: str) -> Optional[str]:
    normalized_path = (rel_path or "").replace("\\", "/")
    for prefix, doc_type in VAULT_OC_TYPE_RULES:
        if normalized_path.startswith(prefix):
            return doc_type
    return None


class VaultOCGraphRAGIngestor(ObsidianGraphRAGIngestor):
    pipeline_version = "graphrag-v6-vault-oc-stage-type"

    vault_env = "VAULT_OC_VAULT_PATH"
    db_env = "VAULT_OC_GRAPH_DB_PATH"
    collection_env = "VAULT_OC_CHROMA_COLLECTION"
    graph_env = "VAULT_OC_GRAPH_PATH"
    api_key_env = "VAULT_OC_DMX_API_KEY"
    api_base_env = "VAULT_OC_DMX_BASE_URL"
    embedding_model_env = "VAULT_OC_DMX_EMBEDDING_MODEL"
    embed_batch_size_env = "VAULT_OC_EMBED_BATCH_SIZE"

    default_collection = "quickstart_vault_oc"
    default_graph = "./graphrag/obsidian_graph_vault_oc.json"

    def load_vault_documents(self, vault_path: str) -> LoadedVaultDocuments:
        vault_markdown_files = discover_vault_markdown_files(vault_path)
        return LoadedVaultDocuments(
            documents=load_vault_oc_docs(vault_markdown_files),
            existing_sources_for_prune=set(vault_markdown_files),
        )

    def apply_custom_metadata(
        self,
        meta: Dict[str, Any],
        source: str,
        vault_path: str,
        frontmatter: Dict[str, Any],
    ) -> Dict[str, Any]:
        rel_path = get_vault_relative_path(source, vault_path)
        if not rel_path:
            return meta

        derived_stage = infer_vault_oc_stage(rel_path)
        if derived_stage:
            meta["fm_stage"] = derived_stage

        derived_type = infer_vault_oc_type(rel_path)
        yaml_type = sanitize_metadata_value(get_frontmatter_value(frontmatter, "type"))
        if derived_type:
            meta["fm_type"] = derived_type
        elif yaml_type is not None:
            meta["fm_type"] = yaml_type

        return meta

    def find_removed_sources(
        self,
        known_sources: Set[str],
        current_sources: Set[str],
        loaded_vault: LoadedVaultDocuments,
        vault_path: str,
    ) -> List[str]:
        del current_sources
        prune_targets = {s for s in known_sources if not should_skip_path(s, vault_path)}
        existing_sources = loaded_vault.existing_sources_for_prune or set()
        return sorted(prune_targets - existing_sources)


def main():
    VaultOCGraphRAGIngestor().run()


if __name__ == "__main__":
    main()
