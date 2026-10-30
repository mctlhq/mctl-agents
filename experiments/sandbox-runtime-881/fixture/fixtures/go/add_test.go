package fixture

import "testing"

func TestAdd(t *testing.T) {
	if Add([]int{1, 2, 3}) != 6 {
		t.Fatal("bad sum")
	}
}
