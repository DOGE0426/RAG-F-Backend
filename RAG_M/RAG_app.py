import os
import json
import asyncio
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from vectorstore.vector_store import VectorStoreManager
from rag.rag_pipeline import RAGPipeline

router = APIRouter()

class QueryRequest(BaseModel):
    query: str
    docs_dir: str = "."

@router.post("/RAG_query")
async def process_query(query_body: QueryRequest):
    """RAG智能查询接口"""
    
    async def generate():
        try:
            print(f"\n=== RAG 查询开始 ===")
            print(f"问题: {query_body.query}")
            print(f"文档目录: {query_body.docs_dir}")
            
            yield f"data: 开始处理查询: {query_body.query}\n\n"
            
            # 构建向量库路径
            vectorstore_path = os.path.normpath(os.path.join(os.getcwd(), "vectorstore", query_body.docs_dir))
            print(f"向量库路径: {vectorstore_path}")
            print(f"路径存在: {os.path.exists(vectorstore_path)}")
            
            yield f"data: 向量库路径: {vectorstore_path}\n\n"
            
            if not os.path.exists(vectorstore_path):
                if os.path.exists(os.path.join(os.getcwd(), "vectorstore")):
                    print(f"vectorstore 目录内容: {os.listdir(os.path.join(os.getcwd(), 'vectorstore'))}")
                error_msg = f"向量存储路径不存在: {vectorstore_path}"
                yield f"data: ERROR: {error_msg}\n\n"
                raise HTTPException(status_code=404, detail=error_msg)
            
            print(f"目录内容: {os.listdir(vectorstore_path)}")
            
            # 加载向量库
            yield "data: 正在加载向量存储...\n\n"
            vector_store_manager = VectorStoreManager(docs_dir=query_body.docs_dir)
            vectorstore = vector_store_manager.load_vectorstore(vectorstore_path, trust_source=True)
            yield "data: 向量存储加载完成\n\n"
            
            # 初始化 RAG 管道
            yield "data: 初始化RAG管道...\n\n"
            rag = RAGPipeline(os.getenv("MODEL", "llama2"), vectorstore)
            yield "data: RAG管道初始化完成\n\n"
            
            # 检索相关文档
            yield "data: 正在检索相关文档...\n\n"
            retriever = vectorstore.as_retriever(search_type="similarity", search_kwargs={"k": 3})
            docs = retriever.get_relevant_documents(query_body.query)
            print(f"找到 {len(docs)} 个相关文档")
            yield f"data: 找到 {len(docs)} 个相关文档\n\n"
            
            # 处理查询
            yield "data: 正在生成答案...\n\n"
            result = rag.process_query(query_body.query)
            print(f"答案: {result['answer']}")
            
            # 返回答案
            paragraphs = result["answer"].split('\n\n')
            for paragraph in paragraphs:
                if paragraph.strip():
                    yield f"data: {paragraph.strip()}\n\n"
                    await asyncio.sleep(0.05)
            
            # 返回来源
            yield "data: 参考来源:\n\n"
            for i, source in enumerate(result.get("sources", []), 1):
                source_text = source.get('source', source.get('path', str(source)))
                yield f"data: {i}. {source_text}\n\n"
            
            # 返回完整结果
            final_result = {
                "answer": result["answer"],
                "sources": result.get("sources", []),
                "vectorstore_path": vectorstore_path
            }
            yield f"data: COMPLETE: {json.dumps(final_result, ensure_ascii=False)}\n\n"
            print("=== RAG 查询完成 ===\n")
            
        except Exception as e:
            import traceback
            error_msg = f"查询处理失败: {str(e)}"
            print(f"[ERROR] {error_msg}")
            traceback.print_exc()
            yield f"data: ERROR: {error_msg}\n\n"
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )
