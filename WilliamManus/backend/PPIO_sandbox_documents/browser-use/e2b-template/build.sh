#!/bin/bash

# Browser-Use 远程 Chromium 连接方案 - 一键构建脚本
# 功能：编译 Go 反向代理 + 构建 E2B Template

set -e  # 遇到错误立即退出

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

source .env

# 输出函数
log_info() {
    echo -e "${CYAN}ℹ️  $1${NC}"
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
}

log_step() {
    echo -e "${PURPLE}🔧 $1${NC}"
}

# 检查必要的工具
check_dependencies() {
    log_step "检查构建依赖..."
    
    # 检查 Go
    if ! command -v go &> /dev/null; then
        log_error "Go 未安装或不在 PATH 中"
        echo "请安装 Go: https://golang.org/doc/install"
        exit 1
    fi
    log_success "Go 已安装: $(go version)"
    
    # 检查 E2B CLI
    if ! command -v e2b &> /dev/null; then
        log_error "E2B CLI 未安装或不在 PATH 中"
        echo "请安装 E2B CLI: npm install -g @e2b/cli"
        exit 1
    fi
    log_success "E2B CLI 已安装: $(e2b --version)"
    
    # 检查关键文件
    if [[ ! -f "reverse-proxy.go" ]]; then
        log_error "reverse-proxy.go 文件不存在"
        exit 1
    fi
    
    if [[ ! -f "e2b.Dockerfile" ]]; then
        log_error "e2b.Dockerfile 文件不存在"
        exit 1
    fi
    
    if [[ ! -f "start-up.sh" ]]; then
        log_error "start-up.sh 文件不存在"
        exit 1
    fi
    
    log_success "所有必要文件已存在"
}

# 编译 Go 反向代理
compile_proxy() {
    log_step "编译 Go 反向代理为 Linux x86 二进制文件..."
    
    # 设置编译参数
    export GOOS=linux
    export GOARCH=amd64
    export CGO_ENABLED=0
    
    # 编译输出文件名
    OUTPUT_FILE="reverse-proxy"
    
    log_info "编译配置:"
    echo "  源文件: reverse-proxy.go"
    echo "  目标文件: $OUTPUT_FILE"
    echo "  目标系统: $GOOS"
    echo "  目标架构: $GOARCH"
    echo "  CGO: $CGO_ENABLED"
    
    # 开始编译
    if go build -ldflags="-s -w" -o "$OUTPUT_FILE" reverse-proxy.go; then
        log_success "反向代理编译成功!"
        
        # 显示文件信息
        if [[ -f "$OUTPUT_FILE" ]]; then
            FILE_SIZE=$(ls -lh "$OUTPUT_FILE" | awk '{print $5}')
            log_info "生成的二进制文件: $OUTPUT_FILE ($FILE_SIZE)"
            
            # 验证文件类型
            FILE_TYPE=$(file "$OUTPUT_FILE")
            log_info "文件类型: $FILE_TYPE"
        fi
    else
        log_error "反向代理编译失败!"
        exit 1
    fi
}

# 构建 E2B Template
build_e2b_template() {
    log_step "构建 E2B Template..."
    export E2B_DOMAIN=sandbox.ppio.cn

    echo "E2B_API_KEY: $E2B_API_KEY"
    echo "E2B_DOMAIN: $E2B_DOMAIN"
    
    # 检查是否已登录 E2B
    if ! e2b auth info &> /dev/null; then
        log_warning "未登录 E2B，请先登录:"
        echo "  e2b auth login"
        exit 1
    fi
    
    # 执行构建
    log_info "开始构建 E2B Template..."
    echo "构建命令: e2b template build -c \"/app/.browser-use/start-up.sh\""
    echo ""
    
    if e2b template build -c "/app/.browser-use/start-up.sh"; then
        log_success "E2B Template 构建成功!"
    else
        log_error "E2B Template 构建失败!"
        log_warning "常见问题排查:"
        echo "  1. 检查 Dockerfile 语法"
        echo "  2. 检查网络连接"
        echo "  3. 确保 E2B 账户有足够权限"
        echo "  4. 查看构建日志获取详细错误信息"
        exit 1
    fi
}

# 显示构建结果和使用说明
show_usage() {
    log_success "🎉 构建完成!"
    echo ""
    echo -e "${BLUE}📋 使用说明:${NC}"
    echo ""
    echo "1. 获取 Template ID (从上面的构建输出中复制)"
    echo ""
    echo "2. 在你的 Python 代码中使用:"
    echo -e "${CYAN}"
    cat << 'EOF'
查看 demo 示例：https://gitlab.paigod.work/saiki/browser-use-template-demo
EOF
    echo -e "${NC}"
    echo ""
    echo -e "${BLUE}🔧 调试端点:${NC}"
    echo "  健康检查: https://9223-your-sandbox-host/health"
    echo "  性能指标: https://9223-your-sandbox-host/metrics"
    echo ""
    echo -e "${GREEN}🚀 现在可以开始使用 browser-use 进行远程浏览器自动化了!${NC}"
}

# 主函数
main() {
    echo -e "${BLUE}"
    echo "╔════════════════════════════════════════════════════════╗"
    echo "║             Browser-Use 远程 Chromium 连接方案         ║"
    echo "║                      一键构建脚本                      ║"
    echo "╚════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
    echo ""
    
    # 检查当前目录
    if [[ ! -f "reverse-proxy.go" ]] || [[ ! -f "e2b.Dockerfile" ]]; then
        log_error "请在项目根目录下运行此脚本"
        exit 1
    fi
    
    # 执行构建步骤
    check_dependencies
    echo ""
    
    compile_proxy
    echo ""
    
    build_e2b_template
    echo ""
    
    show_usage
}

# 错误处理
trap 'log_error "构建过程中发生错误，请检查上面的错误信息"' ERR

# 运行主函数
main "$@" 