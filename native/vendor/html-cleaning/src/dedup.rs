//! Content deduplication utilities.
//!
//! LRU-based cache for detecting duplicate text content.

use std::collections::HashMap;

/// Simple LRU cache for text deduplication.
///
/// Maintains insertion order and evicts the oldest entry when capacity is reached.
/// Note: Updating an existing key does NOT refresh its position (matches Go behavior).
///
/// # Example
///
/// ```
/// use html_cleaning::dedup::LruCache;
///
/// let mut cache = LruCache::new(3);
/// cache.put("a", 1);
/// cache.put("b", 2);
/// assert_eq!(cache.get("a"), Some(1));
/// assert!(cache.has("b"));
/// ```
pub struct LruCache {
    max_size: usize,
    keys: Vec<String>,
    data: HashMap<String, i32>,
}

impl LruCache {
    /// Create new cache with specified capacity.
    #[must_use]
    pub fn new(max_size: usize) -> Self {
        Self {
            max_size,
            keys: Vec::new(),
            data: HashMap::new(),
        }
    }

    /// Get value for key.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<i32> {
        self.data.get(key).copied()
    }

    /// Check if key exists.
    #[must_use]
    pub fn has(&self, key: &str) -> bool {
        self.data.contains_key(key)
    }

    /// Store key-value pair, evicting oldest if at capacity.
    pub fn put(&mut self, key: &str, value: i32) {
        if self.data.contains_key(key) {
            self.data.insert(key.to_string(), value);
            return;
        }

        if self.max_size == 0 {
            return;
        }

        if self.keys.len() >= self.max_size {
            if let Some(oldest_key) = self.keys.first().cloned() {
                self.keys.remove(0);
                self.data.remove(&oldest_key);
            }
        }

        self.keys.push(key.to_string());
        self.data.insert(key.to_string(), value);
    }

    /// Clear all entries.
    pub fn clear(&mut self) {
        self.keys.clear();
        self.data.clear();
    }

    /// Get current size.
    #[must_use]
    pub fn len(&self) -> usize {
        self.keys.len()
    }

    /// Check if empty.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.keys.is_empty()
    }

    /// Remove a specific key from the cache.
    pub fn remove(&mut self, key: &str) {
        if !self.data.contains_key(key) {
            return;
        }

        // Find and remove from keys list
        if let Some(idx) = self.keys.iter().position(|k| k == key) {
            self.keys.remove(idx);
        }
        self.data.remove(key);
    }
}

/// LRU-based content deduplicator.
///
/// Tracks seen text fragments to detect duplicates during processing.
///
/// # Example
///
/// ```
/// use html_cleaning::dedup::Deduplicator;
///
/// let mut dedup = Deduplicator::new(100);
///
/// assert!(!dedup.is_duplicate("first occurrence"));
/// assert!(!dedup.is_duplicate("first occurrence")); // seen once
/// assert!(!dedup.is_duplicate("first occurrence")); // seen twice
/// assert!(dedup.is_duplicate("first occurrence"));  // now duplicate (>2)
/// ```
pub struct Deduplicator {
    cache: LruCache,
    threshold: i32,
}

impl Deduplicator {
    /// Create with specified capacity.
    ///
    /// Uses default threshold of 2 (text seen more than 2 times is duplicate).
    ///
    /// # Arguments
    /// * `capacity` - Maximum number of entries to track
    #[must_use]
    pub fn new(capacity: usize) -> Self {
        Self {
            cache: LruCache::new(capacity),
            threshold: 2,
        }
    }

    /// Create with capacity and custom duplicate threshold.
    ///
    /// # Arguments
    /// * `capacity` - Maximum entries
    /// * `threshold` - Number of times text can appear before considered duplicate
    #[must_use]
    pub fn with_threshold(capacity: usize, threshold: i32) -> Self {
        Self {
            cache: LruCache::new(capacity),
            threshold,
        }
    }

