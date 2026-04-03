# Cache Strategy: Single Source of Truth

## Guiding Principle

> **The database is always the single source of truth.** Redis is a read-through acceleration layer — never the authoritative store for any data. Every piece of cached data must be reconstructable from the database at any time.

This document defines the platform-standard caching patterns for microservices running on the workshop EKS cluster. All decomposed services (order, inventory, customer, product, etc.) must follow these patterns when integrating with the shared Redis cache.

## Architecture Overview

```
                          ┌─────────────────────────────────────┐
                          │         Client Request               │
                          └──────────────┬──────────────────────┘
                                         │
                                         ▼
                          ┌─────────────────────────────────────┐
                          │         API Gateway                  │
                          │   (response cache / rate limiting)   │
                          └──────────────┬──────────────────────┘
                                         │
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                    ▼
          ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
          │  Order Service  │  │ Product Service  │  │Customer Service │
          └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
                   │                    │                     │
          ┌────────▼────────────────────▼─────────────────────▼────────┐
          │                                                             │
          │                    Shared Redis Cache                       │
          │               (ElastiCache / in-cluster)                    │
          │                                                             │
          │   ┌──────────────────────────────────────────────────────┐  │
          │   │  Namespaced Keys:                                    │  │
          │   │    order-svc:order:{id}     → cached order data      │  │
          │   │    product-svc:product:{id} → cached product data    │  │
          │   │    customer-svc:cust:{id}   → cached customer data   │  │
          │   │    api-gw:resp:{hash}       → cached API response    │  │
          │   └──────────────────────────────────────────────────────┘  │
          └─────────────────────────────┬──────────────────────────────┘
                                        │
                              (cache miss → read from DB)
                                        │
                                        ▼
                          ┌─────────────────────────────────────┐
                          │     Database (Source of Truth)        │
                          │   (per-service owned data store)     │
                          └─────────────────────────────────────┘
```

## Core Rules

1. **Writes always go to the database first.** Never write to the cache without first persisting to the database.
2. **Cache entries are always expendable.** Any key can be evicted at any time — the service must handle cache misses gracefully by falling back to the database.
3. **Every cache key has a TTL.** No key lives forever. TTLs ensure stale data is automatically purged even if explicit invalidation fails.
4. **Services own their cache namespace.** Each service prefixes its keys (e.g., `order-svc:order:123`) to avoid collisions and enable per-service monitoring.
5. **Cross-service data is fetched, not shared via cache.** If the order service needs product data, it calls the product service API — it does not read from `product-svc:*` keys directly.

## Cache Patterns

### Pattern 1: Cache-Aside (Lazy Loading) — Default Pattern

Use this for most read-heavy data. The service checks the cache first; on a miss, it reads from the database and populates the cache.

```
READ path:
  1. Service receives read request
  2. Check Redis for key (e.g., "product-svc:product:42")
  3a. CACHE HIT  → return cached data
  3b. CACHE MISS → read from database
  4. Write result to Redis with TTL
  5. Return data to caller

WRITE path:
  1. Service receives write request
  2. Write to database (source of truth)
  3. DELETE the cache key (invalidate, don't update)
  4. Return success
```

**Why delete instead of update on write?** Deleting is safer — it avoids race conditions where two concurrent writes could leave the cache in an inconsistent state. The next read will re-populate the cache from the database.

```csharp
// C# / .NET example (pseudocode)
public async Task<Product> GetProductAsync(int id)
{
    var cacheKey = $"product-svc:product:{id}";
    var cached = await _redis.StringGetAsync(cacheKey);

    if (cached.HasValue)
        return JsonSerializer.Deserialize<Product>(cached!);

    var product = await _dbContext.Products.FindAsync(id);
    if (product != null)
    {
        await _redis.StringSetAsync(
            cacheKey,
            JsonSerializer.Serialize(product),
            TimeSpan.FromMinutes(10)  // TTL
        );
    }
    return product;
}

public async Task UpdateProductAsync(Product product)
{
    // 1. Write to database FIRST (source of truth)
    _dbContext.Products.Update(product);
    await _dbContext.SaveChangesAsync();

    // 2. Invalidate cache (don't update — next read will repopulate)
    await _redis.KeyDeleteAsync($"product-svc:product:{product.Id}");
}
```

