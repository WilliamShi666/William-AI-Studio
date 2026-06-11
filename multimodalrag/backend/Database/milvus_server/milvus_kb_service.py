import os
import json
import uuid
from functools import wraps
from typing import Dict, List, Optional, Tuple, Any, Union

from langchain.schema import Document

from chatchat.settings import Settings
from chatchat.server.db.repository import list_file_num_docs_id_by_kb_name_and_file_name
from chatchat.server.utils import get_Embeddings
from chatchat.server.file_rag.utils import get_Retriever
from chatchat.server.knowledge_base.kb_service.base import (
    KBService,
    SupportedVSType,
    score_threshold_process,
)
from chatchat.utils import build_logger
logger = build_logger()
from chatchat.server.knowledge_base.utils import KnowledgeFile, get_kb_path

import pymilvus
from pymilvus import (
    connections, 
    utility, 
    Collection, 
    CollectionSchema, 
    FieldSchema, 
    DataType, 
    Function, 
    FunctionType,
    MilvusException,
)

# 获取milvus配置

# 使用默认值（与 start_milvus.sh 启动的服务匹配）
MILVUS_HOST = os.getenv('MILVUS_HOST', 'localhost')
MILVUS_PORT = int(os.getenv('MILVUS_PORT', '19530'))
MILVUS_USER = os.getenv('MILVUS_USER', '')
MILVUS_PASSWORD = os.getenv('MILVUS_PASSWORD', '')
MILVUS_SECURE = os.getenv('MILVUS_SECURE', 'false').lower() == 'true'
MILVUS_DB_NAME = os.getenv('MILVUS_DB_NAME', 'default')

