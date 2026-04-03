# chunk_management.py
import sys
import os

# 获取当前文件的目录
current_dir = os.path.dirname(os.path.abspath(__file__))
# 获取 RagBackend 目录
backend_dir = os.path.dirname(current_dir)
# 构建 RAG_M/src 的路径
rag_m_src = os.path.join(backend_dir, 'RAG_M', 'src')

print(f"DEBUG: 添加路径: {rag_m_src}")
sys.path.insert(0, rag_m_src)

from vectorstore.vector_store import VectorStoreManager

from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import TextLoader
from fastapi import UploadFile, File, APIRouter, HTTPException, Form
from fastapi.responses import JSONResponse
from typing import List, Optional
from pydantic import BaseModel
import os
import json
import hashlib
from datetime import datetime
import aiofiles
from pathlib import Path
import asyncio
import shutil
from fastapi import Request

router = APIRouter()

# 配置文件上传相关设置
UPLOAD_DIR = "local-KLB-files"
METADATA_DIR = "metadata"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".doc", ".md", ".rtf"}
VECTORSTORE_DIR = "vectorstore"  # ✅ 新增：向量存储目录

# 确保目录存在
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(METADATA_DIR, exist_ok=True)
os.makedirs(VECTORSTORE_DIR, exist_ok=True)  # ✅ 新增

METADATA_FILE = os.path.join(METADATA_DIR, "documents.json")

# 分块上传临时目录
CHUNK_UPLOAD_DIR = os.path.join(UPLOAD_DIR, "chunks")
os.makedirs(CHUNK_UPLOAD_DIR, exist_ok=True)

# Pydantic 模型
class DocumentStatus(BaseModel):
    documentId: int
    enabled: bool

class DeleteDocuments(BaseModel):
    documentIds: List[int]

class UploadCompleteRequest(BaseModel):
    fileHash: str           
    fileName: str           
    totalChunks: int        
    KLB_id: str             

class DocumentResponse(BaseModel):
    id: int
    name: str
    fileType: str
    chunks: int
    uploadDate: str
    slicingMethod: str
    enabled: bool
    file_size: int
    file_hash: str

# 本地文档管理类
from .doc_manage import LocalDocumentManager

# 创建文档管理器实例
doc_manager = LocalDocumentManager()

# 辅助函数
def get_file_hash(content: bytes) -> str:
    """生成文件的MD5哈希值"""
    return hashlib.md5(content).hexdigest()

def get_file_type(filename: str) -> str:
    """获取文件扩展名"""
    return Path(filename).suffix.lower()

def validate_file(filename: str, content: bytes) -> tuple[bool, str]:
    """验证文件类型和大小"""
    file_ext = get_file_type(filename)
    
    if file_ext not in ALLOWED_EXTENSIONS:
        return False, f"不支持的文件类型: {file_ext}"
    
    if len(content) > MAX_FILE_SIZE:
        return False, f"文件大小超过限制 ({MAX_FILE_SIZE / 1024 / 1024:.1f}MB)"
    
    return True, "验证通过"

def calculate_chunks(content: bytes, slicing_method: str = "按段落") -> int:
    """计算文件分块数量"""
    try:
        content_str = content.decode('utf-8', errors='ignore')
    except:
        return 1
    
    if slicing_method == "按段落":
        chunks = len([p for p in content_str.split('\n\n') if p.strip()])
    elif slicing_method == "固定长度":
        chunk_size = 1000
        chunks = len(content_str) // chunk_size + (1 if len(content_str) % chunk_size else 0)
    else:
        chunks = len([s for s in content_str.split('.') if s.strip()])
    
    return max(1, chunks)