### Pattern 2: Write-Through — For Critical Consistency

Use when the cache must always reflect the latest database state (e.g., inventory counts, pricing). The write path updates both the database and cache atomically.

```
WRITE path:
  1. Service receives write request
  2. Begin database transaction
  3. Write to database
  4. Commit transaction
  5. Write to cache with TTL (overwrite)
  6. Return success

  On cache write failure → DELETE cache key (fall back to cache-aside)
```

**When to use:** Inventory levels, pricing, user session data — anything where serving stale data for even a short window is unacceptable.

### Pattern 3: Event-Driven Invalidation — For Cross-Service Consistency

When Service A updates data that Service B has cached, use domain events to trigger invalidation.

```
  Order Service updates order status
       │
       ▼
  Publish event: "order.status.updated" { orderId: 123 }
       │
       ├──→ Notification Service: sends email
       └──→ API Gateway: invalidates response cache
              └── DELETE "api-gw:resp:orders/123"
```

**Implementation:** Use Redis Pub/Sub or a message broker (RabbitMQ, SQS). The platform Redis has keyspace notifications enabled (`notify-keyspace-events Ex`) so services can subscribe to key expiry events.

## Key Naming Convention

All cache keys must follow this format:

```
{service-name}:{entity-type}:{identifier}[:{qualifier}]
```

Examples:
| Key | Description | TTL |
|-----|-------------|-----|
| `order-svc:order:123` | Cached order by ID | 5m |
| `order-svc:orders:user:456` | User's order list | 2m |
| `product-svc:product:42` | Product by ID | 10m |
| `product-svc:catalog:page:1` | Paginated catalog | 1m |
| `customer-svc:cust:789` | Customer profile | 10m |
| `inventory-svc:stock:42` | Product stock count | 30s |
| `api-gw:resp:sha256abc` | Cached API response | 60s |
| `api-gw:rate:ip:10.0.1.5` | Rate limit counter | 60s |

## TTL Guidelines

| Data Type | Recommended TTL | Rationale |
|-----------|----------------|-----------|
| Static reference data (countries, categories) | 1 hour | Rarely changes |
| Product catalog | 5-10 minutes | Changes infrequently |
| User profiles | 5-10 minutes | Moderate change frequency |
| Order data | 2-5 minutes | Changes during order lifecycle |
| Inventory / stock counts | 15-30 seconds | High change frequency, stale data = overselling |
| API response cache | 30-60 seconds | Aggregated data, short freshness window |
| Rate limit counters | 1 minute (sliding) | Must be accurate for throttling |
| Session data | 30 minutes | Matches session timeout |

## Redis Configuration (Platform-Managed)

The platform provisions Redis with these settings to enforce the single-source-of-truth model:

| Setting | Value | Rationale |
|---------|-------|-----------|
| `maxmemory-policy` | `allkeys-lru` | Evict least-recently-used keys when full — safe because the database is the source of truth |
| `notify-keyspace-events` | `Ex` | Enable expired-key notifications for event-driven invalidation |
| `save` | `""` (disabled) | No RDB persistence — cache is ephemeral and reconstructable from the database |
| `maxmemory` | `192mb` (in-cluster) / instance default (ElastiCache) | Bounded memory prevents OOM |
| `at-rest-encryption` | enabled (ElastiCache) | Data protection at rest |

## Connecting to Redis

### Environment Variables

The platform injects these environment variables into app pods:

| Variable | Description | Example |
|----------|-------------|---------|
| `REDIS_HOST` | Redis endpoint (ElastiCache or in-cluster) | `workshop-dev-redis.xxxxx.cache.amazonaws.com` |
| `REDIS_PORT` | Redis port | `6379` |
| `REDIS_URL` | Full connection string | `redis://workshop-dev-redis.xxxxx.cache.amazonaws.com:6379` |

### .NET / C# (StackExchange.Redis)

```csharp
// In Program.cs / Startup.cs
builder.Services.AddStackExchangeRedisCache(options =>
{
    options.Configuration = Environment.GetEnvironmentVariable("REDIS_URL")
        ?? "redis:6379";
    options.InstanceName = "order-svc:";  // Auto-prefix all keys
});
```

