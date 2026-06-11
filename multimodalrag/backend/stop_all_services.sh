#!/bin/bash

# RAG系统 - 停止所有服务脚本

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}🛑 停止多模态RAG系统所有服务${NC}"
echo -e "${BLUE}========================================${NC}\n"

# 函数: 停止服务
stop_service() {
    local service_name=$1
    local pid_file="pids/${service_name}.pid"
    
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        
        # 检查进程是否存在
        if ps -p $pid > /dev/null 2>&1; then
            echo -e "${YELLOW}停止 ${service_name} (PID: $pid)...${NC}"
            kill $pid 2>/dev/null || true
            
            # 等待进程结束
            local count=0
            while ps -p $pid > /dev/null 2>&1 && [ $count -lt 10 ]; do
                sleep 1
                count=$((count + 1))
            done
            
            # 如果还没结束，强制杀死
            if ps -p $pid > /dev/null 2>&1; then
                echo -e "${YELLOW}  强制停止 ${service_name}...${NC}"
                kill -9 $pid 2>/dev/null || true
            fi
            
            echo -e "${GREEN}  ✓ ${service_name} 已停止${NC}"
        else
            echo -e "${YELLOW}  ⚠️  ${service_name} 进程不存在 (PID: $pid)${NC}"
        fi
        
        rm -f "$pid_file"
    else
        echo -e "${YELLOW}  ⚠️  ${service_name} 未找到PID文件${NC}"
    fi
}

# 按相反顺序停止服务（先停止依赖较多的）
stop_service "debate"
stop_service "chat"
stop_service "milvus_api"
stop_service "chunker"
stop_service "pdf_extraction"

# 清理可能遗留的进程
echo -e "\n${YELLOW}清理可能遗留的服务进程...${NC}"

# 查找并停止可能的遗留进程
pkill -f "unified_pdf_extraction_service.py" 2>/dev/null && echo -e "${GREEN}  ✓ 清理 PDF提取服务${NC}" || true
pkill -f "markdown_chunker_api.py" 2>/dev/null && echo -e "${GREEN}  ✓ 清理 文本切分服务${NC}" || true
pkill -f "milvus_api.py" 2>/dev/null && echo -e "${GREEN}  ✓ 清理 向量数据库服务${NC}" || true
pkill -f "kb_chat.py" 2>/dev/null && echo -e "${GREEN}  ✓ 清理 对话检索服务${NC}" || true
pkill -f "multiagent_debaters.api:app" 2>/dev/null && echo -e "${GREEN}  ✓ 清理 多智能体辩论服务${NC}" || true

echo -e "\n${BLUE}========================================${NC}"
echo -e "${GREEN}✅ 所有服务已停止${NC}"
echo -e "${BLUE}========================================${NC}\n"

echo -e "${YELLOW}提示:${NC}"
echo -e "  日志文件保存在 ${GREEN}logs/${NC} 目录"
echo -e "  重新启动服务: ${GREEN}./start_all_services.sh${NC}\n"
