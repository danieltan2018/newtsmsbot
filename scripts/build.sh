#!/usr/bin/env bash
# Builds the Lambda deployment artefacts into ./build:
#   build/layer/python/   dependencies from requirements.txt (Lambda layer layout)
#   build/function/       bot code + regenerated cache.py / lookup.py
# Target: python3.14 on arm64 (must match Runtime/Architectures in infra/template.yaml).
# Wheels tagged manylinux2014 or manylinux_2_28 both run on the AL2023-based runtime (glibc 2.34).
set -euo pipefail
cd "$(dirname "$0")/.."

PY_VERSION="${PY_VERSION:-3.14}"

rm -rf build
mkdir -p build/layer/python build/function

echo "==> Regenerating lambda/cache.py and lambda/lookup.py from books/ and media/"
python3 utilities/cacheloader.py

echo "==> Installing dependencies for cp${PY_VERSION/./} aarch64 into build/layer/python"
python3 -m pip install --quiet --upgrade \
  --target build/layer/python \
  --platform manylinux2014_aarch64 \
  --platform manylinux_2_28_aarch64 \
  --python-version "$PY_VERSION" \
  --implementation cp \
  --only-binary=:all: \
  -r requirements.txt
rm -rf build/layer/python/bin
find build/layer -name '__pycache__' -type d -prune -exec rm -rf {} +
find build/layer -name '*.dist-info' -type d -prune -exec rm -rf {} +

echo "==> Copying function code"
cp lambda/*.py build/function/
find build/function -name '__pycache__' -type d -prune -exec rm -rf {} +

echo "==> Sizes"
du -sh build/layer build/function
