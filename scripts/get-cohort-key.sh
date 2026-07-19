#!/usr/bin/env bash
set -euo pipefail

subscription_id="$(az account show --query id -o tsv)"
resource_id="/subscriptions/${subscription_id}/resourceGroups/tulane-ai-rg/providers/Microsoft.ApiManagement/service/tulane-asklit-gateway/subscriptions/educator-cohort"

az rest \
  --method post \
  --url "https://management.azure.com${resource_id}/listSecrets?api-version=2024-05-01" \
  --query primaryKey \
  -o tsv
