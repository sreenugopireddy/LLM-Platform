#!/bin/bash
# ── Deploy LLM Platform to Azure ─────────────────────────────────────────────
# Prerequisites:
#   az login
#   az account set --subscription <your-subscription-id>
#
# Usage:  bash infra/bicep/deploy.sh [resource-group] [location]

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

# 2. Deploy Bicep
echo "→ Deploying infrastructure (this takes ~3 minutes)..."
az deployment group create \
  --resource-group "$RG" \
  --template-file infra/bicep/main.bicep \
  --parameters appName="$APP_NAME" location="$LOCATION" \
  --output table

# 3. Set Azure OpenAI key (update with your actual key)
echo ""
echo "→ Next steps:"
echo "   Set your Azure OpenAI key:"
echo "   az containerapp secret set \\"
echo "     --name ${APP_NAME}-inference \\"
echo "     --resource-group $RG \\"
echo "     --secrets azure-oai-payg-key=<YOUR_AZURE_OAI_KEY>"
echo ""
echo "   Set your JWT secret:"
echo "   az containerapp secret set \\"
echo "     --name ${APP_NAME}-gateway \\"
echo "     --resource-group $RG \\"
echo "     --secrets jwt-secret=<YOUR_JWT_SECRET>"
echo ""
echo "Deployment complete! Gateway URL:"
az deployment group show \
  --resource-group "$RG" \
  --name main \
  --query "properties.outputs.gatewayUrl.value" \
  --output tsv