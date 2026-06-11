#!/bin/bash

# RAG系统 - 启动所有服务脚本
# 包含: PDF提取、文本切分、向量数据库、对话检索

set -e  # 遇到错误立即退出

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# 加载 .env 配置（兼容等号两侧空格）
ENV_FILE="${SCRIPT_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source <(sed -e 's/^[[:space:]]*//' \
        -e 's/[[:space:]]*#.*$//' \
        -e '/^$/d' \
        -e 's/[[:space:]]*=[[:space:]]*/=/' "$ENV_FILE")
    set +a
    echo -e "${GREEN}✓ 已加载 .env 配置${NC}"
else
    echo -e "${YELLOW}⚠️  未找到 .env 配置，将使用默认环境变量${NC}"
fi

# 创建必要的目录
mkdir -p logs
mkdir -p pids

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}🚀 启动多模态RAG系统所有服务${NC}"
echo -e "${BLUE}========================================${NC}\n"

# 检查 Python。可通过 PYTHON_CMD 指向自己的虚拟环境解释器。
if [ -n "${PYTHON_CMD:-}" ]; then
    echo -e "${GREEN}✓ 使用 PYTHON_CMD: $PYTHON_CMD${NC}"
elif command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
    echo -e "${YELLOW}⚠️  使用系统Python (可能缺少依赖)${NC}"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
    echo -e "${YELLOW}⚠️  使用系统Python (可能缺少依赖)${NC}"
else
    echo -e "${RED}❌ 错误: 未找到Python${NC}"
    exit 1
fi

echo -e "${YELLOW}Python路径: $PYTHON_CMD${NC}\n"

# 函数: 启动服务
start_service() {
    local service_name=$1
    local service_path=$2
    local service_file=$3
    local port=$4
    local pid_file="pids/${service_name}.pid"
    local log_file="logs/${service_name}.log"
    
    echo -e "${YELLOW}启动 ${service_name} (端口 ${port})...${NC}"
    
    # 检查服务文件是否存在
    if [ ! -f "${service_path}/${service_file}" ]; then
        echo -e "${RED}  ❌ 错误: 文件不存在 ${service_path}/${service_file}${NC}"
        return 1
    fi
    
    # 检查端口是否被占用
    if lsof -Pi :${port} -sTCP:LISTEN -t >/dev/null 2>&1 ; then
        echo -e "${YELLOW}  ⚠️  端口 ${port} 已被占用，正在清理...${NC}"
        
        # 获取占用端口的进程PID
        local occupied_pid=$(lsof -Pi :${port} -sTCP:LISTEN -t)
        
        if [ -n "$occupied_pid" ]; then
            echo -e "${YELLOW}  正在停止占用端口的进程 (PID: $occupied_pid)...${NC}"
            
            # 尝试优雅停止
            kill $occupied_pid 2>/dev/null || true
            
            # 等待进程结束
            local count=0
            while ps -p $occupied_pid > /dev/null 2>&1 && [ $count -lt 5 ]; do
                sleep 1
                count=$((count + 1))
            done
            
            # 如果还没结束，强制杀死
            if ps -p $occupied_pid > /dev/null 2>&1; then
                echo -e "${YELLOW}  强制停止进程...${NC}"
                kill -9 $occupied_pid 2>/dev/null || true
                sleep 1
            fi
            
            echo -e "${GREEN}  ✓ 端口 ${port} 已清理${NC}"
        fi
    fi
    
    # 启动服务
    cd "${service_path}"
    nohup $PYTHON_CMD "${service_file}" > "${SCRIPT_DIR}/${log_file}" 2>&1 &
    local pid=$!
    
    # 保存PID
    echo $pid > "${SCRIPT_DIR}/${pid_file}"
    
    # 等待服务启动
    sleep 2
    
    # 检查进程是否还在运行
    if ps -p $pid > /dev/null; then
        echo -e "${GREEN}  ✓ ${service_name} 启动成功 (PID: $pid)${NC}"
        echo -e "${BLUE}    日志: logs/${service_name}.log${NC}"
        echo -e "${BLUE}    访问: http://localhost:${port}${NC}"
    else
        echo -e "${RED}  ❌ ${service_name} 启动失败，请查看日志: logs/${service_name}.log${NC}"
        rm -f "${SCRIPT_DIR}/${pid_file}"
        return 1
    fi
    
    cd "${SCRIPT_DIR}"
    echo ""
}

