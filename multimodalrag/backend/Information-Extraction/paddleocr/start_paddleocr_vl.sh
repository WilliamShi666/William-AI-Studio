#!/usr/bin/env bash
set -euo pipefail

# PaddleOCR-VL API 服务启动脚本

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ==================== 配置区域 ====================
# PaddleOCR-VL 模型路径
VL_MODEL_DIR="${PADDLEOCR_VL_MODEL_DIR:-./models/PaddleOCR-VL-0.9B}"

# 布局检测模型路径
LAYOUT_MODEL_DIR="${PADDLEOCR_LAYOUT_MODEL_DIR:-./models/PP-DocLayoutV2}"

# GPU ID (如果有多张显卡，可以指定使用哪张)
GPU_ID="${PADDLEOCR_GPU_ID:-0}"

# 服务端口
PORT="${PADDLEOCR_PORT:-8802}"

# 服务地址
HOST="${PADDLEOCR_HOST:-127.0.0.1}"

# Worker 数量
WORKERS=1

# ==================== 检查区域 ====================

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}🚀 启动 PaddleOCR-VL API 服务${NC}"
echo -e "${BLUE}========================================${NC}\n"

# 检查模型路径
if [ ! -d "$VL_MODEL_DIR" ]; then
    echo -e "${RED}❌ 错误: VL模型路径不存在: $VL_MODEL_DIR${NC}"
    echo -e "${YELLOW}请修改脚本中的 VL_MODEL_DIR 为实际的模型路径${NC}"
    exit 1
fi

if [ ! -d "$LAYOUT_MODEL_DIR" ]; then
    echo -e "${RED}❌ 错误: 布局检测模型路径不存在: $LAYOUT_MODEL_DIR${NC}"
    echo -e "${YELLOW}请修改脚本中的 LAYOUT_MODEL_DIR 为实际的模型路径${NC}"
    exit 1
fi

echo -e "${GREEN}✓ VL模型路径: $VL_MODEL_DIR${NC}"
echo -e "${GREEN}✓ 布局模型路径: $LAYOUT_MODEL_DIR${NC}"

# 检查CUDA
if command -v nvidia-smi &> /dev/null; then
    echo -e "${GREEN}✓ CUDA 可用${NC}"
    nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader | head -1
else
    echo -e "${YELLOW}⚠️  CUDA 不可用，将使用 CPU（速度较慢）${NC}"
fi

# 检查Python
if [ -n "${PYTHON_CMD:-}" ]; then
    echo -e "${GREEN}✓ 使用 PYTHON_CMD: $PYTHON_CMD${NC}"
elif command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
    echo -e "${YELLOW}⚠️  使用系统Python${NC}"
else
    echo -e "${RED}❌ 错误: 未找到Python${NC}"
    exit 1
fi

# 检查必要的Python包
echo -e "\n${YELLOW}检查依赖包...${NC}"
REQUIRED_PACKAGES=("paddleocr" "fastapi" "uvicorn" "pillow" "html2text")
for pkg in "${REQUIRED_PACKAGES[@]}"; do
    if $PYTHON_CMD -c "import $pkg" 2>/dev/null; then
        echo -e "${GREEN}  ✓ $pkg${NC}"
    else
        echo -e "${RED}  ❌ $pkg 未安装${NC}"
        echo -e "${YELLOW}     安装命令: pip install $pkg${NC}"
    fi
done

# 检查端口占用
if lsof -Pi :${PORT} -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo -e "\n${YELLOW}⚠️  端口 ${PORT} 已被占用${NC}"
    OCCUPIED_PID=$(lsof -Pi :${PORT} -sTCP:LISTEN -t)
    echo -e "${YELLOW}是否停止占用进程 (PID: $OCCUPIED_PID)? [y/N]${NC}"
    read -r response
    if [[ "$response" =~ ^[Yy]$ ]]; then
        kill $OCCUPIED_PID 2>/dev/null || true
        sleep 2
        echo -e "${GREEN}✓ 端口已清理${NC}"
    else
        echo -e "${RED}❌ 请手动清理端口或更改配置${NC}"
        exit 1
    fi
fi

# ==================== 启动服务 ====================

echo -e "\n${BLUE}========================================${NC}"
echo -e "${YELLOW}启动 PaddleOCR-VL API 服务...${NC}"
echo -e "${BLUE}========================================${NC}\n"

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# 创建日志目录
mkdir -p logs

# 设置环境变量
export CUDA_VISIBLE_DEVICES=$GPU_ID

# 构建启动命令
CMD="$PYTHON_CMD -m uvicorn api_paddleocr_vl_mineru:app \
    --host $HOST \
    --port $PORT \
    --workers $WORKERS"

echo -e "${BLUE}启动命令:${NC}"
echo -e "${YELLOW}$CMD${NC}\n"

# 启动服务（前台运行，方便查看日志）
echo -e "${GREEN}🚀 服务启动中...${NC}"
echo -e "${YELLOW}提示: 按 Ctrl+C 停止服务${NC}\n"

$CMD

# 如果想后台运行，使用以下命令:
# nohup $CMD > logs/paddleocr_vl.log 2>&1 &
# echo $! > logs/paddleocr_vl.pid
# echo -e "${GREEN}✓ 服务已在后台启动 (PID: $!)${NC}"
# echo -e "${BLUE}查看日志: tail -f logs/paddleocr_vl.log${NC}"
# echo -e "${BLUE}停止服务: kill \$(cat logs/paddleocr_vl.pid)${NC}"
