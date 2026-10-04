# Login API Rate Limiting — Interview Notes

## Summary

This session discussed **preventing random password attempts on a Login API** as an interview question related to **10–15 LPA packages**. It covered **rate limiting, fixed window counters, the boundary problem with fixed windows, token bucket design, and using shared Redis storage for a 10-server setup**.

## Key Takeaway

A simple **fixed window counter** can allow **twice the intended number of requests within just 2 seconds** because of the minute boundary problem.

A **token bucket** provides more accurate rate control because tokens are refilled at a fixed rate. In a **multi-server setup**, the bucket/counter state should be stored in a shared storage system such as **Redis**.

## Rate Limiting on the Login API

Rate limiting is necessary to prevent repeated login attempts using random passwords.

If someone repeatedly tries to log in using random passwords for another user's email, a **rate limiter acts as a guard** by counting requests for each user.

Once the limit is exceeded, the request is rejected and the server returns:

**HTTP 429 — Too Many Requests**

## Fixed Window Counter Design

A fixed window counter can be used to implement rate limiting by maintaining a counter for each user.

For example:

- Allow **100 requests per minute** for each user.
- Maintain a counter for every user.
- Increment the counter whenever a request arrives.
- Once the counter reaches 100, reject additional requests.
- When the minute ends, reset the counter to zero.

### Example

```text
User → Login Request
          ↓
     Check Counter
          ↓
    Is count < 100?
       /       \
     Yes        No
      ↓          ↓
  Allow       Reject
  Request     HTTP 429
```

## The Fixed Window Boundary Problem

The fixed window counter has a major weakness around the **minute boundary**.

Suppose the limit is **100 requests per minute**.

A user can:

1. Send 100 requests during the **59th second** of the current minute.
2. At the beginning of the next minute, the counter resets.
3. Immediately send another 100 requests during the **first second**.

Therefore:

**100 requests + 100 requests = 200 requests in approximately 2 seconds**

Even though the intended limit was only **100 requests per minute**.

### Timeline

```text
Minute 1
-----------------------------------------
59th second
→ 100 requests
→ Counter = 100
→ Requests allowed

Minute 2
-----------------------------------------
1st second
→ Counter resets to 0
→ Another 100 requests
→ Requests allowed

Total:
200 requests in ~2 seconds
```

This is known as the **fixed window boundary problem**.

## Token Bucket Rate Limiting

The **Token Bucket algorithm** can solve the boundary problem more effectively.

For each user, maintain a bucket with a maximum capacity of **100 tokens**.

### Basic Rules

- The bucket can contain a maximum of **100 tokens**.
- Each incoming request consumes **1 token**.
- If a token is available → allow the request.
- If the bucket is empty → reject the request.
- Tokens are continuously added back at a fixed rate.

### Example

Suppose:

**Bucket capacity = 100 tokens**

**Rate = 100 requests/minute**

Then:

```text
100 tokens / 60 seconds
≈ 1.67 tokens per second
```

So approximately **1 token is added every 0.6 seconds**.

### Token Bucket Flow

```text
             Token Refill
                  ↓
        ┌─────────────────┐
        │   Token Bucket  │
        │                 │
        │  100 99 98 ...  │
        └────────┬────────┘
                 │
          Request arrives
                 │
          Is token available?
             /          \
           Yes           No
            ↓             ↓
      Consume token     Reject
            ↓          HTTP 429
        Allow request
```

The important difference is that tokens are **continuously refilled**, rather than resetting everything at a fixed minute boundary.

## Redis for Multiple Servers

In a multi-server application, the rate limiter's state should be stored in a **shared storage system such as Redis**.

Suppose the application has **10 servers**.

If every server maintains the rate limiter in its own RAM:

```text
Server 1 → Counter = 50
Server 2 → Counter = 30
Server 3 → Counter = 20
...
Server 10 → Counter = 10
```

The user could potentially bypass the intended limit because each server has an independent counter.

### The Solution: Shared Redis

Instead, all servers should use the same Redis instance/cluster:

```text
                         ┌── Server 1 ──┐
                         │              │
                         ├── Server 2 ──┤
                         │              │
User → Load Balancer ────┼── Server 3 ──┼──→ Redis
                         │              │
                         ├── ... ───────┤
                         │              │
                         └── Server 10 ─┘
```

All servers read and update the **same rate-limiter state** in Redis.

Therefore, it does not matter which server receives the login request—the rate limit remains consistent.

## Why Redis?

Redis is a good choice because:

- It is primarily **in-memory**, so operations are very fast.
- It supports atomic operations useful for counters.
- Multiple application servers can access the same state.
- It can store token-bucket information such as:
  - Current token count
  - Last refill timestamp
  - User identifier
- It supports expiration/TTL, which is useful for rate-limiter state.

## Interview Answer — Short Version

If asked:

> **How would you prevent brute-force login attempts?**

A good answer would be:

> I would implement rate limiting on the login API. For example, I could use a token bucket that allows a certain number of attempts per user within a given rate. Every login attempt consumes a token, and tokens are refilled at a fixed rate. If the bucket is empty, I would reject the request with HTTP 429. Since the application may run on multiple servers, I would store the bucket state in shared Redis rather than server-local memory. This ensures that the rate limit is consistent regardless of which server handles the request.

## Key Concepts to Remember

| Concept | Purpose |
|---|---|
| Rate Limiter | Controls how many requests a user can make |
| HTTP 429 | Indicates too many requests |
| Fixed Window | Simple counter reset at fixed intervals |
| Fixed Window Problem | Boundary can allow bursts |
| Token Bucket | Smooth rate limiting with controlled bursts |
| Redis | Shared state across multiple servers |
| TTL | Automatically expires unused rate-limit data |
| Brute Force | Repeated password attempts to gain access |

## One-Line Mental Model

```text
Login API
   ↓
Rate Limiter
   ↓
Token Bucket
   ↓
Redis (shared across servers)
   ↓
Allow / Reject (HTTP 429)
```