# ============ API 端点 ============
@router.post("/api/upload-chunk/")
async def upload_chunk(
    chunk: UploadFile = File(...),
    fileHash: str = Form(...),
    chunkIndex: int = Form(...),
    totalChunks: int = Form(...),
    fileName: str = Form(...),
    KLB_id: str = Form(...)
):
    """上传文件分块"""
    try:
        print(f"DEBUG: 接收参数 - fileHash={fileHash}, chunkIndex={chunkIndex}, totalChunks={totalChunks}")
        
        # 为每个文件创建单独的分块目录
        file_chunk_dir = os.path.join(CHUNK_UPLOAD_DIR, KLB_id, fileHash)
        os.makedirs(file_chunk_dir, exist_ok=True)

        # 分块文件路径
        chunk_file_path = os.path.join(file_chunk_dir, f"chunk_{chunkIndex}.part")
        
        # 保存分块
        async with aiofiles.open(chunk_file_path, 'wb') as f:
            content = await chunk.read()
            await f.write(content)

        print(f"分块上传成功: 文件名={fileName}, 分块索引={chunkIndex}/{totalChunks}")
        return {
            "message": "分块上传成功",
            "fileHash": fileHash,
            "chunkIndex": chunkIndex,
            "totalChunks": totalChunks
        }
    except Exception as e:
        print(f"分块上传失败: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"分块上传失败: {str(e)}")


@router.post("/api/upload-complete/")
async def upload_complete(data: UploadCompleteRequest):  
    """合并文件分块并向量化"""
    try:
        fileHash = data.fileHash      
        fileName = data.fileName      
        totalChunks = data.totalChunks 
        KLB_id = data.KLB_id
        
        print(f"接收到的请求参数: fileHash={fileHash}, fileName={fileName}, totalChunks={totalChunks}, KLB_id={KLB_id}")

        # ===== 第一阶段：合并文件 =====
        file_chunk_dir = os.path.join(CHUNK_UPLOAD_DIR, KLB_id, fileHash)

        if not os.path.exists(file_chunk_dir):
            print(f"分块目录 {file_chunk_dir} 不存在")
            raise HTTPException(status_code=400, detail="分块目录不存在")
        
        uploaded_chunks = os.listdir(file_chunk_dir)
        print(f"上传的分块数量: {len(uploaded_chunks)}, 预期分块数量: {totalChunks}")
        
        if len(uploaded_chunks) != totalChunks:
            raise HTTPException(status_code=400, detail="分块未全部上传")

        # 生成最终文件路径
        file_ext = get_file_type(fileName)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved_filename = f"{fileName}-{timestamp}_{fileHash[:8]}{file_ext}"
        final_file_path = os.path.join(UPLOAD_DIR, KLB_id, saved_filename)

        os.makedirs(os.path.dirname(final_file_path), exist_ok=True)

        # 合并分块
        with open(final_file_path, 'wb') as outfile:
            for i in range(totalChunks):
                chunk_file_path = os.path.join(file_chunk_dir, f"chunk_{i}.part")
                if not os.path.exists(chunk_file_path):
                    print(f"分块文件 {chunk_file_path} 不存在")
                    raise HTTPException(status_code=400, detail=f"分块文件 {chunk_file_path} 不存在")
                with open(chunk_file_path, 'rb') as infile:
                    shutil.copyfileobj(infile, outfile)

        print(f"文件合并成功: {final_file_path}")

        # 读取合并后的文件内容
        async with aiofiles.open(final_file_path, 'rb') as f:
            content = await f.read()

        # 验证文件
        is_valid, message = validate_file(fileName, content)
        if not is_valid:
            os.remove(final_file_path)
            raise HTTPException(status_code=400, detail=message)

        # 检查是否已存在相同文件
        existing_file_hash = get_file_hash(content)
        existing_docs = [doc for doc in doc_manager.get_all_documents() 
                    if doc.get('file_hash') == existing_file_hash]
            
        if existing_docs:
            for doc in existing_docs:
                doc_manager.delete_document(doc['id'], KLB_id)
                print(f"更新文件: {doc['name']}")

        # 计算分块数
        slicing_method = "按段落"
        chunks = calculate_chunks(content, slicing_method)

        # 创建文档记录
        document_data = {
            "id": None,
            "name": fileName,
            "fileType": file_ext.replace('.', ''),
            "chunks": chunks,
            "uploadDate": datetime.now().strftime("%Y-%m-%d"),
            "slicingMethod": slicing_method,
            "enabled": True,
            "file_size": len(content),
            "file_hash": existing_file_hash,
            "file_path": final_file_path,
            "created_at": datetime.now().isoformat()
        }

        doc_id = doc_manager.add_document(document_data)
        document_data['id'] = doc_id

        # 删除分块临时目录
        shutil.rmtree(file_chunk_dir)

        # ===== 第二阶段：向量化文件 =====
        print(f"开始向量化文件: {final_file_path}")

        try:
            # 检查文件是否存在
            if not os.path.exists(final_file_path):
                print(f"❌ 文件不存在: {final_file_path}")
                raise FileNotFoundError(f"文件不存在: {final_file_path}")
            
            file_size = os.path.getsize(final_file_path)
            print(f"✓ 文件存在，大小: {file_size} 字节")
            
            # 尝试加载文档
            print(f"正在加载文档...")
            loader = TextLoader(final_file_path, encoding='utf-8')
            documents = loader.load()
            print(f"✓ 文档加载成功，共 {len(documents)} 个文档")
            
            # 分割文本
            print(f"正在分割文本...")
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=800,
                chunk_overlap=100
            )
            doc_chunks = text_splitter.split_documents(documents)
            print(f"✓ 文本分割成功，共 {len(doc_chunks)} 个文本块")
            
            # 创建向量存储
            # 使用相对路径避免 Windows 特殊字符问题
            vectorstore_path = os.path.join(".", VECTORSTORE_DIR, KLB_id)
            vectorstore_path = os.path.normpath(vectorstore_path)
            print(f"正在创建向量存储，保存到: {vectorstore_path}")
            
            os.makedirs(vectorstore_path, exist_ok=True)
            print(f"✓ 向量存储目录已创建")
            
            manager = VectorStoreManager(docs_dir=KLB_id)
            print(f"✓ VectorStoreManager 已初始化")
            
            manager.create_vectorstore(doc_chunks, vectorstore_path)
            print(f"✓ 向量存储已创建")
            
            # 验证文件是否创建成功
            if os.path.exists(os.path.join(vectorstore_path, 'index.faiss')):
                print(f"✅ 向量化成功: {vectorstore_path}")
            else:
                print(f"❌ 向量库文件未找到，但未抛出异常")
                
        except FileNotFoundError as e:
            print(f"❌ 文件错误: {str(e)}")
        except Exception as e:
            print(f"❌ 向量化失败: {str(e)}")
            import traceback
            traceback.print_exc()

        # 返回成功响应
        return {
            "success": True,
            "message": "文件合并成功",
            "fileId": doc_id,
            "fileName": fileName,
            "filePath": final_file_path,
            "chunks": chunks,
            "slicingMethod": slicing_method
        }
        
    except HTTPException as http_exc:
        print(f"HTTP异常: {http_exc.detail}")
        raise
    except Exception as e:
        print(f"处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")
