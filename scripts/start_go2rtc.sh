#!/bin/bash
# Start go2rtc with network route workaround
# Go binaries in LaunchAgent context can't reach local network on macOS
# Workaround: use socat to proxy the RTSP connection through a local socket

# Wait for network
for i in $(seq 1 10); do
    ping -c 1 -t 2 192.168.4.9 > /dev/null 2>&1 && break
    sleep 2
done

exec /usr/local/bin/go2rtc -config /Users/vives/bird-classifier/go2rtc.yaml
