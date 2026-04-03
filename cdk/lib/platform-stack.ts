import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as eks from 'aws-cdk-lib/aws-eks';
import { Construct } from 'constructs';
import {
  Networking,
  EksCluster,
  EcrRepositories,
  DnsZone,
  K8sNamespaces,
  NamespaceConfig,
  ElastiCacheRedis,
} from './constructs';

export interface PlatformStackProps extends cdk.StackProps {
  /**
   * Environment name (dev, staging, prod).
   */
  readonly environment: string;

  /**
   * Kubernetes version for the EKS cluster.
   * @default eks.KubernetesVersion.V1_31
   */
  readonly kubernetesVersion?: eks.KubernetesVersion;

  /**
   * EC2 instance types for the managed node group.
   * @default [ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MEDIUM)]
   */
  readonly nodeInstanceTypes?: ec2.InstanceType[];

  /**
   * Minimum number of worker nodes.
   * @default 1
   */
  readonly nodeMinSize?: number;

  /**
   * Maximum number of worker nodes.
   * @default 3
   */
  readonly nodeMaxSize?: number;

  /**
   * Desired number of worker nodes.
   * @default 2
   */
  readonly nodeDesiredSize?: number;

  /**
   * Number of NAT Gateways (1 for non-prod cost saving).
   * @default 1
   */
  readonly natGateways?: number;

  /**
   * Maximum AZs to use.
   * @default 2
   */
  readonly maxAzs?: number;

  /**
   * ECR repository names to create.
   */
  readonly ecrRepositoryNames: string[];

  /**
   * Domain name for the Route 53 hosted zone.
   * @default undefined (no DNS zone created)
   */
  readonly domainName?: string;

  /**
   * Kubernetes namespaces to provision.
   */
  readonly namespaces: NamespaceConfig[];

  /**
   * Whether to provision a shared ElastiCache Redis cluster.
   * @default true
   */
  readonly redisEnabled?: boolean;

  /**
   * ElastiCache node type for Redis.
   * @default 'cache.t3.micro'
   */
  readonly redisNodeType?: string;

  /**
   * Number of Redis cache nodes (1 for dev, 2+ for prod HA).
   * @default 1
   */
  readonly redisNumNodes?: number;

  /**
   * Whether to enable Redis automatic failover (requires redisNumNodes >= 2).
   * @default false
   */
  readonly redisAutomaticFailover?: boolean;

  /**
   * Whether to enable Redis Multi-AZ (requires automatic failover).
   * @default false
   */
  readonly redisMultiAz?: boolean;
}

/**
 * Main platform stack that provisions the complete shared infrastructure:
 * VPC, EKS cluster, ECR repositories, DNS zone, and Kubernetes namespaces.
 *
 * All resources use RemovalPolicy.DESTROY so the entire stack can be torn
 * down cleanly with `cdk destroy`.
 */
export class PlatformStack extends cdk.Stack {
  public readonly networking: Networking;
  public readonly eksCluster: EksCluster;
  public readonly ecrRepositories: EcrRepositories;
  public readonly dnsZone?: DnsZone;
  public readonly k8sNamespaces: K8sNamespaces;
  public readonly elastiCacheRedis?: ElastiCacheRedis;

  constructor(scope: Construct, id: string, props: PlatformStackProps) {
    super(scope, id, props);

    // ──────────────────────────────────────────────────────────────────────
    // VPC and Networking
    // ──────────────────────────────────────────────────────────────────────
    this.networking = new Networking(this, 'Networking', {
      vpcName: `workshop-${props.environment}`,
      maxAzs: props.maxAzs ?? 2,
      natGateways: props.natGateways ?? 1,
    });

    // ──────────────────────────────────────────────────────────────────────
    // EKS Cluster
    // ──────────────────────────────────────────────────────────────────────
    this.eksCluster = new EksCluster(this, 'EksCluster', {
      clusterName: `workshop-${props.environment}`,
      kubernetesVersion: props.kubernetesVersion,
      vpc: this.networking.vpc,
      nodeInstanceTypes: props.nodeInstanceTypes,
      nodeMinSize: props.nodeMinSize,
      nodeMaxSize: props.nodeMaxSize,
      nodeDesiredSize: props.nodeDesiredSize,
      environment: props.environment,
    });

    // ──────────────────────────────────────────────────────────────────────
    // ECR Repositories
    // ──────────────────────────────────────────────────────────────────────
    this.ecrRepositories = new EcrRepositories(this, 'EcrRepositories', {
      repositoryNames: props.ecrRepositoryNames,
    });

    // ──────────────────────────────────────────────────────────────────────
    // DNS Zone (optional)
    // ──────────────────────────────────────────────────────────────────────
    if (props.domainName) {
      this.dnsZone = new DnsZone(this, 'DnsZone', {
        domainName: props.domainName,
      });
    }

    // ──────────────────────────────────────────────────────────────────────
    // Kubernetes Namespaces
    // ──────────────────────────────────────────────────────────────────────
    this.k8sNamespaces = new K8sNamespaces(this, 'K8sNamespaces', {
      cluster: this.eksCluster.cluster,
      namespaces: props.namespaces,
    });

    // ──────────────────────────────────────────────────────────────────────
    // Shared Cache (ElastiCache Redis)
    // ──────────────────────────────────────────────────────────────────────
    if (props.redisEnabled !== false) {
      this.elastiCacheRedis = new ElastiCacheRedis(this, 'ElastiCacheRedis', {
        clusterName: `workshop-${props.environment}`,
        vpc: this.networking.vpc,
        nodeType: props.redisNodeType,
        numCacheNodes: props.redisNumNodes,
        automaticFailoverEnabled: props.redisAutomaticFailover,
        multiAzEnabled: props.redisMultiAz,
        environment: props.environment,
      });
    }

    // ──────────────────────────────────────────────────────────────────────
    // Outputs
    // ──────────────────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'ClusterName', {
      value: this.eksCluster.cluster.clusterName,
      description: 'EKS cluster name',
    });

    new cdk.CfnOutput(this, 'ClusterEndpoint', {
      value: this.eksCluster.cluster.clusterEndpoint,
      description: 'EKS cluster API endpoint',
    });

    new cdk.CfnOutput(this, 'ClusterArn', {
      value: this.eksCluster.cluster.clusterArn,
      description: 'EKS cluster ARN',
    });

    new cdk.CfnOutput(this, 'KubectlConfigCommand', {
      value: `aws eks update-kubeconfig --region ${this.region} --name ${this.eksCluster.cluster.clusterName}`,
      description: 'Command to configure kubectl',
    });

    if (this.dnsZone) {
      new cdk.CfnOutput(this, 'HostedZoneId', {
        value: this.dnsZone.zone.hostedZoneId,
        description: 'Route 53 hosted zone ID',
      });
    }

    if (this.elastiCacheRedis) {
      new cdk.CfnOutput(this, 'RedisEndpoint', {
        value: this.elastiCacheRedis.primaryEndpoint,
        description: 'ElastiCache Redis primary endpoint',
      });

      new cdk.CfnOutput(this, 'RedisPort', {
        value: this.elastiCacheRedis.port,
        description: 'ElastiCache Redis port',
      });
    }
  }
}
