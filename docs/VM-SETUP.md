# Running on a single VM (no Kubernetes cluster)

The pipeline starts one pod per task, so it needs Kubernetes. On one machine, install k3s, a
single-node Kubernetes, and everything else works unchanged.

> **Status: untested.** [`scripts/setup-vm.sh`](../scripts/setup-vm.sh) was written from the k3s
> documentation and has not been run on a real VM. Expect a snag or two, and tell us what they were.
> Every step is safe to repeat.

## What to ask for

| | Recommended | Minimum |
|---|---|---|
| **Machine** | 16 vCPU, 64 GB RAM (GCP `n2-standard-16`) | 8 vCPU, 32 GB RAM |
| **Disk** | 500 GB SSD | 250 GB SSD |
| **OS** | Ubuntu 22.04 LTS, **x86_64** | same |
| **Network** | outbound HTTPS to Docker Hub, GitHub and your model API; inbound SSH only | same |
| **Access** | SSH with sudo | same |
| **Workers** | `workers: 8` | `workers: 4` (a full run takes roughly twice as long) |
| **Full run takes** | about 7 to 10 hours | about 14 to 16 hours |

Why these numbers: a task pod requests 1 CPU and can use 2, with up to 6 GiB of memory, so eight at
once need up to 16 cores and 48 GiB, plus the proxy, k3s and the OS. The disk figure is an estimate
for the per-task images (several GB each), logs and results; the SWE-bench docs suggest at least
120 GB for their own harness. It was not measured on our runs.

Use **x86_64** (not ARM) and a normal Ubuntu image, not a locked-down container OS: the setup
script installs software with sudo.

## Setup

```bash
git clone <this repo> && cd swebench-run
bash scripts/setup-vm.sh
```

It checks the machine against the minimums, installs k3s, gives your user `kubectl` access, waits for
the node to be ready, and installs this package into `.venv`. Then continue with
[GETTING-STARTED.md](GETTING-STARTED.md), using `configs/single-vm.yaml`:

```bash
source .venv/bin/activate
export SOKET_API_KEY=...
swebench-run create-secret -c configs/single-vm.yaml
swebench-run meter-up      -c configs/single-vm.yaml
swebench-run run           -c configs/smoke-13.yaml     # 13 tasks first
```

## Notes for a single node

- **Do not set `k8s.allowed_nodes` or `priority_class`**: you own the machine.
- The images are stored by k3s under `/var/lib/rancher/k3s`. If the disk fills, that is where.
- k3s starts at boot, so a reboot does not lose the cluster. A reboot does kill a running
  `swebench-run`; start it again with the same command and finished tasks are kept.
- The proxy and the DeepSeek package cache are ordinary pods; `meter-down` and `cache-down` remove them.
- To remove everything: `/usr/local/bin/k3s-uninstall.sh`.

## Building Loop from a branch

The pipeline downloads **released** Loop builds (`harness.options.version`, default `v0.7.0`). To
test an unreleased branch such as `staging`, build it and publish the tarball somewhere the pods can
reach (same layout as the release: one top-level directory holding the `loop` binary, and a `.sha256`
file next to it), then point at it. That last step is not implemented yet: the version option only
takes release tags from the `soketlabs/loop` GitHub releases.
