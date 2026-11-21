# Redis

Redis caches session data for the Gateway service. Redis evicts keys with an LRU policy.

Redis Sentinel monitors the Redis primary and promotes a replica on failure.
