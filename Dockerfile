# 使用官方的 Python 镜像
FROM python:3.9-slim

# 在容器中设置工作目录
WORKDIR /app

# 复制 requirements.txt 文件到容器中
COPY requirements.txt .

# 安装依赖项
RUN pip install --no-cache-dir -r requirements.txt

# 复制其余的应用程序代码到容器中
COPY . .

# 暴露 Flask 应用程序运行的端口
EXPOSE 5000

# 定义运行 Flask 应用程序的命令
CMD ["python", "main.py"]