class MilvusKBService(KBService):
    """使用 pymilvus 直接实现的 Milvus 知识库服务"""

    def ensure_connection_static(func):
        """确保 Milvus 连接的静态方法装饰器"""
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                if not connections.has_connection("default") or not connections.get_connection_addr("default"):
                    logger.info(f"连接 Milvus: host={MILVUS_HOST}, port={MILVUS_PORT}")
                    connections.connect(
                        alias="default",
                        host=MILVUS_HOST,
                        port=MILVUS_PORT,
                        user=MILVUS_USER if MILVUS_USER else None,
                        password=MILVUS_PASSWORD if MILVUS_PASSWORD else None,
                        secure=MILVUS_SECURE,
                        db_name=MILVUS_DB_NAME
                    )
                    logger.info("Milvus 连接成功。")
            except Exception as e:
                logger.error(f"连接 Milvus 失败: {e}")
                raise RuntimeError(f"连接 Milvus 失败: {e}") from e
            return func(*args, **kwargs)
        return wrapper
    
    @ensure_connection_static
    def get_collections(self):
        """获取所有集合名称"""
        return utility.list_collections()
    
    @staticmethod
    @ensure_connection_static
    def get_collection(collection_name: str):
        """获取指定名称的集合对象"""
        # 检查集合是否存在
        if utility.has_collection(collection_name):
            return Collection(collection_name)
        else:
            return None
    
    @staticmethod
    def search(milvus_name, content, limit=3):
        """搜索 Milvus 集合"""
        search_params = {
            "metric_type": "IP",
            "params": {"nprobe": 10},
        }
        if (c:=MilvusKBService.get_collection(milvus_name)):
            return c.search(
                content, "embeddings", search_params, limit=limit, output_fields=["content"]
            )
        return None

    @ensure_connection_static
    def do_create_kb(self):
        """创建 Milvus 集合与索引"""
        # 检查集合是否已存在
        if utility.has_collection(self.kb_name):
            logger.info(f"Milvus 集合 '{self.kb_name}' 已存在.")
            self.collection = Collection(self.kb_name)
            return

        # 获取嵌入维度
        embed_function = get_Embeddings(self.embed_model)
        dimension = 1024  # 默认维度
        try:
            test_embedding = embed_function.embed_query("test")
            if isinstance(test_embedding, list) and len(test_embedding) > 0:
                dimension = len(test_embedding)
            else:
                logger.warning(f"获取到的向量维度无效或为0，将使用默认值 {dimension}")
        except Exception as e:
            logger.error(f"从向量模型获取维度失败: {e}，将使用默认值 {dimension}")

        logger.info(f"为 Milvus 集合 '{self.kb_name}' 定义 Schema，向量维度: {dimension}...")
        
        # 定义字段
        fields = [
            FieldSchema(
                name="pk",
                dtype=DataType.INT64,
                is_primary=True,
                auto_id=True,
                description="主键 ID"
            ),
            FieldSchema(
                name="parent_code",
                dtype=DataType.VARCHAR,
                max_length=36,
                description="文本块的唯一标识符"
            ),
            FieldSchema(
                name="file_code",
                dtype=DataType.VARCHAR,
                max_length=36,
                description="所属文档的唯一标识符"
            ),
            FieldSchema(
                name="file_name",
                dtype=DataType.VARCHAR,
                max_length=255,
                description="所属文档的文件名"
            ),
            FieldSchema(
                name="text",
                dtype=DataType.VARCHAR,
                max_length=65535,
                enable_analyzer=True,
                # enable_match = True, #查看是否有该参数，支持全文匹配
                analyzer_params={"tokenizer": "jieba"},
                description="文本块原始内容"
            ),
            FieldSchema(
                name="metadata",
                dtype=DataType.JSON,
                description="存储额外元数据的 JSON 字段"
            ),
            FieldSchema(
                name="vector",
                dtype=DataType.FLOAT_VECTOR,
                dim=dimension,
                description="稠密向量嵌入"
            ),
            FieldSchema(
                name="sparse_vector",
                dtype=DataType.SPARSE_FLOAT_VECTOR,
                description="稀疏向量 (BM25 function 输出)"
            )
        ]
        
        # 定义 Schema
        schema = CollectionSchema(
            fields=fields,
            description=f"知识库集合: {self.kb_name}",
            enable_dynamic_field=False
        )
        
        # 添加 BM25 Function
        try:
            bm25_function = Function(
                name=f"{self.kb_name}_bm25_func",
                function_type=FunctionType.BM25,
                input_field_names=["text"],
                output_field_names=["sparse_vector"]
            )
            schema.add_function(bm25_function)
            logger.info(f"BM25 Function 添加成功")
        except Exception as e:
            logger.error(f"添加 BM25 Function 失败: {e}")
            raise  RuntimeError(f"添加 BM25 Function 失败: {e}")
        
        # 创建集合
        try:
            self.collection = Collection(
                name=self.kb_name,
                schema=schema,
                using='default',
                consistency_level="Bounded"
            )
            logger.info(f"Milvus 集合 '{self.kb_name}' 创建成功.")
            
            # 创建索引
            # 稠密向量索引
            index_params = Settings.kb_settings.kbs_config.get("milvus_kwargs", {}).get("index_params")
            if not index_params:
                index_params = {
                    "metric_type": "IP", 
                    "index_type": "HNSW",
                    "params": {"M": 16, "efConstruction": 64}
                }
            
            self.collection.create_index(
                field_name="vector",
                index_params=index_params,
                index_name="idx_vector_hnsw"
            )
            logger.info(f"向量索引创建成功")
            
            # 稀疏向量索引
            sparse_index_params = {
                "metric_type": "BM25",
                "index_type": "SPARSE_INVERTED_INDEX",
                "params": {
                    "drop_ratio_build": 0.001,  # 建索引时的丢弃比例
                    "bm25_k1": 1.2,            
                    "bm25_b": 0.75             
                }
            }
            
            self.collection.create_index(
                field_name="sparse_vector",
                index_params=sparse_index_params,
                index_name="idx_sparse_bm25"
            )
            logger.info(f"稀疏向量索引创建成功")
            
            # 创建其他索引
            self.collection.create_index(field_name="file_code", index_name="idx_file_code")
            self.collection.create_index(field_name="parent_code", index_name="idx_parent_code")
            self.collection.create_index(field_name="file_name", index_name="idx_file_name")

            # 加载集合
            self.collection.load()
            logger.info(f"集合 '{self.kb_name}' 已加载到内存")
            
        except Exception as e:
            logger.error(f"创建 Milvus 集合失败: {e}")
            # if utility.has_collection(self.kb_name):
            #     try:
            #         utility.drop_collection(self.kb_name)
            #     except Exception as drop_e:
            #         logger.error(f"删除失败的集合时出错: {drop_e}")
            raise RuntimeError(f"创建 Milvus 集合失败") from e

    def vs_type(self) -> str:
        """返回向量存储类型"""
        return SupportedVSType.MILVUS
    
    @ensure_connection_static
    def do_init(self):
        """初始化 Milvus 客户端连接"""
        try:
            # 检查集合是否存在
            if utility.has_collection(self.kb_name):
                self.collection = Collection(self.kb_name)
                logger.info(f"成功获取集合 '{self.kb_name}'")
            else:
                logger.info(f"集合 '{self.kb_name}' 不存在，将在需要时创建")
                self.collection = None
                
        except Exception as e:
            logger.error(f"初始化 Milvus 集合失败: {e}")
            self.collection = None
    
    @ensure_connection_static    
    def do_drop_kb(self):
        """删除 Milvus 集合"""        
        self.kb_name = self.kb_name
        logger.info(f"准备删除 Milvus 知识库 (集合名称: '{self.kb_name}')...")

        try:
            # 检查集合是否存在
            collection_exists = utility.has_collection(self.kb_name, using='default')

            if collection_exists:
                logger.info(f"发现 Milvus 集合 '{self.kb_name}'，开始删除流程...")

                # 尝试释放（可选，失败不阻塞）
                try:
                    logger.info(f"尝试释放集合 '{self.kb_name}' (如果已加载)...")
                    self.collection.release()
                    logger.info(f"集合 '{self.kb_name}' 释放操作已发送。")
                except Exception as release_err:
                    logger.warning(f"释放集合 '{self.kb_name}' 时发生错误 (通常不阻塞删除): {release_err}")

                # 删除集合
                logger.info(f"开始删除集合 '{self.kb_name}'...")
                utility.drop_collection(self.kb_name, using='default')
                logger.info(f"Milvus 集合 '{self.kb_name}' 已成功删除。")
                self.collection = None
                return

            else:
                # 集合不存在，记录日志并正常返回
                logger.info(f"Milvus 集合 '{self.kb_name}' 不存在于 Milvus 中，无需执行删除操作。")
                return

        except MilvusException as e:
            # 捕获检查或删除过程中发生的 Milvus 特定错误
            logger.error(f"处理 Milvus 集合 '{self.kb_name}' 时发生 Milvus 特定错误: {e}")
            raise e
        except Exception as e:
            # 捕获其他未预料的错误
            logger.error(f"处理 Milvus 集合 '{self.kb_name}' 期间发生未预料的错误: {e}", exc_info=True)
            raise e

    @ensure_connection_static    
    def do_clear_vs(self):
        """清空向量存储但保留集合结构"""
        
        if not utility.has_collection(self.kb_name):
            logger.info(f"集合 '{self.kb_name}' 不存在，无需清空")
            return
        
        try:
            collection = Collection(self.kb_name)
            # 删除所有数据
            collection.delete(expr="pk > 0")
            logger.info(f"已清空 Milvus 集合 '{self.kb_name}' 中的所有数据")
        except Exception as e:
            logger.error(f"清空 Milvus 集合数据失败: {e}")
            # 如果清空失败，尝试重建集合
            try:
                self.do_drop_kb()
                self.do_create_kb()
            except Exception as rebuild_e:
                logger.error(f"重建集合失败: {rebuild_e}")
                raise e

    @ensure_connection_static    
    def do_add_doc(self, docs: List[Document], **kwargs) -> List[Dict]:
        """
        添加【子文档块】到 Milvus 集合，强制要求元数据完整性。
        优化：先准备所有数据和向量，再分批次插入Milvus
        """
        if not utility.has_collection(self.kb_name):
            logger.info(f"集合 '{self.kb_name}' 不存在，正在创建...")
            try:
                self.do_create_kb()
                self.collection = Collection(self.kb_name)
                self.collection.load()
                logger.info(f"集合 '{self.kb_name}' 创建并加载成功。")
            except Exception as create_e:
                logger.error(f"创建 Milvus 集合 '{self.kb_name}' 失败: {create_e}", exc_info=True)
                raise RuntimeError(f"创建 Milvus 集合失败") from create_e
        
        if not docs:
            logger.warning("没有文档需要添加")
            return []
        
        if not self.collection:
            self.collection = Collection(self.kb_name)
            try:
                # 优化：仅在未加载时加载
                if not self.collection.has_index():
                    logger.info(f"加载 Milvus 集合 '{self.kb_name}'...")
                    self.collection.load()
                    logger.info(f"集合 '{self.kb_name}' 加载成功。")
                else:
                    logger.info(f"集合 '{self.kb_name}' 已加载或有索引。")
            except Exception as load_e:
                logger.error(f"加载 Milvus 集合 '{self.kb_name}' 失败: {load_e}", exc_info=True)
                raise RuntimeError(f"加载 Milvus 集合失败") from load_e

        MAX_TEXT_LENGTH = 65_535
        MILVUS_BATCH_SIZE = 64*32  # Milvus插入批次大小
        EMBED_BATCH_SIZE = 32     # 向量嵌入的批处理大小
        doc_infos = []

        try:
            embed_function = get_Embeddings(self.embed_model)
            
            logger.info(f"开始准备 {len(docs)} 个文档的数据和向量...")

            # --- 第一阶段：预处理所有文档和元数据 ---
            all_parent_codes = []
            all_file_codes = []
            all_file_names = []
            all_texts = []
            all_metadata_jsons = []
            
            # 处理所有文档的元数据
            for doc_idx, doc in enumerate(docs):
                # 强制检查关键元数据字段
                if not hasattr(doc, 'metadata') or not isinstance(doc.metadata, dict):
                    error_msg = f"文档索引 {doc_idx} 缺少 metadata 字典或类型不正确。"
                    logger.error(error_msg)
                    raise ValueError(error_msg)

                metadata = doc.metadata
                try:
                    parent_code = metadata['parent_code']
                    file_code = metadata['file_code']
                    file_name = metadata['file_name']

                    # --- 开始: 扁平化metadata处理父文档元数据 ---
                    flattened_metadata = metadata.copy()
                    if "metadata" in flattened_metadata and isinstance(flattened_metadata["metadata"], dict):
                        inner_meta = flattened_metadata.pop("metadata")
                        flattened_metadata.update(inner_meta) # 内层键会覆盖外层同名键 
                    metadata = flattened_metadata

                except KeyError as e:
                    missing_key = str(e).strip("'")
                    error_msg = f"文档索引 {doc_idx} 的 metadata 中缺少必需键: '{missing_key}'。处理中止。"
                    logger.error(error_msg + f" Metadata 内容: {metadata}")
                    raise ValueError(error_msg) from e
                
                # 确保提取的值是字符串
                if not all(isinstance(val, str) for val in [parent_code, file_code, file_name]):
                    error_msg = f"文档索引 {doc_idx} 的 parent_code, file_code 或 file_name 不是字符串类型。处理中止。"
                    logger.error(error_msg + f" Values: parent='{parent_code}', file_code='{file_code}', file_name='{file_name}'")
                    raise ValueError(error_msg)

                # 准备 metadata 字段的 JSON
                try:
                    metadata_json = json.dumps(metadata, ensure_ascii=False)
                except Exception as json_err:
                    error_msg = f"序列化文档索引 {doc_idx} 的 metadata 失败: {json_err}. 处理中止。"
                    logger.error(error_msg + f" Metadata: {metadata}")
                    raise ValueError(error_msg) from json_err


                # 处理文本内容（截断）
                page_content = doc.page_content
                if len(page_content.encode('utf-8')) > MAX_TEXT_LENGTH:
                    original_len = len(page_content.encode('utf-8'))
                    page_content = page_content.encode('utf-8')[:MAX_TEXT_LENGTH].decode('utf-8', 'ignore')
                    logger.warning(f"文档索引 {doc_idx} 内容超长 ({original_len} > {MAX_TEXT_LENGTH} bytes)，已截断。Parent Code: {parent_code}")
                # 收集有效的元数据和文本
                all_parent_codes.append(parent_code)
                all_file_codes.append(file_code)
                all_file_names.append(file_name)
                all_texts.append(page_content)
                all_metadata_jsons.append(metadata_json)
            
            logger.info(f"所有 {len(all_texts)} 个文档元数据处理完成，开始生成向量嵌入...")
        
            # --- 第二阶段：生成所有文档的向量嵌入 ---
            all_vectors = []
            if all_texts: # 检查是否有需要处理的文本
                logger.info(f"开始为全部 {len(all_texts)} 个文本生成向量嵌入...")
                try:
                    # 直接调用最合适的嵌入方法处理整个列表
                    if hasattr(embed_function, 'embed_batch_documents'):
                        logger.info("使用 'embed_batch_documents' 方法进行批量并行处理...")
                        all_vectors = embed_function.embed_batch_documents(all_texts,EMBED_BATCH_SIZE)
                        logger.info(f"已为全部 {len(all_texts)} 个文本生成了 {len(all_vectors)} 个向量。")

                    elif hasattr(embed_function, 'embed_documents'):
                        logger.info("使用 'embed_documents' 方法进行处理...")
                        all_vectors = embed_function.embed_documents(all_texts)
                        logger.info(f"已为全部 {len(all_texts)} 个文本生成了 {len(all_vectors)} 个向量。")

                    else:
                        logger.warning("警告：未找到 'embed_batch_documents' 或 'embed_documents' 方法，将逐个生成向量，可能较慢。")
                        all_vectors = []
                        for idx, text in enumerate(all_texts):
                            if idx % 100 == 0 and idx > 0: # 每处理100个打印一次进度
                                logger.info(f"正在处理第 {idx+1}/{len(all_texts)} 个文本...")
                            try:
                                vector = embed_function.embed_query(text)
                                all_vectors.append(vector)
                            except Exception as embed_err:
                                logger.error(f"处理第 {idx+1} 个文本时嵌入失败: {embed_err}", exc_info=True)
                                raise RuntimeError(f"处理第 {idx+1} 个文本时嵌入失败: {embed_err}")
                        logger.info(f"逐个处理完成，成功生成了 {len(all_vectors)} 个向量 (共 {len(all_texts)} 个文本)。")

                except Exception as e:
                    error_msg = f"生成向量嵌入过程中发生严重错误: {e}"
                    logger.exception(error_msg) # 使用 logger.exception 自动记录堆栈信息
                    raise RuntimeError(error_msg) from e
            else:
                logger.info("没有需要生成向量嵌入的文本。")

            # --- 第三阶段：分批次将数据插入Milvus ---
            total_docs = len(all_parent_codes)
            logger.info(f"开始将 {total_docs} 个文档分批插入 Milvus, 批次大小: {MILVUS_BATCH_SIZE}")
            
            for i in range(0, total_docs, MILVUS_BATCH_SIZE):
                end_idx = min(i + MILVUS_BATCH_SIZE, total_docs)
                
                logger.info(f"处理 Milvus 插入批次 {i // MILVUS_BATCH_SIZE + 1}/{(total_docs-1) // MILVUS_BATCH_SIZE + 1} "
                        f"(文档索引 {i} 到 {end_idx-1})...")
                
                # 准备当前批次的数据
                batch_parent_codes = all_parent_codes[i:end_idx]
                batch_file_codes = all_file_codes[i:end_idx]
                batch_file_names = all_file_names[i:end_idx]
                batch_texts = all_texts[i:end_idx]
                batch_metadata_jsons = all_metadata_jsons[i:end_idx]
                batch_vectors = all_vectors[i:end_idx]
                
                # 准备插入的数据
                data_to_insert = [
                    batch_parent_codes,
                    batch_file_codes,
                    batch_file_names,
                    batch_texts,
                    batch_metadata_jsons,
                    batch_vectors
                ]
                
                try:
                    insert_result = self.collection.insert(data_to_insert)
                    primary_keys = insert_result.primary_keys
                    
                    # 创建返回信息
                    for j, pk in enumerate(primary_keys):
                        try:
                            loaded_metadata = json.loads(batch_metadata_jsons[j])
                        except json.JSONDecodeError:
                            loaded_metadata = {"error": "failed to parse stored metadata", "raw": batch_metadata_jsons[j]}
                            
                        doc_info = {
                            "id": str(pk), 
                            "metadata": loaded_metadata
                        }
                        doc_infos.append(doc_info)
                    
                    logger.info(f"批次 {i // MILVUS_BATCH_SIZE + 1}: 成功添加 {len(primary_keys)} 条文档到集合 '{self.kb_name}'")
                
                except MilvusException as batch_e:
                    logger.error(f"批量插入文档失败 (批次 {i // MILVUS_BATCH_SIZE + 1}): {batch_e.error_code}, {batch_e.message}. 处理中止。")
                    raise batch_e
                except Exception as batch_e:
                    logger.error(f"批量插入文档时发生未知错误 (批次 {i // MILVUS_BATCH_SIZE + 1}): {batch_e}. 处理中止。", exc_info=True)
                    raise batch_e
            
            logger.info(f"文档添加完成，总共添加了 {len(doc_infos)} 条文档到集合 '{self.kb_name}'")
            return doc_infos
            
        except (ValueError, RuntimeError, MilvusException) as e:
            logger.error(f"添加文档到 Milvus 过程中断: {e}", exc_info=False)
            raise e
        except Exception as e:
            logger.error(f"添加文档到 Milvus 时发生未预料的错误: {e}", exc_info=True)
            raise e       
            
    @ensure_connection_static    
    def do_delete_doc(self, kb_file: KnowledgeFile, **kwargs):
        """
        从Milvus集合中删除特定文件的文档，优先使用file_code
        """        
        if not utility.has_collection(self.kb_name):
            logger.info(f"集合 '{self.kb_name}' 不存在，无需删除文档")
            return
        
        # 验证file_code是否存在
        if not hasattr(kb_file, 'file_code') or not kb_file.file_code:
            logger.error(f"缺少必要的file_code，无法删除文档")
            raise ValueError(f"删除文档必须提供file_code")
        
        file_code = kb_file.file_code
        
        try:
            collection = Collection(self.kb_name)
            
            # 使用file_code查询文档
            expr = f"file_code == '{file_code}'"
            
            # 执行删除操作
            delete_result = collection.delete(expr=expr)
            deleted_count = delete_result.delete_count if hasattr(delete_result, 'delete_count') else "未知"
            
            logger.info(f"已从collection: '{self.kb_name}' 中删除文件 '{kb_file.filename}' (file_code: {file_code}) 的文档，删除数量: {deleted_count}")
            return True
            
        except Exception as e:
            logger.error(f"删除文档失败: {e}")
            raise RuntimeError(f"删除Milvus中的文档失败: {e}")
    
    @ensure_connection_static
    def do_search(
        self,
        query: str,
        top_k: int,
        score_threshold: float,
        search_mode: str = "hybrid",  # 新增参数: 'hybrid', 'dense', 'sparse'
        metadata: Union[dict,str] = {},
        ranker=None,  # 新增参数，允许自定义融合器
        **kwargs,
    ) -> List[dict]:
        """
        搜索文档，支持多种检索模式
        
        参数:
            query: 查询字符串
            top_k: 返回的最大结果数
            score_threshold: 得分阈值，低于此值的结果将被过滤
            file_codes: 可选，限制搜索范围到指定文件代码列表
            search_mode: 检索模式 - 'hybrid'(默认，混合检索), 'dense'(稠密向量), 'sparse'(稀疏向量/BM25)
            ranker: 自定义结果融合器，仅在混合检索模式下有效
        
        返回:
            List[dict]: 包含检索结果的字典列表，每个字典包含id、score、text和metadata等字段以及所有请求的输出字段
        """
        if not utility.has_collection(self.kb_name):
            logger.warning(f"集合 '{self.kb_name}' 不存在，无法搜索")
            return []
        
        # 确保 search_mode 是有效值
        if search_mode not in ["hybrid", "dense", "sparse"]:
            logger.warning(f"无效的检索模式 '{search_mode}'，将使用默认的 'hybrid' 模式")
            search_mode = "hybrid"
        
        # 定义默认参数
        file_codes = metadata.get("file_codes", [])
        output_fields = getattr(self, 'output_fields', 
                            ["pk", "text", "file_code", "file_name", "parent_code", "metadata"])
        if "file_code" not in output_fields:
            output_fields.append("file_code")
                                
        
        try:
            collection = Collection(self.kb_name)
            collection.load()
            
            # 获取嵌入函数
            embed_function = get_Embeddings(self.embed_model)
            
            # 准备过滤表达式
            expr = None
            if file_codes and len(file_codes) > 0:
                try:
                    if len(file_codes) == 1:
                        # 单个文件代码
                        expr = f"file_code == '{file_codes[0]}'"
                    else:
                        # 多个文件代码
                        expr = (
                            'file_code in ['
                            + ', '.join(f'"{x}"' for x in file_codes)
                            + ']'  
                        )
                    logger.info(f"文件过滤表达式: {expr}")
                except Exception as e:
                    logger.warning(f"构建file_codes查询表达式失败: {e}")
                    # 搜索所有文档，结果会在后面进行过滤
            
            # 准备搜索参数
            milvus_config = Settings.kb_settings.kbs_config.get("milvus_kwargs", {})
            
            # 稠密向量搜索参数
            dense_param = milvus_config.get("search_params", {
                "metric_type": "IP",
                "params": {"ef": 10}
            })
            
            # 稀疏向量搜索参数 (BM25)
            sparse_param = milvus_config.get("sparse_search_params", {
                "metric_type": "BM25",
                "params": {
                    "drop_ratio_search": 0.1,  # 可以调整优化性能
                    "bm25_k1": 1.2,            
                    "bm25_b": 0.75             
                }
            })
            # 确保 metric_type 是 BM25
            sparse_param["metric_type"] = "BM25"
            
            # 融合器配置
            if ranker is None:
                from pymilvus import RRFRanker
                ranker = RRFRanker(k=60)  # 默认融合器
            
            # 准备搜索请求
            from pymilvus import AnnSearchRequest
            search_requests = []
            
            # 根据搜索模式准备请求
            logger.info(f"执行{search_mode}检索 - 查询: '{query[:50]}...'")
            
            if search_mode in ["dense", "hybrid"]:
                # 生成稠密向量
                query_vector = embed_function.embed_query(query)
                
                # 准备稠密向量搜索请求
                dense_req = AnnSearchRequest(
                    data=[query_vector],
                    anns_field="vector",
                    param=dense_param,
                    limit=top_k,
                    expr=expr
                )
                search_requests.append(dense_req)
                logger.debug("已添加稠密向量搜索请求")
            
            if search_mode in ["sparse", "hybrid"]:
                # 准备稀疏向量搜索请求
                sparse_req = AnnSearchRequest(
                    data=[query],  # 使用原始查询文本
                    anns_field="sparse_vector",
                    param=sparse_param,
                    limit=top_k,
                    expr=expr
                )
                search_requests.append(sparse_req)
                logger.debug("已添加稀疏向量搜索请求")
            
            # 执行搜索
            search_results_list = []
            
            # 如果有搜索请求，执行hybrid_search
            if search_requests:

                if  search_mode == "hybrid":           
                    # 对于单一类型搜索，可以不使用融合器
                    use_rerank = search_mode == "hybrid" and ranker is not None
                    
                    # 执行混合搜索 (即使只有一种请求也可使用hybrid_search)
                    search_result = collection.hybrid_search(
                        reqs=search_requests,
                        rerank=ranker if use_rerank else None,
                        limit=top_k,
                        output_fields=output_fields
                    )
                elif search_mode == "dense":
                    search_result = collection.search(
                        data=[query_vector],
                        anns_field="vector",
                        param=dense_param,
                        limit=top_k,
                        expr=expr,
                        output_fields=output_fields
                    )
                elif search_mode == "sparse":
                    search_result = collection.search(
                        data=[query],
                        anns_field="sparse_vector",
                        param=sparse_param,
                        limit=top_k,
                        expr=expr,
                        output_fields=output_fields
                    )
                
                # 提取结果
                if search_result and search_result[0]:  # hybrid_search 返回 List[SearchResult], 每个对应一个查询组合
                    search_result_hybrid = search_result[0]
                    mode_str = {"hybrid": "混合", "dense": "稠密向量", "sparse": "稀疏向量"}[search_mode]
                    logger.info(f"{mode_str}检索找到 {len(search_result_hybrid)} 个结果。")
                    
                    for i, hit in enumerate(search_result_hybrid):
                        # 创建基础结果字典，包含基本信息
                        result_dict = {
                            "id": hit.id,
                            "score": hit.score,
                            "distance": hit.distance
                        }
                        
                        # logger.debug(f"Hit {i+1}: id={hit.id}, score={hit.score}, distance={hit.distance}")
                        
                        # 将所有请求的字段添加到结果字典
                        if hit.entity:
                            for field_name in output_fields:
                                retrieved_value = hit.entity.get(field_name)
                                result_dict[field_name] = retrieved_value
                            
                            # if "text" in result_dict:
                            #     logger.debug(f"  结果 {i+1}, Hit ID {hit.id}: 提取到的 Text: '{str(result_dict.get('text'))[:100]}...'")
                        else:
                            logger.warning(f"  结果 {i+1}, Hit ID {hit.id}: hit.entity 为 None。")
                            for field_name in output_fields:
                                result_dict[field_name] = None
                        
                        # 处理元数据
                        metadata = {}
                        if isinstance(result_dict.get("metadata"), str):
                            try:
                                metadata = json.loads(result_dict.get("metadata"))
                            except:
                                metadata = {"source": "unknown"}
                        elif isinstance(result_dict.get("metadata"), dict):
                            metadata = result_dict.get("metadata")
                        
                        # 如果需要过滤file_codes且表达式构建失败，在这里过滤
                        if file_codes and expr is None:
                            if result_dict.get("file_code") not in file_codes and metadata.get("file_code") not in file_codes:
                                continue
                        
                        # 添加解析后的元数据到结果字典
                        result_dict["metadata"] = metadata
                        search_results_list.append(result_dict)
                    
                    mode_str = {"hybrid": "混合", "dense": "稠密向量", "sparse": "稀疏向量"}[search_mode]
                    logger.info(f"{mode_str}检索完成，找到 {len(search_results_list)} 个结果。")
                else:
                    mode_str = {"hybrid": "混合", "dense": "稠密向量", "sparse": "稀疏向量"}[search_mode]
                    logger.info(f"{mode_str}检索未找到结果。")
            else:
                logger.error(f"未能为 {search_mode} 模式准备有效的搜索请求。")
                return []

            # 按分数排序（降序）
            search_results_list.sort(key=lambda x: x.get("score", 0), reverse=True)

            #tok过滤
            if top_k > 0 and len(search_results_list) > top_k:
                search_results_list = search_results_list[:top_k]

            # 应用分数阈值过滤
            if search_results_list and score_threshold > 0:
                temp_filtered_results = []
                for result in search_results_list:
                    if result.get("score", 0) >= score_threshold:
                        temp_filtered_results.append(result)
                search_results_list = temp_filtered_results
            
            logger.info(f"应用分数阈值 {score_threshold} 和 top_k {top_k} 后，结果数量: {len(search_results_list)}")
            return search_results_list
            
        except Exception as e:
            logger.error(f"搜索失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return []
    
    @ensure_connection_static
    def get_doc_by_ids(self, file_codes: List[str]) -> List[Dict]:
        """
        通过file_code获取文档
        参数：
            file_codes: 文件代码列表
        返回：
            包含文档信息的字典列表，每个字典可直接用于创建DocumentWithVSId
        """        
        if not utility.has_collection(self.kb_name):
            logger.warning(f"集合 '{self.kb_name}' 不存在，无法获取文档")
            return []
        
        if not file_codes:
            return []
        
        # 定义输出字段，如果未在类中定义
        output_fields = getattr(self, 'output_fields', 
                            ["pk", "text", "file_code", "file_name", "parent_code", "metadata"])
        
        try:
            collection = Collection(self.kb_name)
            results = []
            
            # 构建file_code查询表达式
            if file_codes:
                logger.info(f"正在通过file_code查询，数量: {len(file_codes)}")
                expr = (
                    'file_code in ['
                    + ', '.join(f'"{x}"' for x in file_codes)
                    + ']'  
                )
                results = collection.query(
                    expr=expr,
                    output_fields=output_fields
                )
                logger.info(f"查询到 {len(results)} 条结果")
            
            # 处理结果，转换为所需的格式
            docs = []
            for item in results:
                # 解析元数据
                metadata = {}
                if isinstance(item.get("metadata"), str):
                    try:
                        metadata = json.loads(item.get("metadata"))
                    except:
                        metadata = {"source": "unknown"}
                elif isinstance(item.get("metadata"), dict):
                    metadata = item.get("metadata")
                
                # 创建输出字典
                doc_dict = {
                    "id": str(item.get("pk")),
                    "text": item.get("text", ""),
                    "metadata": metadata,
                    "file_code": item.get("file_code", ""),
                    "file_name": item.get("file_name", ""),
                    "parent_code": item.get("parent_code", "")
                }

                new_dict = {}
                for key in output_fields:
                    if key in doc_dict:
                        new_dict[key] = doc_dict[key]
                docs.append(new_dict)
            
            logger.info(f"返回文档数量: {len(docs)}")
            return docs
            
        except Exception as e:
            logger.error(f"通过file_code获取文档失败: {e}")
            logger.exception(e)
            return []
    @ensure_connection_static
    def del_doc_by_ids(self, ids: List[str]) -> bool:
        """通过ID删除文档"""        
        if not utility.has_collection(self.kb_name):
            logger.warning(f"集合 '{self.kb_name}' 不存在，无法删除文档")
            return False
        
        if not ids:
            return True
        
        try:
            collection = Collection(self.kb_name)
            # 执行删除
            collection.delete(expr=f"pk in {[int(id) for id in ids]}")
            return True
        except Exception as e:
            logger.error(f"通过ID删除文档失败: {e}")
            return False