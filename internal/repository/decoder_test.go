package repository

import "testing"

func TestSourceValuesRejectsTrailingJSONContent(t *testing.T) {
	if _, err := sourceValues(`["甲"] garbage`); err == nil {
		t.Fatal("expected trailing JSON content to fail")
	}
}
