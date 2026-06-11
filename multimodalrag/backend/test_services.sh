#!/bin/bash

# RAG系统 - 测试所有服务是否正常运行

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}🧪 测试多模态RAG系统所有服务${NC}"
echo -e "${BLUE}========================================${NC}\n"

# 函数: 测试服务健康检查
test_service() {
    local service_name=$1
    local url=$2
    
    echo -e "${YELLOW}测试 ${service_name}...${NC}"
    
    # 使用curl进行健康检查
    if command -v curl &> /dev/null; then
        response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url" 2>/dev/null)
        
        if [ "$response" == "200" ]; then
            echo -e "${GREEN}  ✓ ${service_name} 正常 (HTTP 200)${NC}"
            echo -e "    访问地址: $url"
            return 0
        else
            echo -e "${RED}  ✗ ${service_name} 异常 (HTTP $response)${NC}"
            echo -e "    访问地址: $url"
            return 1
        fi
    else
        echo -e "${YELLOW}  ⚠️  curl未安装，跳过HTTP测试${NC}"
        return 0
    fi
}

success_count=0
total_count=5

# 测试各个服务
test_service "PDF提取服务" "http://localhost:8006/" && success_count=$((success_count + 1))
echo ""

test_service "文本切分服务" "http://localhost:8001/" && success_count=$((success_count + 1))
echo ""

test_service "向量数据库服务" "http://localhost:8000/health" && success_count=$((success_count + 1))
echo ""

test_service "对话检索服务" "http://localhost:8501/" && success_count=$((success_count + 1))
echo ""

test_service "多智能体辩论服务" "http://localhost:8602/health" && success_count=$((success_count + 1))
echo ""

# 显示测试结果
echo -e "${BLUE}========================================${NC}"
if [ $success_count -eq $total_count ]; then
    echo -e "${GREEN}✅ 所有服务测试通过 (${success_count}/${total_count})${NC}"
else
    echo -e "${YELLOW}⚠️  部分服务测试失败 (${success_count}/${total_count})${NC}"
fi
echo -e "${BLUE}========================================${NC}\n"

# 提示查看API文档
echo -e "${YELLOW}API文档地址:${NC}"
echo -e "  PDF提取:     http://localhost:8006/docs"
echo -e "  文本切分:    http://localhost:8001/docs"
echo -e "  向量数据库:  http://localhost:8000/docs"
echo -e "  对话检索:    http://localhost:8501/docs"
echo -e "  多智能体辩论: http://localhost:8602/docs"
echo ""

exit $((total_count - success_count))
