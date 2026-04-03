#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { PlatformStack } from '../lib/platform-stack';

const app = new cdk.App();

// ────────────────────────────────────────────────────────────────────────────
// Dev Environment
// ────────────────────────────────────────────────────────────────────────────
new PlatformStack(app, 'WorkshopPlatformDev', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
  },
  environment: 'dev',
  nodeInstanceTypes: [
    ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MEDIUM),
  ],
  nodeMinSize: 1,
  nodeMaxSize: 3,
  nodeDesiredSize: 2,
  natGateways: 1,
  maxAzs: 2,
  ecrRepositoryNames: [
    'workshop/web-frontend',
    'workshop/api-gateway',
    'workshop/order-service',
    'workshop/inventory-service',
    'workshop/customer-service',
    'workshop/product-service',
  ],
  namespaces: [
    {
      name: 'decomposition-dev',
      environment: 'dev',
      team: 'dotnet-angular-monolith',
    },
    {
      name: 'decomposition-staging',
      environment: 'staging',
      team: 'dotnet-angular-monolith',
    },
  ],
  // Shared cache — single-node Redis for dev (cost-optimized)
  redisEnabled: true,
  redisNodeType: 'cache.t3.micro',
  redisNumNodes: 1,
});

// ────────────────────────────────────────────────────────────────────────────
// Staging Environment
// ────────────────────────────────────────────────────────────────────────────
new PlatformStack(app, 'WorkshopPlatformStaging', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
  },
  environment: 'staging',
  nodeInstanceTypes: [
    ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MEDIUM),
  ],
  nodeMinSize: 2,
  nodeMaxSize: 5,
  nodeDesiredSize: 3,
  natGateways: 1,
  maxAzs: 2,
  ecrRepositoryNames: [
    'workshop/web-frontend',
    'workshop/api-gateway',
    'workshop/order-service',
    'workshop/inventory-service',
    'workshop/customer-service',
    'workshop/product-service',
  ],
  namespaces: [
    {
      name: 'decomposition-staging',
      environment: 'staging',
      team: 'dotnet-angular-monolith',
    },
  ],
  // Shared cache — single-node Redis for staging (cost-optimized)
  redisEnabled: true,
  redisNodeType: 'cache.t3.micro',
  redisNumNodes: 1,
});

// ────────────────────────────────────────────────────────────────────────────
// Production Environment
// ────────────────────────────────────────────────────────────────────────────
new PlatformStack(app, 'WorkshopPlatformProd', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
  },
  environment: 'prod',
  nodeInstanceTypes: [
    ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.LARGE),
  ],
  nodeMinSize: 3,
  nodeMaxSize: 10,
  nodeDesiredSize: 3,
  natGateways: 2,
  maxAzs: 3,
  ecrRepositoryNames: [
    'workshop/web-frontend',
    'workshop/api-gateway',
    'workshop/order-service',
    'workshop/inventory-service',
    'workshop/customer-service',
    'workshop/product-service',
  ],
  namespaces: [
    {
      name: 'decomposition-prod',
      environment: 'prod',
      team: 'dotnet-angular-monolith',
    },
  ],
  // Shared cache — HA Redis for production (multi-node with failover)
  redisEnabled: true,
  redisNodeType: 'cache.t3.medium',
  redisNumNodes: 2,
  redisAutomaticFailover: true,
  redisMultiAz: true,
});

app.synth();
