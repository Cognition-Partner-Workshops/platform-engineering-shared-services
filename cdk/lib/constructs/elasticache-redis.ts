import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elasticache from 'aws-cdk-lib/aws-elasticache';
import { Construct } from 'constructs';

export interface ElastiCacheRedisProps {
  /**
   * Name prefix for the Redis cluster resources.
   */
  readonly clusterName: string;

  /**
   * VPC in which to create the Redis cluster.
   */
  readonly vpc: ec2.IVpc;

  /**
   * ElastiCache node type.
   * @default 'cache.t3.micro'
   */
  readonly nodeType?: string;

  /**
   * Number of cache nodes (1 for dev, 2+ for prod with replicas).
   * @default 1
   */
  readonly numCacheNodes?: number;

  /**
   * Redis engine version.
   * @default '7.1'
   */
  readonly engineVersion?: string;

  /**
   * Whether to enable automatic failover (requires numCacheNodes >= 2).
   * @default false
   */
  readonly automaticFailoverEnabled?: boolean;

  /**
   * Whether to enable Multi-AZ (requires automaticFailover).
   * @default false
   */
  readonly multiAzEnabled?: boolean;

  /**
   * CIDR blocks allowed to connect to Redis (typically the VPC CIDR).
   */
  readonly allowedCidrs?: string[];

  /**
   * Security groups allowed to connect to Redis (e.g., EKS node SG).
   */
  readonly allowedSecurityGroups?: ec2.ISecurityGroup[];

  /**
   * Environment label (dev, staging, prod).
   */
  readonly environment: string;
}

/**
 * Provisions an ElastiCache Redis cluster as the platform's shared caching
 * layer. This is the single source of truth for distributed cache state across
 * all microservices in the cluster.
 *
 * Dev/staging uses a single-node cluster (cache.t3.micro) for cost efficiency.
 * Production uses a replication group with automatic failover and Multi-AZ.
 *
 * All resources use DESTROY removal policy for clean teardown.
 */
export class ElastiCacheRedis extends Construct {
  public readonly securityGroup: ec2.SecurityGroup;
  public readonly subnetGroup: elasticache.CfnSubnetGroup;
  public readonly parameterGroup: elasticache.CfnParameterGroup;
  public readonly replicationGroup: elasticache.CfnReplicationGroup;

  /**
   * The primary endpoint address for the Redis cluster.
   */
  public readonly primaryEndpoint: string;

  /**
   * The port for the Redis cluster (always 6379).
   */
  public readonly port: string = '6379';

  constructor(scope: Construct, id: string, props: ElastiCacheRedisProps) {
    super(scope, id);

    const nodeType = props.nodeType ?? 'cache.t3.micro';
    const numCacheNodes = props.numCacheNodes ?? 1;
    const engineVersion = props.engineVersion ?? '7.1';
    const automaticFailover = props.automaticFailoverEnabled ?? false;
    const multiAz = props.multiAzEnabled ?? false;

    // Security group for the Redis cluster
    this.securityGroup = new ec2.SecurityGroup(this, 'SecurityGroup', {
      vpc: props.vpc,
      description: `Security group for ${props.clusterName} Redis cluster`,
      securityGroupName: `${props.clusterName}-redis-sg`,
      allowAllOutbound: false,
    });
    this.securityGroup.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    // Allow inbound from specified CIDRs
    if (props.allowedCidrs) {
      for (const cidr of props.allowedCidrs) {
        this.securityGroup.addIngressRule(
          ec2.Peer.ipv4(cidr),
          ec2.Port.tcp(6379),
          `Allow Redis access from ${cidr}`,
        );
      }
    }

    // Allow inbound from VPC CIDR by default
    this.securityGroup.addIngressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(6379),
      'Allow Redis access from VPC',
    );

    // Allow inbound from specified security groups
    if (props.allowedSecurityGroups) {
      for (const sg of props.allowedSecurityGroups) {
        this.securityGroup.addIngressRule(
          ec2.Peer.securityGroupId(sg.securityGroupId),
          ec2.Port.tcp(6379),
          `Allow Redis access from ${sg.securityGroupId}`,
        );
      }
    }

    // Subnet group — place Redis in private subnets
    this.subnetGroup = new elasticache.CfnSubnetGroup(this, 'SubnetGroup', {
      cacheSubnetGroupName: `${props.clusterName}-redis-subnets`,
      description: `Subnet group for ${props.clusterName} Redis`,
      subnetIds: props.vpc.privateSubnets.map(s => s.subnetId),
    });
    this.subnetGroup.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    // Parameter group with eviction policy and memory optimization
    this.parameterGroup = new elasticache.CfnParameterGroup(this, 'ParameterGroup', {
      cacheParameterGroupFamily: 'redis7',
      description: `Parameters for ${props.clusterName} Redis — single-source-of-truth cache`,
      properties: {
        // allkeys-lru: evict least-recently-used keys when memory is full.
        // This ensures the cache self-manages memory while the database
        // remains the authoritative source of truth.
        'maxmemory-policy': 'allkeys-lru',
        // Enable keyspace notifications for cache invalidation events.
        // Services can subscribe to key expiry/eviction events to reload
        // from the database (the source of truth).
        'notify-keyspace-events': 'Ex',
      },
    });
    this.parameterGroup.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    // Replication group (works for both single-node and multi-node)
    this.replicationGroup = new elasticache.CfnReplicationGroup(this, 'ReplicationGroup', {
      replicationGroupDescription: `${props.clusterName} Redis — shared cache layer`,
      replicationGroupId: `${props.clusterName}-redis`,
      engine: 'redis',
      engineVersion: engineVersion,
      cacheNodeType: nodeType,
      numCacheClusters: numCacheNodes,
      automaticFailoverEnabled: automaticFailover,
      multiAzEnabled: multiAz,
      cacheSubnetGroupName: this.subnetGroup.cacheSubnetGroupName!,
      cacheParameterGroupName: this.parameterGroup.ref,
      securityGroupIds: [this.securityGroup.securityGroupId],
      port: 6379,
      atRestEncryptionEnabled: true,
      transitEncryptionEnabled: false, // Simplifies connectivity from pods
      snapshotRetentionLimit: props.environment === 'prod' ? 7 : 0,
      tags: [
        { key: 'platform/component', value: 'shared-cache' },
        { key: 'platform/environment', value: props.environment },
        { key: 'platform/managed-by', value: 'cdk' },
      ],
    });
    this.replicationGroup.addDependency(this.subnetGroup);
    this.replicationGroup.addDependency(this.parameterGroup);
    this.replicationGroup.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    this.primaryEndpoint = this.replicationGroup.attrPrimaryEndPointAddress;
  }
}