# 函数: 启动基于 Uvicorn 的FastAPI服务
start_uvicorn_service() {
    local service_name=$1
    local service_path=$2
    local app_import=$3   # 例如: multiagent_debaters.api:app
    local port=$4
    local pid_file="pids/${service_name}.pid"
    local log_file="logs/${service_name}.log"
    
    echo -e "${YELLOW}启动 ${service_name} (端口 ${port})...${NC}"
    
    # 检查端口是否被占用
    if lsof -Pi :${port} -sTCP:LISTEN -t >/dev/null 2>&1 ; then
        echo -e "${YELLOW}  ⚠️  端口 ${port} 已被占用，正在清理...${NC}"
        
        local occupied_pid
        occupied_pid=$(lsof -Pi :${port} -sTCP:LISTEN -t)
        if [ -n "$occupied_pid" ]; then
            echo -e "${YELLOW}  正在停止占用端口的进程 (PID: $occupied_pid)...${NC}"
            kill "$occupied_pid" 2>/dev/null || true
            local count=0
            while ps -p "$occupied_pid" > /dev/null 2>&1 && [ $count -lt 5 ]; do
                sleep 1
                count=$((count + 1))
            done
            if ps -p "$occupied_pid" > /dev/null 2>&1; then
                echo -e "${YELLOW}  强制停止进程...${NC}"
                kill -9 "$occupied_pid" 2>/dev/null || true
                sleep 1
            fi
            echo -e "${GREEN}  ✓ 端口 ${port} 已清理${NC}"
        fi
    fi
    
    cd "${service_path}"
    # 使用 python -m uvicorn 启动 FastAPI 应用
    local bind_host="${SERVICE_BIND_HOST:-127.0.0.1}"
    nohup "$PYTHON_CMD" -m uvicorn "${app_import}" --host "${bind_host}" --port "${port}" > "${SCRIPT_DIR}/${log_file}" 2>&1 &
    local pid=$!
    
    echo $pid > "${SCRIPT_DIR}/${pid_file}"
    sleep 2
    
    if ps -p $pid > /dev/null; then
        echo -e "${GREEN}  ✓ ${service_name} 启动成功 (PID: $pid)${NC}"
        echo -e "${BLUE}    日志: logs/${service_name}.log${NC}"
        echo -e "${BLUE}    访问: http://localhost:${port}${NC}"
    else
        echo -e "${RED}  ❌ ${service_name} 启动失败，请查看日志: logs/${service_name}.log${NC}"
        rm -f "${SCRIPT_DIR}/${pid_file}"
        cd "${SCRIPT_DIR}"
        return 1
    fi
    
    cd "${SCRIPT_DIR}"
    echo ""
}

# 1. 启动PDF提取服务
start_service "pdf_extraction" \
    "Information-Extraction/unified" \
    "unified_pdf_extraction_service.py" \
    "8006"

# 2. 启动文本切分服务
start_service "chunker" \
    "Text_segmentation" \
    "markdown_chunker_api.py" \
    "8001"

# 3. 启动向量数据库服务
start_service "milvus_api" \
    "Database/milvus_server" \
    "milvus_api.py" \
    "8000"

# 4. 启动对话检索服务
start_service "chat" \
    "chat" \
    "kb_chat.py" \
    "8501"

# 4.5 启动多智能体辩论服务（可选）
if [ "$AUTO_START_DEBATE" = "true" ]; then
    start_uvicorn_service "debate" \
        "multiagent_debaters" \
        "multiagent_debaters.api:app" \
        "8602"
else
    echo -e "${YELLOW}⚠️  跳过多智能体辩论服务${NC}"
    echo -e "${BLUE}如需使用，请先在独立环境安装 requirements-debate.txt，或设置 AUTO_START_DEBATE=true${NC}\n"
fi

# 5. 启动 DeepSeek-OCR 服务（可选）
echo -e "${BLUE}----------------------------------------${NC}"
echo -e "${YELLOW}检查 DeepSeek-OCR 服务...${NC}"
DEEPSEEK_SCRIPT="Information-Extraction/deepseekocr/start_deepseek_ocr.sh"
DEEPSEEK_API="Information-Extraction/deepseekocr/api_server_mineru_format.py"

