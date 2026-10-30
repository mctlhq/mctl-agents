package fixture

import "sync"

// Add sums concurrently so that -race has something to observe.
func Add(xs []int) int {
	var mu sync.Mutex
	var wg sync.WaitGroup
	total := 0
	for _, x := range xs {
		wg.Add(1)
		go func(v int) {
			defer wg.Done()
			mu.Lock()
			total += v
			mu.Unlock()
		}(x)
	}
	wg.Wait()
	return total
}
