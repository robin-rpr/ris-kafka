<img title="RIS Kafka" src="logo.svg" height="74" align="left" />

<br />
<br />

---

[https://ris-kafka.com](https://ris-kafka.com/?ref=github) — RIS Kafka

A high-performance message broker that streams BGP updates from RIPE NCC's Routing Information Service (RIS) Live websocket feed to Apache Kafka, with real-time JSON to BMPv3 protocol conversion.

> [!NOTE]
> This is an independent experimental service and not operated by or affiliated with the RIPE NCC. This service is relying on the RIPE NCC's Routing Information Service (RIS) websocket feed and is therefore subject to the same usage policies as the RIS itself. We want to acknowledge the RIPE NCC and RIPE Community for providing this invaluable service and for making it available to the public.

## Overview

This service connects to the [RIS Live](https://ris-live.ripe.net/) websocket endpoint to receive real-time BGP routing updates from RIPE NCC's global network of Route Collectors. It processes these messages by:

1. Converting the JSON-encoded BGP messages to BMPv3 wire format
2. Publishing to Kafka topics organized by collector and peer ASN
3. Maintaining message ordering with timestamps and unique IDs

We provide a public read-only cluster at `stream.ris-kafka.com:9092`:

```python
# Example Python Kafka Consumer
consumer = Consumer({
    'bootstrap.servers': 'stream.ris-kafka.org:9092',
    'group.id': f'my-example-group',
    'partition.assignment.strategy': 'roundrobin',
    'enable.auto.commit': False,
    'security.protocol': 'PLAINTEXT',
    'fetch.max.bytes': 50 * 1024 * 1024, # 50 MB
    'session.timeout.ms': 30000,  # For stable group membership
})
```

## Features

- Real-time BGP update streaming
- Native BMPv3 protocol support
- Horizontally scalable architecture
- Docker containerized deployment
- High Availability with automatic failover
  - Collector instances can run with unlimited replicas
  - Automatic leader election using Redis
  - Seamless failover with no message loss
  - Consistent message ordering maintained during failover

## Kafka Topics

The topic names are structured as lowercase `<collector_name>.<peer_asn>.bmp_raw`.
Additionally you can consult the [RIS Route Collector Documentation](https://ris.ripe.net/docs/route-collectors/) for more information on the available collectors.

- `<collector_name>.<peer_asn>.bmp_raw`
- `rrc13.2854.bmp_raw`
- ...

## Quick Start

1. Clone the repository:
```bash
git clone git@github.com:robin-rpr/ris-kafka.git
cd ris-kafka
```

2. Start the service:
```bash
docker compose up
```

> **Note:** This will start collecting from RRC13 by default. You can change the `RIS_HOST` in `docker-compose.yaml` to collect from another RIS Collector.

3. Open http://localhost:8080 (Kafbat Dashboard)

## High Availability Deployment

For production environments, you can run multiple replicas to ensure high availability:

```bash
docker compose up --scale app=3
```

This will start 3 collector instances with automatic leader election and failover capabilities:
- Only one instance actively collects data at a time
- Automatic failover if the leader becomes unavailable
- No message loss during failover
- Consistent message ordering maintained

# WhatsApp Communication

Join our WhatsApp group to discuss the service and get help.

<img title="WhatsApp" src="whatsapp.svg" height="165" align="left" />

