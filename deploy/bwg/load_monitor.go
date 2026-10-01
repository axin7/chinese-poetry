//go:build ignore

// Read-only resource sampling during a bounded production load probe.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

func counters(path string) map[string]uint64 {
	data, err := os.ReadFile(path)
	result := map[string]uint64{}
	if err != nil {
		return result
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		value, err := strconv.ParseUint(strings.TrimSuffix(fields[1], ":"), 10, 64)
		if err == nil {
			result[strings.TrimSuffix(fields[0], ":")] = value
		}
	}
	return result
}

func scalar(path string) uint64 {
	data, _ := os.ReadFile(path)
	value, _ := strconv.ParseUint(strings.TrimSpace(string(data)), 10, 64)
	return value
}

func service(path string) map[string]any {
	return map[string]any{
		"cpu":           counters(filepath.Join(path, "cpu.stat")),
		"memory_bytes":  scalar(filepath.Join(path, "memory.current")),
		"memory_events": counters(filepath.Join(path, "memory.events")),
		"pids":          scalar(filepath.Join(path, "pids.current")),
	}
}

func snapshot() map[string]any {
	mem := counters("/proc/meminfo")
	load, _ := os.ReadFile("/proc/loadavg")
	return map[string]any{
		"utc":                time.Now().UTC().Format(time.RFC3339Nano),
		"api":                service("/sys/fs/cgroup/system.slice/poetry-api.service"),
		"caddy":              service("/sys/fs/cgroup/system.slice/caddy.service"),
		"host_available_kib": mem["MemAvailable"],
		"host_swap_free_kib": mem["SwapFree"],
		"loadavg":            strings.TrimSpace(string(load)),
		"vmstat":             counters("/proc/vmstat"),
	}
}

func main() {
	report := flag.String("report", "", "New JSONL output file")
	stopFile := flag.String("stop-file", "", "Stop when this owned file exists")
	duration := flag.Duration("duration", 20*time.Minute, "Maximum sampling duration")
	flag.Parse()
	if *report == "" || *stopFile == "" {
		panic("report and stop-file required")
	}
	file, err := os.OpenFile(*report, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		panic(err)
	}
	defer file.Close()
	encoder := json.NewEncoder(file)
	deadline := time.Now().Add(*duration)
	for time.Now().Before(deadline) {
		if err := encoder.Encode(snapshot()); err != nil {
			panic(err)
		}
		if _, err := os.Stat(*stopFile); err == nil {
			break
		}
		time.Sleep(time.Second)
	}
	fmt.Println("Resource sampling finished")
}
