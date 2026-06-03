param location string = 'eastus'
param appName string = 'llm-platform'

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2023-04-15' = {
  name: '${appName}-cosmos'
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [{ locationName: location, failoverPriority: 0 }]
    capabilities: [{ name: 'EnableServerless' }]  // free tier friendly
  }
}

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: '${appName}-gateway'
  location: location
  properties: {
    configuration: {
      ingress: { external: true, targetPort: 8000 }
    }
    template: {
      containers: [{
        name: 'gateway'
        image: 'ghcr.io/YOUR_ORG/llm-platform-gateway:latest'
        resources: { cpu: '0.25', memory: '0.5Gi' }
      }]
      scale: { minReplicas: 0, maxReplicas: 5 }  // scale-to-zero saves credits
    }
  }
}