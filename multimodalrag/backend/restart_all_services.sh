#!/bin/bash

# RAG系统 - 重启所有服务脚本

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
echo -e "${BLUE}🔄 重启多模态RAG系统所有服务${NC}"
echo -e "${BLUE}========================================${NC}\n"

# 1. 停止所有服务
echo -e "${YELLOW}第1步: 停止所有服务...${NC}\n"
./stop_all_services.sh

echo -e "\n${YELLOW}等待3秒...${NC}\n"
sleep 3

# 2. 启动所有服务
echo -e "${YELLOW}第2步: 启动所有服务...${NC}\n"
./start_all_services.sh

echo -e "\n${YELLOW}等待5秒让服务完全启动...${NC}\n"
sleep 5

# 3. 测试服务
echo -e "${YELLOW}第3步: 测试服务健康状态...${NC}\n"
./test_services.sh

echo -e "\n${GREEN}✅ 重启完成！${NC}\n"