    /// Check if text is duplicate, adding to cache.
    ///
    /// Returns `true` if text has been seen more than threshold times.
    pub fn is_duplicate(&mut self, text: &str) -> bool {
        let count = self.cache.get(text).unwrap_or(0);
        let is_dup = count > self.threshold;

        // Always increment count
        self.cache.put(text, count + 1);

        is_dup
    }

    /// Check without adding to cache.
    #[must_use]
    pub fn check(&self, text: &str) -> bool {
        self.cache
            .get(text)
            .is_some_and(|count| count > self.threshold)
    }

    /// Check if text has been seen (regardless of count).
    #[must_use]
    pub fn has_seen(&self, text: &str) -> bool {
        self.cache.has(text)
    }

    /// Clear the cache.
    pub fn clear(&mut self) {
        self.cache.clear();
    }

    /// Get current cache size.
    #[must_use]
    pub fn len(&self) -> usize {
        self.cache.len()
    }

    /// Check if cache is empty.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.cache.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_new_deduplicator() {
        let dedup = Deduplicator::new(100);
        assert!(dedup.is_empty());
        assert_eq!(dedup.len(), 0);
    }

    #[test]
    fn test_is_duplicate() {
        let mut dedup = Deduplicator::new(100);

        // First 3 occurrences should not be duplicate (threshold is 2)
        assert!(!dedup.is_duplicate("test"));
        assert!(!dedup.is_duplicate("test"));
        assert!(!dedup.is_duplicate("test"));

        // Fourth should be duplicate
        assert!(dedup.is_duplicate("test"));
    }

    #[test]
    fn test_check_without_adding() {
        let mut dedup = Deduplicator::new(100);

        // check on unseen text returns false without modifying state
        assert!(!dedup.check("test"));
        assert!(!dedup.has_seen("test"));

        // Add text enough times to exceed threshold
        dedup.is_duplicate("test");
        dedup.is_duplicate("test");
        dedup.is_duplicate("test");

        // check returns true now (count 3 > threshold 2)
        assert!(dedup.check("test"));
    }

    #[test]
    fn test_has_seen() {
        let mut dedup = Deduplicator::new(100);

        assert!(!dedup.has_seen("test"));
        dedup.is_duplicate("test");
        assert!(dedup.has_seen("test"));
    }

    #[test]
    fn test_custom_threshold() {
        let mut dedup = Deduplicator::with_threshold(100, 1);

        // With threshold 1, second occurrence is duplicate
        assert!(!dedup.is_duplicate("test")); // count: 1
        assert!(!dedup.is_duplicate("test")); // count: 2, 1 > 1 = false
        assert!(dedup.is_duplicate("test")); // count: 3, 2 > 1 = true
    }

    #[test]
    fn test_clear() {
        let mut dedup = Deduplicator::new(100);

        dedup.is_duplicate("test");
        assert!(!dedup.is_empty());

        dedup.clear();
        assert!(dedup.is_empty());
        assert!(!dedup.has_seen("test"));
    }

    #[test]
    fn test_capacity_eviction() {
        let mut dedup = Deduplicator::new(2);

        dedup.is_duplicate("a");
        dedup.is_duplicate("b");
        assert_eq!(dedup.len(), 2);

        dedup.is_duplicate("c"); // Should evict "a"
        assert_eq!(dedup.len(), 2);
        assert!(!dedup.has_seen("a"));
        assert!(dedup.has_seen("b"));
        assert!(dedup.has_seen("c"));
    }

    // ========== LruCache Tests ==========

    #[test]
    fn test_lru_basic_put_get() {
        let mut cache = LruCache::new(10);

        cache.put("key1", 1);
        cache.put("key2", 2);

        assert_eq!(cache.get("key1"), Some(1));
        assert_eq!(cache.get("key2"), Some(2));
        assert_eq!(cache.get("key3"), None);
    }

    #[test]
    fn test_lru_has() {
        let mut cache = LruCache::new(10);

        cache.put("exists", 1);

        assert!(cache.has("exists"));
        assert!(!cache.has("missing"));
    }

    #[test]
    fn test_lru_eviction_at_capacity() {
        let mut cache = LruCache::new(3);

        cache.put("a", 1);
        cache.put("b", 2);
        cache.put("c", 3);

        // All three should exist
        assert!(cache.has("a"));
        assert!(cache.has("b"));
        assert!(cache.has("c"));

        // Add fourth, should evict "a" (oldest)
        cache.put("d", 4);

        assert!(!cache.has("a")); // Evicted
        assert!(cache.has("b"));
        assert!(cache.has("c"));
        assert!(cache.has("d"));
    }

    #[test]
    fn test_lru_update_existing_key() {
        let mut cache = LruCache::new(3);

        cache.put("a", 1);
        cache.put("b", 2);
        cache.put("c", 3);

        // Update "a" - should NOT change order
        cache.put("a", 100);

        assert_eq!(cache.get("a"), Some(100));

        // Add new key, should still evict "a" (oldest by insertion)
        cache.put("d", 4);

        // Updating doesn't refresh position, so "a" should be evicted
        assert!(!cache.has("a"));
    }

    #[test]
    fn test_lru_remove() {
        let mut cache = LruCache::new(10);

        cache.put("a", 1);
        cache.put("b", 2);

        assert!(cache.has("a"));
        cache.remove("a");
        assert!(!cache.has("a"));
        assert!(cache.has("b"));
    }

    #[test]
    fn test_lru_clear() {
        let mut cache = LruCache::new(10);

        cache.put("a", 1);
        cache.put("b", 2);

        assert_eq!(cache.len(), 2);
        cache.clear();
        assert_eq!(cache.len(), 0);
        assert!(cache.is_empty());
    }

    #[test]
    fn test_lru_zero_capacity() {
        let mut cache = LruCache::new(0);

        // With zero capacity, nothing should be stored
        cache.put("a", 1);
        assert!(!cache.has("a"));
    }

    #[test]
    fn test_lru_len() {
        let mut cache = LruCache::new(10);

        assert_eq!(cache.len(), 0);
        cache.put("a", 1);
        assert_eq!(cache.len(), 1);
        cache.put("b", 2);
        assert_eq!(cache.len(), 2);
    }

    #[test]
    fn test_lru_multiple_evictions() {
        let mut cache = LruCache::new(2);

        cache.put("a", 1);
        cache.put("b", 2);
        cache.put("c", 3); // Evicts "a"
        cache.put("d", 4); // Evicts "b"

        assert!(!cache.has("a"));
        assert!(!cache.has("b"));
        assert!(cache.has("c"));
        assert!(cache.has("d"));
        assert_eq!(cache.len(), 2);
    }

    #[test]
    fn test_lru_remove_then_add() {
        let mut cache = LruCache::new(3);

        cache.put("a", 1);
        cache.put("b", 2);
        cache.remove("a");

        // Should have room for "a" again
        cache.put("a", 10);
        assert_eq!(cache.get("a"), Some(10));
        assert_eq!(cache.len(), 2);
    }

    #[test]
    fn test_lru_single_capacity() {
        let mut cache = LruCache::new(1);

        cache.put("a", 1);
        assert!(cache.has("a"));

        cache.put("b", 2);
        assert!(!cache.has("a")); // Evicted
        assert!(cache.has("b"));
    }

    #[test]
    fn test_lru_empty_operations() {
        let mut cache = LruCache::new(10);

        assert!(cache.is_empty());
        assert_eq!(cache.len(), 0);
        assert_eq!(cache.get("missing"), None);
        assert!(!cache.has("missing"));

        // Remove on empty cache should not panic
        cache.remove("missing");
        assert!(cache.is_empty());

        // Clear on empty cache should not panic
        cache.clear();
        assert!(cache.is_empty());
    }
}
