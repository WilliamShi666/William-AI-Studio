#!/bin/bash

# RAG系统 - 查看所有服务状态脚本

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}📊 多模态RAG系统服务状态${NC}"
echo -e "${BLUE}========================================${NC}\n"

# 函数: 检查服务状态
check_service_status() {
    local service_name=$1
    local port=$2
    local pid_file="pids/${service_name}.pid"
    
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}服务: ${service_name}${NC}"
    
    # 检查PID文件
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        
        # 检查进程是否运行
        if ps -p $pid > /dev/null 2>&1; then
            echo -e "${GREEN}  状态: ✓ 运行中${NC}"
            echo -e "  PID:  ${pid}"
            
            # 获取进程信息
            local cpu_mem=$(ps -p $pid -o %cpu,%mem --no-headers)
            echo -e "  资源: CPU: $(echo $cpu_mem | awk '{print $1}')%  内存: $(echo $cpu_mem | awk '{print $2}')%"
            
            # 检查端口
            if lsof -Pi :${port} -sTCP:LISTEN -t >/dev/null 2>&1 ; then
                echo -e "${GREEN}  端口: ${port} (监听中)${NC}"
                echo -e "  访问: http://localhost:${port}"
            else
                echo -e "${YELLOW}  端口: ${port} (未监听)${NC}"
            fi
            
            # 检查日志最后更新时间
            local log_file="logs/${service_name}.log"
            if [ -f "$log_file" ]; then
                local log_time=$(stat -c %y "$log_file" 2>/dev/null | cut -d'.' -f1)
                echo -e "  日志: ${log_file}"
                echo -e "  更新: ${log_time}"
            fi
        else
            echo -e "${RED}  状态: ✗ 未运行 (PID文件存在但进程已退出)${NC}"
            echo -e "  PID:  ${pid} (无效)"
        fi
    else
        # 检查端口是否被占用（可能是手动启动的）
        if lsof -Pi :${port} -sTCP:LISTEN -t >/dev/null 2>&1 ; then
            local manual_pid=$(lsof -Pi :${port} -sTCP:LISTEN -t)
            echo -e "${YELLOW}  状态: ⚠️  运行中 (手动启动)${NC}"
            echo -e "  PID:  ${manual_pid}"
            echo -e "  端口: ${port} (监听中)"
            echo -e "  访问: http://localhost:${port}"
        else
            echo -e "${RED}  状态: ✗ 未运行${NC}"
            echo -e "  端口: ${port}"
        fi
    fi
}

# 检查各个服务状态
check_service_status "pdf_extraction" "8006"
check_service_status "chunker" "8001"
check_service_status "milvus_api" "8000"
check_service_status "chat" "8501"
check_service_status "debate" "8602"

echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

# 统计运行中的服务数量
running_count=0
total_count=5

for service in pdf_extraction chunker milvus_api chat debate; do
    pid_file="pids/${service}.pid"
    if [ -f "$pid_file" ]; then
        pid=$(cat "$pid_file")
        if ps -p $pid > /dev/null 2>&1; then
            running_count=$((running_count + 1))
        fi
    fi
done

echo -e "${BLUE}总计: ${running_count}/${total_count} 个服务运行中${NC}\n"

# 提示
echo -e "${YELLOW}常用命令:${NC}"
echo -e "  启动所有服务: ${GREEN}./start_all_services.sh${NC}"
echo -e "  停止所有服务: ${GREEN}./stop_all_services.sh${NC}"
echo -e "  查看实时日志: ${GREEN}tail -f logs/*.log${NC}"
echo -e "  重启所有服务: ${GREEN}./stop_all_services.sh && ./start_all_services.sh${NC}\n"

echo -e "${BLUE}========================================${NC}\n"
