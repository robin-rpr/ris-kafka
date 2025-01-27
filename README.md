# RIS Kafka Proxy

A high-performance message broker that streams BGP updates from RIPE NCC's Routing Information Service (RIS) Live websocket feed to Apache Kafka, with real-time JSON to BMPv3 protocol conversion.

## Overview

This service connects to the [RIS Live](https://ris-live.ripe.net/) websocket endpoint to receive real-time BGP routing updates from RIPE NCC's global network of Route Collectors. It processes these messages by:

1. Converting the JSON-encoded BGP messages to BMPv3 wire format
2. Deduplicating messages using Redis
3. Publishing to Kafka topics organized by collector and peer ASN
4. Maintaining message ordering with timestamps

## Features

- Real-time BGP update streaming
- Native BMPv3 protocol support
- Message deduplication
- Horizontally scalable architecture
- Docker containerized deployment
- Fault-tolerant message delivery

## Quick Start

1. Clone the repository:
```bash
git clone git@github.com:robin-rpr/ris-kafka-proxy.git
cd ris-kafka-proxy
```

2. Start all services:
```bash
docker compose up -d
```

> **Heads up:** This will start collecting from RRC13. You can change the `RIS_HOST` in `docker-compose.yaml` to collect from another RIS Collector.

3. Open http://localhost:8080
