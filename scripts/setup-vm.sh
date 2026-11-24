#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 22.04 x86_64 VM to run swebench-run on a single node.
#
# It installs k3s (a one-node Kubernetes), points kubectl at it, and installs this package in a
# virtualenv. Run it from the repo root as a user with sudo:
#
#     bash scripts/setup-vm.sh
#
# Status: written from the k3s docs and NOT yet run on a real VM. If a step fails, the message
# says which one; every step is safe to run again.
#
# Optional: K3S_VERSION=v1.30.5+k3s1 bash scripts/setup-vm.sh   (default: k3s stable channel)
set -euo pipefail

MIN_CPU=8
MIN_MEM_GB=30
MIN_DISK_GB=250
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

say "Checking the machine"
[ "$(uname -m)" = "x86_64" ] || die "this needs x86_64: the prebuilt SWE-bench images are amd64 (found $(uname -m))"
[ "$(id -u)" -ne 0 ] || die "run as a normal user with sudo, not as root"
sudo -v || die "sudo is needed to install k3s"
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || warn "tested target is Ubuntu 22.04; found ${PRETTY_NAME:-unknown}"

cpus=$(nproc)
mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
disk_gb=$(df -BG --output=avail /var | tail -1 | tr -dc '0-9')
echo "vCPUs: $cpus   memory: ${mem_gb} GB   free disk on /var: ${disk_gb} GB"
[ "$cpus" -ge "$MIN_CPU" ] || warn "fewer than $MIN_CPU vCPUs: use workers: 4 or fewer in your config"
[ "$mem_gb" -ge "$MIN_MEM_GB" ] || warn "less than $MIN_MEM_GB GB memory: use workers: 4 or fewer"
[ "$disk_gb" -ge "$MIN_DISK_GB" ] || warn "less than $MIN_DISK_GB GB free: image pulls may fill the disk (500 GB recommended)"

say "Installing system packages"
sudo apt-get update -y
sudo apt-get install -y curl git python3 python3-venv python3-pip

say "Installing k3s (single-node Kubernetes)"
if command -v k3s >/dev/null 2>&1; then
  echo "k3s is already installed: $(k3s --version | head -1)"
else
  curl -sfL https://get.k3s.io | \
    INSTALL_K3S_EXEC="--disable traefik --disable servicelb" \
    ${K3S_VERSION:+INSTALL_K3S_VERSION="$K3S_VERSION"} sh -
fi

say "Giving your user kubectl access"
mkdir -p "$HOME/.kube"
sudo cp /etc/rancher/k3s/k3s.yaml "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"
chmod 600 "$HOME/.kube/config"
KUBECTL="sudo k3s kubectl"
command -v kubectl >/dev/null 2>&1 && KUBECTL=kubectl

say "Waiting for the node to be ready (up to 3 minutes)"
for _ in $(seq 1 36); do
  if $KUBECTL get nodes 2>/dev/null | grep -q " Ready"; then break; fi
  sleep 5
done
$KUBECTL get nodes
$KUBECTL get nodes | grep -q " Ready" || die "the node did not become Ready; check: sudo systemctl status k3s"

say "Installing swebench-run into $REPO_ROOT/.venv"
cd "$REPO_ROOT"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
swebench-run --help >/dev/null && echo "swebench-run is installed"

cat <<EOF

Done. Next:

  source .venv/bin/activate
  export SOKET_API_KEY=...                       # your model API key (any OpenAI-compatible key works)
  swebench-run create-secret -c configs/single-vm.yaml
  swebench-run meter-up      -c configs/single-vm.yaml
  swebench-run validate      -c configs/single-vm.yaml -n 3     # gold patches must pass
  swebench-run run           -c configs/smoke-13.yaml           # 13 tasks, about an hour

See docs/GETTING-STARTED.md.
EOF