if [ -f "$DEEPSEEK_API" ]; then
    echo -e "${GREEN}✓ DeepSeek-OCR 服务可用${NC}"
    echo -e "${YELLOW}是否启动 DeepSeek-OCR 服务? [y/N]${NC}"
    
    # 如果环境变量 AUTO_START_DEEPSEEK=true，则自动启动
    if [ "$AUTO_START_DEEPSEEK" = "true" ]; then
        response="y"
        echo -e "${GREEN}自动启动模式 (AUTO_START_DEEPSEEK=true)${NC}"
    else
        # 设置5秒超时，默认不启动
        read -t 5 -r response || response="n"
    fi
    
    if [[ "$response" =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}启动 DeepSeek-OCR 服务...${NC}"
        echo -e "${BLUE}提示: DeepSeek-OCR 需要在后台独立运行${NC}"
        echo -e "${BLUE}请在新终端运行: bash $DEEPSEEK_SCRIPT${NC}"
        echo -e "${YELLOW}或设置 AUTO_START_DEEPSEEK=true 环境变量自动启动${NC}\n"
    else
        echo -e "${YELLOW}⚠️  跳过 DeepSeek-OCR 服务${NC}"
        echo -e "${BLUE}如需使用，请手动运行: bash $DEEPSEEK_SCRIPT${NC}\n"
    fi
else
    echo -e "${YELLOW}⚠️  DeepSeek-OCR 服务未安装（可选）${NC}\n"
fi

# 6. 启动 PaddleOCR-VL 服务（可选）
echo -e "${BLUE}----------------------------------------${NC}"
echo -e "${YELLOW}检查 PaddleOCR-VL 服务...${NC}"
PADDLEOCR_SCRIPT="Information-Extraction/paddleocr/start_paddleocr_vl.sh"
PADDLEOCR_API="Information-Extraction/paddleocr/api_paddleocr_vl_mineru.py"

if [ -f "$PADDLEOCR_API" ]; then
    echo -e "${GREEN}✓ PaddleOCR-VL 服务可用${NC}"
    echo -e "${YELLOW}是否启动 PaddleOCR-VL 服务? [y/N]${NC}"
    
    # 如果环境变量 AUTO_START_PADDLEOCR=true，则自动启动
    if [ "$AUTO_START_PADDLEOCR" = "true" ]; then
        response="y"
        echo -e "${GREEN}自动启动模式 (AUTO_START_PADDLEOCR=true)${NC}"
    else
        # 设置5秒超时，默认不启动
        read -t 5 -r response || response="n"
    fi
    
    if [[ "$response" =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}启动 PaddleOCR-VL 服务...${NC}"
        echo -e "${BLUE}提示: PaddleOCR-VL 需要在后台独立运行${NC}"
        echo -e "${BLUE}请在新终端运行: bash $PADDLEOCR_SCRIPT${NC}"
        echo -e "${YELLOW}或设置 AUTO_START_PADDLEOCR=true 环境变量自动启动${NC}\n"
    else
        echo -e "${YELLOW}⚠️  跳过 PaddleOCR-VL 服务${NC}"
        echo -e "${BLUE}如需使用，请手动运行: bash $PADDLEOCR_SCRIPT${NC}\n"
    fi
else
    echo -e "${YELLOW}⚠️  PaddleOCR-VL 服务未安装（可选）${NC}\n"
fi

echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}✅ 所有服务启动完成！${NC}"
echo -e "${BLUE}========================================${NC}\n"

echo -e "${YELLOW}服务列表:${NC}"
echo -e "  📄 PDF提取服务:     http://localhost:8006"
echo -e "  ✂️  文本切分服务:     http://localhost:8001"
echo -e "  🗄️  向量数据库服务:   http://localhost:8000"
echo -e "  💬 对话检索服务:     http://localhost:8501"
echo -e "  🧠 多智能体辩论服务: http://localhost:8602"
echo -e "  🤖 DeepSeek-OCR:    http://localhost:8705 ${YELLOW}(可选，需手动启动)${NC}"
echo -e "  🔍 PaddleOCR-VL:    http://localhost:8802 ${YELLOW}(可选，需手动启动)${NC}"

echo -e "\n${YELLOW}常用命令:${NC}"
echo -e "  查看服务状态: ${GREEN}./status_services.sh${NC}"
echo -e "  查看所有日志: ${GREEN}tail -f logs/*.log${NC}"
echo -e "  查看单个日志: ${GREEN}tail -f logs/chat.log${NC}"
echo -e "  停止所有服务: ${GREEN}./stop_all_services.sh${NC}"

echo -e "\n${BLUE}========================================${NC}\n"
