// ── LLM Platform — Azure Infrastructure (Bicep)
param location string = 'eastus'
param appName string = 'llm-platform'
param containerTag string = 'latest'
param ghcrOrg string = 'sreenugopireddy'

// ── Log Analytics ─────────────────────────────────────────────────────────────
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${appName}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// ── Container Apps Environment ────────────────────────────────────────────────
resource containerEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: '${appName}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ── Cosmos DB (Serverless) ────────────────────────────────────────────────────
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2023-04-15' = {
  name: '${appName}-cosmos'
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [{ locationName: location, failoverPriority: 0, isZoneRedundant: false }]
    capabilities: [{ name: 'EnableServerless' }]
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    enableFreeTier: true
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2023-04-15' = {
  parent: cosmos
  name: 'llm-platform'
  properties: { resource: { id: 'llm-platform' } }
}

resource promptsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2023-04-15' = {
  parent: cosmosDb
  name: 'prompts'
  properties: {
    resource: {
      id: 'prompts'
      partitionKey: { paths: ['/name'], kind: 'Hash' }
    }
  }
}

resource evalContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2023-04-15' = {
  parent: cosmosDb
  name: 'eval-results'
  properties: {
    resource: {
      id: 'eval-results'
      partitionKey: { paths: ['/prompt'], kind: 'Hash' }
      defaultTtl: 2592000
    }
  }
}

// ── Service Bus ───────────────────────────────────────────────────────────────
resource serviceBus 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: '${appName}-sb'
  location: location
  sku: { name: 'Basic', tier: 'Basic' }
}

// ── Shared secrets ────────────────────────────────────────────────────────────
var cosmosConnStr = cosmos.listConnectionStrings().connectionStrings[0].connectionString

// ── Inference Container App (internal) ───────────────────────────────────────
resource inferenceApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: '${appName}-inference'
  location: location
  properties: {
    environmentId: containerEnv.id
    configuration: {
      ingress: {
        external: false
        targetPort: 8001
        transport: 'http'
      }
      secrets: [
        { name: 'cosmos-conn-str', value: cosmosConnStr }
        { name: 'azure-oai-payg-key', value: '' }
      ]
    }
    template: {
      containers: [{
        name: 'inference'
        image: 'ghcr.io/${ghcrOrg}/llm-platform-inference:${containerTag}'
        resources: {
          cpu: json('0.5')
          memory: '1Gi'
        }
        env: [
          { name: 'COSMOS_CONN_STR', secretRef: 'cosmos-conn-str' }
          { name: 'AZURE_OAI_PAYG_KEY', secretRef: 'azure-oai-payg-key' }
          { name: 'AZURE_OAI_API_VERSION', value: '2024-10-21' }
        ]
      }]
      scale: { minReplicas: 0, maxReplicas: 10 }
    }
  }
}

// ── Prompt Registry Container App (internal) ─────────────────────────────────
resource registryApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: '${appName}-registry'
  location: location
  properties: {
    environmentId: containerEnv.id
    configuration: {
      ingress: {
        external: false
        targetPort: 8002
        transport: 'http'
      }
      secrets: [
        { name: 'cosmos-conn-str', value: cosmosConnStr }
      ]
    }
    template: {
      containers: [{
        name: 'registry'
        image: 'ghcr.io/${ghcrOrg}/llm-platform-registry:${containerTag}'
        resources: {
          cpu: json('0.25')
          memory: '0.5Gi'
        }
        env: [
          { name: 'COSMOS_CONN_STR', secretRef: 'cosmos-conn-str' }
        ]
      }]
      scale: { minReplicas: 0, maxReplicas: 5 }
    }
  }
}

// ── Gateway Container App (public) ───────────────────────────────────────────
resource gatewayApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: '${appName}-gateway'
  location: location
  properties: {
    environmentId: containerEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
      }
      secrets: [
        { name: 'cosmos-conn-str', value: cosmosConnStr }
        { name: 'jwt-secret', value: 'my-super-secret-key-change-in-production-32chars' }
      ]
    }
    template: {
      containers: [{
        name: 'gateway'
        image: 'ghcr.io/${ghcrOrg}/llm-platform-gateway:${containerTag}'
        resources: {
          cpu: json('0.25')
          memory: '0.5Gi'
        }
        env: [
          { name: 'JWT_SECRET', secretRef: 'jwt-secret' }
          { name: 'INFERENCE_SERVICE_URL', value: 'https://${inferenceApp.properties.configuration.ingress.fqdn}' }
          { name: 'PROMPT_REGISTRY_URL', value: 'https://${registryApp.properties.configuration.ingress.fqdn}' }
          { name: 'DEFAULT_RPM', value: '60' }
          { name: 'DEFAULT_TPM', value: '100000' }
        ]
      }]
      scale: { minReplicas: 0, maxReplicas: 10 }
    }
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────
output gatewayUrl string = 'https://${gatewayApp.properties.configuration.ingress.fqdn}'
output cosmosEndpoint string = cosmos.properties.documentEndpoint
