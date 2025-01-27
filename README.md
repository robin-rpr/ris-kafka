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

To start the service with all dependencies (Kafka, Zookeeper, Redis):
