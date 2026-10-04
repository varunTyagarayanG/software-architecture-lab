# Cache–Database Synchronization

## Summary

This session explains the rules for keeping a cache and database synchronized, the process of fetching data on a cache miss and setting it in the cache, and a race condition where an outdated value can be written back into the cache during a price update in an online store.

It also discusses the limitations of using TTL (Time-To-Live) or expiration as a simple solution and introduces a better approach based on **lease tokens**. The session mentions that Facebook developed this technique for its caching system in 2013.

## Key Takeaway

The basic rule for cache–database synchronization is:

> **Update the database first, and then delete the corresponding key from the cache.**

However, a race condition can occur when a request experiences a cache miss and is fetching data from the database while another request updates the database and deletes the cache key. The first request may then write an outdated value back into the cache.

TTL is a basic solution, but a **lease token** provides a stronger mechanism by ensuring that an old request cannot populate the cache with stale data after the cache entry has been invalidated.

---

## 1. Cache–Database Synchronization Rule

The straightforward rule for keeping the cache synchronized with the database is:

1. **Update the database first.**
2. **Then delete the corresponding key from the cache.**

This is a common interview topic and is a fundamental principle rather than a particularly complex concept.

For example:

```text
Update DB
   ↓
Delete Cache Key
```

The reason for deleting the cache after updating the database is that the next request can fetch the latest value from the database and repopulate the cache.

---

## 2. Cache Miss and Re-Caching Flow

When a request looks for data in the cache and the data is not present, a **cache miss** occurs.

The typical flow is:

```text
Request
   ↓
Check Cache
   ↓
Cache Miss
   ↓
Fetch latest data from DB
   ↓
Set data in Cache
   ↓
Return data
```

For example, if the product price is not available in the cache:

1. The request checks the cache.
2. The cache does not contain the product price.
3. The request queries the database.
4. The latest price is retrieved.
5. The request stores the price in the cache.
6. The price is returned to the user.

This works normally when there are no concurrent updates. The problem appears when another request changes the database while the first request is still processing the cache miss.

---

## 3. Online Store Price Update Race Condition

Consider an online store where a product currently costs **₹100**.

Initially:

```text
Database: ₹100
Cache:    No value
```

### Step 1 — User A Requests the Product

User A requests the product price.

The cache does not contain the price, so User A experiences a cache miss.

```text
User A
  ↓
Cache Miss
  ↓
Database → ₹100
```

At this point, User A has retrieved the old price of **₹100** from the database but has not yet written it into the cache.

### Step 2 — Admin Updates the Price

While User A's request is still processing, an administrator changes the price:

```text
Database: ₹100 → ₹200
```

The application then follows the standard cache invalidation rule and tries to delete the cache key:

```text
Update Database → ₹200
Delete Cache Key
```

But the cache key is already missing because User A originally experienced a cache miss.

Therefore:

```text
Cache: No value
```

The delete operation has nothing to remove.

### Step 3 — User A Writes the Old Value to the Cache

User A's request now continues.

It still has the value it fetched earlier:

```text
₹100
```

So it writes that value into the cache:

```text
Cache: ₹100
```

Now the system is in an inconsistent state:

```text
Database: ₹200
Cache:    ₹100  ← Stale data
```

Every subsequent user who gets the value from the cache may see the incorrect price of **₹100**.

### The Race Condition

The sequence can be represented as:

```text
User A                         Admin

Cache Miss
   |
   |---- Fetch ₹100 from DB
   |
   |                         Update DB → ₹200
   |                         Delete Cache Key
   |
   |---- Set Cache → ₹100
   |
   ↓
Cache contains stale ₹100
```

This is the important problem that a simple "update DB, then delete cache" strategy does not completely solve.

---

## 4. TTL / Expiration as a Solution

One simple solution is to configure a **TTL (Time-To-Live)** for cache entries.

For example:

```text
Cache: ₹100
TTL: 60 seconds
```

After 60 seconds, the cache entry automatically expires.

This limits how long stale data can remain in the cache.

However, TTL does **not completely prevent** stale data from being written into the cache.

