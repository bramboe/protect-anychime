FROM python:3.13-alpine
RUN apk add --no-cache openssl iproute2 busybox-extras ffmpeg iptables nftables && pip install --no-cache-dir aiohttp==3.13.5
LABEL org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY app/ /app/
RUN chmod 755 /app/dhcp_hook.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 48200
CMD ["python", "server.py"]
