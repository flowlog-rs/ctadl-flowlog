#!/usr/bin/env bash
# Optional import tools for the pinned Linux executables and Android APKs.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
cd "$CTADL_ROOT"
[[ -x .venv/bin/python ]] || { echo "Run bash benchmark/setup.sh first" >&2; exit 2; }
log="benchmark/runs/setup-frontends-$(date -u +%Y%m%dT%H%M%S)-$$"
mkdir -p "$log" .local/frontends/downloads
exec > >(tee "$log/stdout.log") 2> >(tee "$log/stderr.log" >&2)
fetch() {
    local url=$1 file=$2 checksum=$3
    if [[ ! -f "$file" ]]; then
        curl --fail --location --retry 2 --max-time 900 "$url" -o "$file.part"
        mv "$file.part" "$file"
    fi
    echo "$checksum  $file" | sha256sum -c -
}
if [[ ! -d .local/frontends/ghidra_11.2_PUBLIC ]]; then
    fetch https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_11.2_build/ghidra_11.2_PUBLIC_20240926.zip \
        .local/frontends/downloads/ghidra.zip a98fe01038fe8791c54b121ede545ea799d26358794d7c2ac09fa3f5054f3cdc
    unzip -q .local/frontends/downloads/ghidra.zip -d .local/frontends
fi
if [[ ! -d .local/frontends/jdk-21.0.13+9 ]]; then
    fetch https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.13%2B9/OpenJDK21U-jdk_x64_linux_hotspot_21.0.13_9.tar.gz \
        .local/frontends/downloads/jdk.tar.gz d7f5d5cf865a2902b70ff549b97586a7d3f234e016f041ab447ea740c7d66d1c
    tar -xzf .local/frontends/downloads/jdk.tar.gz -C .local/frontends
fi
.venv/bin/python -m pip install 'ctadl-jadx-fact-generator-plugin==0.6.2'
echo "Ready. Source benchmark/env.sh again before importing real programs."