For example:

```text
Database: ₹200
Cache:    ₹100
```

The stale value may still remain available until the TTL expires.

The interviewer in the discussion considered TTL a basic answer and expected a stronger solution.

---

## 5. Lease Token Approach

A better approach is to use a **lease token**.

With a lease-based mechanism, a cache miss does not simply return the data-fetch permission. It also provides a token (lease) that authorizes the request to populate the cache.

The basic idea is:

```text
Cache Miss
   ↓
Get Lease Token
   ↓
Fetch data from DB
   ↓
Before setting cache:
   Check whether lease is still valid
   ↓
If valid → Set Cache
If invalid → Do NOT Set Cache
```

### How It Solves the Race Condition

Consider the same example.

#### Step 1 — User A Gets a Cache Miss

User A checks the cache and finds nothing.

The system gives User A a lease token:

```text
User A → Cache Miss
          ↓
       Lease Token
```

User A then fetches the price:

```text
Database → ₹100
```

#### Step 2 — Admin Updates the Price

The admin changes the price:

```text
Database: ₹100 → ₹200
```

The application then invalidates the cache and, importantly, **invalidates/cancels the associated lease**.

```text
Update DB → ₹200
Delete Cache
Cancel Lease
```

#### Step 3 — User A Tries to Populate the Cache

User A returns with:

```text
Value = ₹100
Lease Token = old/invalid
```

Before allowing User A to write to the cache, the system checks the lease.

The lease is no longer valid.

Therefore:

```text
Do NOT write ₹100 to cache
```

The stale value is prevented from being reinserted into the cache.

---

## 6. Lease Token Flow

The complete flow can be visualized as:

```text
                    Cache Miss
                        |
                        ↓
                 Obtain Lease Token
                        |
                        ↓
                  Fetch from DB
                        |
              ┌─────────┴─────────┐
              ↓                   ↓
       DB Update Happens?       No Update
              |                   |
             Yes                  |
              ↓                   |
       Invalidate Lease            |
              |                   |
              └─────────┬─────────┘
                        ↓
                 Check Lease
                        |
             ┌──────────┴──────────┐
             ↓                     ↓
          Valid                  Invalid
             |                     |
             ↓                     ↓
       Set Cache              Reject Set
```

The important point is:

> **A request can populate the cache only if the lease/token it received is still valid.**

This prevents an older request from overwriting the cache with stale data after another request has changed the database.

---

## 7. TTL vs. Lease Token

| Approach | What it does | Limitation |
|---|---|---|
| TTL | Automatically expires cached data after a period | Stale data can still be served until expiration |
| Cache Delete | Removes the cache after a DB update | A concurrent cache-miss request can reinsert stale data |
| Lease Token | Authorizes a request to populate the cache only while its lease is valid | Requires more sophisticated cache coordination |

The key distinction is:

```text
TTL:
"Eventually remove the stale value."

Lease:
"Prevent an outdated request from writing the stale value."
```

---

## 8. Facebook and Lease-Based Caching

The discussion mentions that Facebook developed a lease-based approach for its caching infrastructure in **2013** to address problems of this kind.

The technique is associated with Facebook's work on improving the consistency and behavior of its distributed caching infrastructure.

The central idea remains:

> **Use a lease/token to control which request is allowed to populate the cache, preventing stale requests from overwriting newer cache state.**

---

## Final Takeaway

For cache–database synchronization, remember these three levels:

### Basic Rule

```text
1. Update DB
2. Delete Cache
```

### Problem

A concurrent cache miss can fetch an old value before the update and later put that old value back into the cache.

```text
Old Request → Fetch ₹100
                    ↓
Admin → DB becomes ₹200 → Delete Cache
                    ↓
Old Request → Cache ₹100  ❌
```

### Better Solution

Use a **lease/token**:

```text
Cache Miss
   ↓
Get Lease
   ↓
Fetch DB
   ↓
Lease still valid?
   ├── Yes → Set Cache
   └── No  → Reject Cache Set
```

This prevents stale data from being reinserted into the cache after the underlying database value has changed.
