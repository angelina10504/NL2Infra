#!/usr/bin/env bash
set -e

echo "============================================================"
echo "          NL2Infra: Environment Setup & Cluster Setup       "
echo "============================================================"

# Determine architecture
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)
        K8S_ARCH="amd64"
        CONFTEST_ARCH="x86_64"
        ;;
    aarch64|arm64)
        K8S_ARCH="arm64"
        CONFTEST_ARCH="arm64"
        ;;
    *)
        echo "Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

# Installation directory
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
export PATH="$LOCAL_BIN:$HOME/.cargo/bin:$PATH"

# Remove any 0-byte corrupt files from previous interruptions
for bin_name in uv uvx kind kubectl conftest; do
    if [ -f "$LOCAL_BIN/$bin_name" ] && [ ! -s "$LOCAL_BIN/$bin_name" ]; then
        echo "Removing truncated $bin_name binary..."
        rm -f "$LOCAL_BIN/$bin_name"
    fi
done

# Persist in ~/.bashrc if not present
if ! grep -q 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"' "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"' >> "$HOME/.bashrc"
fi

# 1. Docker check
echo "[1/6] Verifying Docker..."
if ! command -v docker &> /dev/null; then
    echo "Docker not found. Installing docker.io..."
    sudo apt-get update && sudo apt-get install -y docker.io curl wget git
    sudo usermod -aG docker "$USER" || true
    sudo systemctl enable --now docker || true
else
    echo "Docker is installed: $(docker --version)"
fi

# 2. Install uv
echo "[2/6] Checking/Installing uv..."
if ! command -v uv &> /dev/null || [ ! -s "$LOCAL_BIN/uv" ]; then
    echo "Downloading uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$LOCAL_BIN:$HOME/.cargo/bin:$PATH"
fi
echo "uv version: $(uv --version)"

# 3. Install kind
echo "[3/6] Checking/Installing kind..."
if ! command -v kind &> /dev/null || [ ! -s "$LOCAL_BIN/kind" ]; then
    KIND_VERSION="v0.27.0"
    echo "Downloading kind ${KIND_VERSION} for ${K8S_ARCH}..."
    curl -Lo "$LOCAL_BIN/kind" "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${K8S_ARCH}"
    chmod +x "$LOCAL_BIN/kind"
fi
echo "kind version: $(kind --version)"

# 4. Install kubectl
echo "[4/6] Checking/Installing kubectl..."
if ! command -v kubectl &> /dev/null || [ ! -s "$LOCAL_BIN/kubectl" ]; then
    K8S_VERSION=$(curl -L -s https://dl.k8s.io/release/stable.txt 2>/dev/null | tr -d '[:space:]')
    if [[ -z "$K8S_VERSION" || "$K8S_VERSION" != v* ]]; then
        K8S_VERSION="v1.32.2"
    fi
    echo "Downloading kubectl ${K8S_VERSION} for ${K8S_ARCH}..."
    curl -Lo "$LOCAL_BIN/kubectl" "https://dl.k8s.io/release/${K8S_VERSION}/bin/linux/${K8S_ARCH}/kubectl"
    chmod +x "$LOCAL_BIN/kubectl"
fi
echo "kubectl installed successfully."

# 5. Install conftest
echo "[5/6] Checking/Installing conftest..."
if ! command -v conftest &> /dev/null || [ ! -s "$LOCAL_BIN/conftest" ]; then
    CONFTEST_VERSION="0.56.0"
    echo "Downloading conftest v${CONFTEST_VERSION}..."
    TEMP_DIR=$(mktemp -d)
    curl -Lo "$TEMP_DIR/conftest.tar.gz" "https://github.com/open-policy-agent/conftest/releases/download/v${CONFTEST_VERSION}/conftest_${CONFTEST_VERSION}_Linux_${CONFTEST_ARCH}.tar.gz"
    tar -xzf "$TEMP_DIR/conftest.tar.gz" -C "$TEMP_DIR"
    mv "$TEMP_DIR/conftest" "$LOCAL_BIN/conftest"
    chmod +x "$LOCAL_BIN/conftest"
    rm -rf "$TEMP_DIR"
fi
echo "conftest version: $(conftest --version | head -n 1)"

# 6. Run make setup (uv virtualenv & dependencies including checkov)
echo "[6/6] Running make setup (Python venv and dependencies)..."
make setup

# Create kind cluster if not already existing
if ! kind get clusters 2>/dev/null | grep -q "^nl2infra$"; then
    echo "Creating kind cluster 'nl2infra'..."
    make cluster
else
    echo "Kind cluster 'nl2infra' already exists."
fi

echo "============================================================"
echo "    Setup completed successfully!"
echo "    Virtualenv: .venv"
echo "    Kind Cluster: nl2infra"
echo "============================================================"
