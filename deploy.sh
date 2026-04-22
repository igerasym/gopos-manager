#!/bin/bash
# Deploy The Frame Manager to EC2
# Usage: ssh into EC2, then run this script

set -e

REPO_DIR="/home/ec2-user/cafe-manager"
REPO_URL="https://github.com/igerasym/gopos-manager.git"

echo "=== The Frame Manager — Deploy ==="

# Install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    sudo yum update -y
    sudo yum install -y docker
    sudo systemctl start docker
    sudo systemctl enable docker
    sudo usermod -aG docker ec2-user
    echo "Docker installed. Please log out and back in, then re-run this script."
    exit 0
fi

# Install docker-compose plugin if not present
if ! docker compose version &> /dev/null; then
    echo "Installing Docker Compose plugin..."
    sudo mkdir -p /usr/local/lib/docker/cli-plugins
    sudo curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose
    sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

# Install git if not present
if ! command -v git &> /dev/null; then
    sudo yum install -y git
fi

# Clone or pull repo
if [ -d "$REPO_DIR" ]; then
    echo "Pulling latest changes..."
    cd "$REPO_DIR"
    git pull
else
    echo "Cloning repo..."
    git clone "$REPO_URL" "$REPO_DIR"
    cd "$REPO_DIR"
fi

# Create .env if not exists
if [ ! -f .env ]; then
    echo "Creating .env file..."
    cat > .env << 'EOF'
GOPOS_EMAIL=your-email@example.com
GOPOS_PASSWORD=your-password-here
GOPOS_VENUE_ID=your-venue-id
GOPOS_URL=https://app.gopos.io
SESSION_SECRET=$(openssl rand -hex 32)
EOF
    echo "⚠️  Edit .env with your GoPos credentials: nano $REPO_DIR/.env"
fi

# Create data dir
mkdir -p data

# Build and start
echo "Building and starting..."
docker compose up -d --build

echo ""
echo "=== Deploy complete ==="
echo "App running on http://localhost:8000"
echo "Default login: admin / admin (change password immediately!)"
echo ""
echo "To add nginx proxy, run:"
echo "  sudo tee /etc/nginx/conf.d/cafe.conf << 'NGINX'"
echo "  server {"
echo "      listen 80;"
echo "      server_name cafe.catchmyactions.com;"
echo "      location / {"
echo "          proxy_pass http://127.0.0.1:8000;"
echo "          proxy_set_header Host \$host;"
echo "          proxy_set_header X-Real-IP \$remote_addr;"
echo "          proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;"
echo "          proxy_set_header X-Forwarded-Proto \$scheme;"
echo "      }"
echo "  }"
echo "  NGINX"
echo "  sudo nginx -t && sudo systemctl reload nginx"
