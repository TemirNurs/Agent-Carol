FROM node:22-slim

# Install Python and system deps
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Install OpenClaw
RUN npm install -g openclaw@latest

# Set up workspace
WORKDIR /app
COPY . /app

# Install Python dependencies
RUN python3 -m pip install --break-system-packages -r requirements.txt

# Expose gateway port and canvas port
EXPOSE 18789 18793

# Environment variables (set these at runtime)
ENV ANTHROPIC_API_KEY=""

# Start OpenClaw gateway
CMD ["openclaw", "gateway"]
