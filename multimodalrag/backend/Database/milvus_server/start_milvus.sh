#!/bin/bash

echo "🚀 启动Milvus服务..."

# 检查docker和docker compose是否安装
if ! command -v docker &> /dev/null; then
    echo "❌ Docker未安装，请先安装Docker"
    exit 1
fi

if docker compose version &> /dev/null; then
    DOCKER_COMPOSE="docker compose"
elif command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    echo "❌ Docker Compose未安装，请先安装Docker Compose"
    exit 1
fi

# 创建必要的目录
mkdir -p volumes/etcd volumes/minio volumes/milvus

# 停止已有的服务
echo "🛑 停止现有服务..."
$DOCKER_COMPOSE down

# 启动服务
echo "▶️  启动服务..."
$DOCKER_COMPOSE up -d

# 等待服务启动
echo "⏳ 等待服务启动..."
sleep 10

# 检查服务状态
echo ""
echo "📊 服务状态检查："
$DOCKER_COMPOSE ps

# 检查健康状态
echo ""
echo "🏥 健康检查："
for i in {1..30}; do
    if curl -f http://localhost:9091/healthz &> /dev/null; then
        echo "✅ Milvus服务启动成功！"
        echo ""
        echo "📍 服务地址："
        echo "   - Milvus: localhost:19530"
        echo "   - Attu管理界面: http://localhost:8080"
        echo "   - MinIO控制台: http://localhost:9011"
        echo ""
        echo "🔑 MinIO登录信息："
        echo "   用户名: ${MINIO_ROOT_USER:-minioadmin}"
        echo "   密码: ${MINIO_ROOT_PASSWORD:-minioadmin}"
        exit 0
    fi
    echo "等待中... ($i/30)"
    sleep 2
done

echo "❌ 服务启动超时，请检查日志："
echo "   $DOCKER_COMPOSE logs -f standalone"
exit 1