### Node.js (ioredis)

```typescript
import Redis from 'ioredis';

const redis = new Redis({
  host: process.env.REDIS_HOST ?? 'redis-master.redis.svc.cluster.local',
  port: parseInt(process.env.REDIS_PORT ?? '6379'),
  keyPrefix: 'product-svc:',  // Auto-prefix all keys
  retryStrategy: (times) => Math.min(times * 50, 2000),
});
```

## Monitoring

The platform Prometheus + Grafana stack collects Redis metrics automatically via the redis-exporter sidecar.

### Key Metrics to Watch

| Metric | Alert Threshold | Meaning |
|--------|----------------|---------|
| `redis_memory_used_bytes / redis_memory_max_bytes` | > 80% | Cache is nearing capacity — review TTLs or scale |
| `redis_keyspace_hits / (redis_keyspace_hits + redis_keyspace_misses)` | < 50% | Low hit rate — cache is not effective, review key strategy |
| `redis_evicted_keys_total` | > 100/min sustained | Heavy eviction — increase memory or reduce cached data volume |
| `redis_connected_clients` | > 100 | Connection pool exhaustion risk |
| `redis_commands_duration_seconds_bucket` | p99 > 5ms | Redis is slow — investigate network or memory pressure |

### Grafana Dashboard

A pre-built Redis dashboard is available at `Dashboards → Platform → Redis Cache`. It shows:
- Memory usage and eviction rate
- Hit/miss ratio per service (via key prefix)
- Command latency percentiles
- Connected clients over time
- Keys by TTL distribution

## Anti-Patterns (Do NOT Do These)

| Anti-Pattern | Why It Breaks Single Source of Truth | Correct Approach |
|-------------|--------------------------------------|------------------|
| Write to cache first, then database | Cache has data the database doesn't — if the DB write fails, cache is inconsistent | Always write to database first |
| Cache without TTL | Data becomes permanently stale if invalidation fails | Every key must have a TTL |
| Read another service's cache keys directly | Tight coupling — breaks service autonomy and data ownership | Call the service API instead |
| Use cache as primary data store | Data loss on eviction or restart | Cache is acceleration only — database is the store |
| Update cache on write (instead of delete) | Race conditions between concurrent writers | Delete on write; let the next read repopulate |
| Cache null/empty results without short TTL | Negative cache persists even after data is created | Cache nulls with a very short TTL (15-30s) or skip caching |
| Store large objects (> 1MB) in cache | Memory pressure, slow serialization, large network payloads | Cache IDs/summaries, not full objects; use pagination |

## Cache Failure Resilience

The cache layer must be treated as **optional and unreliable**. Services must degrade gracefully:

```
1. Redis is down → Service falls back to direct database reads
2. Redis is slow  → Service uses circuit breaker (timeout 100ms)
3. Redis evicts keys → Service handles cache miss, reads from DB
4. Redis data is corrupt → Service detects (e.g., deserialization fails),
                           deletes key, reads from DB
```

### Circuit Breaker Configuration

```csharp
// Example: Polly circuit breaker for Redis calls
var circuitBreakerPolicy = Policy
    .Handle<RedisConnectionException>()
    .Or<RedisTimeoutException>()
    .CircuitBreakerAsync(
        exceptionsAllowedBeforeBreaking: 5,
        durationOfBreak: TimeSpan.FromSeconds(30)
    );
```

When the circuit breaker is open, the service reads directly from the database. This is slower but correct — the database is always the source of truth.

## Migration from Monolith

When decomposing the monolith into microservices, follow this sequence:

1. **Extract the service** with its own database (standard decomposition)
2. **Add cache-aside reads** using the shared Redis with the service's key prefix
3. **Add write-path invalidation** (delete cache key after DB write)
4. **Add ServiceMonitor** for Redis metrics
5. **Configure TTLs** based on data volatility (see TTL Guidelines above)
6. **Test cache failure mode** — verify the service works with Redis unavailable

Do NOT add caching during the decomposition itself. Get the service working with direct database access first, then layer caching as an optimization.
