#!/bin/bash
# ── Deploy LLM Platform to Azure ─────────────────────────────────────────────
set -e

RG="${1:-llm-platform-rg}"
LOCATION="${2:-eastus}"
APP_NAME="llm-platform"

echo "Deploying LLM Platform to Azure"
echo "  Resource Group : $RG"
echo "  Location       : $LOCATION"
echo ""

# 1. Create resource group
echo "→ Creating resource group..."
az group create --name "$RG" --location "$LOCATION" --output none

# 2. Deploy Bicep (uses placeholder images — we update them after push)
echo "→ Deploying infrastructure (~3 minutes)..."
az deployment group create \
  --resource-group "$RG" \
  --template-file infra/bicep/main.bicep \
  --parameters \
      appName="$APP_NAME" \
      location="$LOCATION" \
      ghcrOrg="sreenugopireddy" \
  --output table

echo ""
echo "✓ Infrastructure deployed!"
echo ""
echo "→ Next: push Docker images with GitHub Actions, then update container apps:"
echo ""
echo "   After images are pushed, run:"
echo "   bash infra/bicep/update-images.sh $RG $APP_NAME"
echo ""
echo "Gateway URL:"
az deployment group show \
  --resource-group "$RG" \
  --name main \
  --query "properties.outputs.gatewayUrl.value" \
  --output tsv 2>/dev/null || echo "  (check Azure portal for gateway URL)"
