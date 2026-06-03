#!/bin/bash
# ── Update Container Apps with real images after GitHub Actions pushes them ───
# Run this after CI/CD has pushed images to ghcr.io
# Usage: bash infra/bicep/update-images.sh llm-platform-rg llm-platform <git-sha>

RG="${1:-llm-platform-rg}"
APP="${2:-llm-platform}"
TAG="${3:-latest}"
ORG="sreenugopireddy"

echo "Updating container images to tag: $TAG"

az containerapp update \
  --name "${APP}-gateway" \
  --resource-group "$RG" \
  --image "ghcr.io/${ORG}/llm-platform-gateway:${TAG}"

az containerapp update \
  --name "${APP}-inference" \
  --resource-group "$RG" \
  --image "ghcr.io/${ORG}/llm-platform-inference:${TAG}"

az containerapp update \
  --name "${APP}-registry" \
  --resource-group "$RG" \
  --image "ghcr.io/${ORG}/llm-platform-registry:${TAG}"

echo "✓ All images updated to $TAG"

# Set real Azure OpenAI key
echo ""
echo "→ Set your Azure OpenAI key:"
echo "   az containerapp secret set \\"
echo "     --name ${APP}-inference \\"
echo "     --resource-group $RG \\"
echo "     --secrets azure-oai-payg-key=YOUR_KEY_HERE"
echo ""
echo "   az containerapp update \\"
echo "     --name ${APP}-inference \\"
echo "     --resource-group $RG \\"
echo "     --set-env-vars AZURE_OAI_PAYG_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com"
