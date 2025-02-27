<img title="RIS Kafka" src="logo.svg" height="125" align="left" />

<br />
<br />
<br />
<br />
<br />

---

[https://ris-kafka.com](https://ris-kafka.com/?ref=github) — Routing Information Service (RIS) Kafka

A high-performance message broker that streams BGP updates from RIPE NCC's Routing Information Service (RIS) Live websocket feed to Apache Kafka, with real-time JSON to BMPv3 protocol conversion.

## Overview

This service connects to the [RIS Live](https://ris-live.ripe.net/) websocket endpoint to receive real-time BGP routing updates from RIPE NCC's global network of Route Collectors. It processes these messages by:

1. Converting the JSON-encoded BGP messages to BMPv3 wire format
2. Publishing to Kafka topics organized by collector and peer ASN
3. Maintaining message ordering with timestamps and unique IDs

We provide a public read-only cluster at `stream.ris-kafka.com:9092`:

```python
# Example Python Kafka Consumer
consumer = Consumer({
    'bootstrap.servers': 'stream.ris-kafka.com:9092',
    'group.id': 'my-example-group',
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
  - Automatic leader election using Zookeeper
  - Seamless failover with no message loss
  - Consistent message ordering maintained during failover

## Kafka Topics

The topic names are structured as lowercase `<collector_name>.<peer_asn>.bmp_raw` and a singular `<collector_name>.meta.bmp_raw`.
Additionally you can consult the [RIS Route Collector Documentation](https://ris.ripe.net/docs/route-collectors/) for more information on the available collectors.

**Metadata topic with RIS (internal) Peer up / down messages:**
> Messages do not represent a BGP state but the RRC's <collector_name> internal connection status to any of its peers <peer_asn>. Contains only Peer up / down BMPv3 Messages.

- `<collector_name>.meta.bmp_raw`
- `rrc04.meta.bmp_raw`
- ...

**Per Peer ASN topic with all BGP Messages:**
- `<collector_name>.<peer_asn>.bmp_raw`
- `rrc04.15547.bmp_raw`
- ...

## Prerequisites

Before you begin, ensure you have the following installed on your system:

-   [Docker](https://docs.docker.com/get-docker/)
-   [Docker Compose](https://docs.docker.com/compose/install/)
-   [Git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)
-   [Jinja2 CLI](https://github.com/mattrobenolt/jinja2-cli)

## Getting Started

1. Clone the repository:
```bash
git clone git@github.com:robin-rpr/ris-kafka.git
cd ris-kafka
```

2. Start the service:
```sh
jinja2 docker-compose.jinja values.yaml | docker compose -f - up
```

> **Note:** This will start collecting from all RIS Collectors. You can further specify to collect from a specific host by modifying the `values.yaml` file. 

3. Open http://localhost:8080 (Kafbat Dashboard)

## Production Deployment

For production deployment, we recommend using Docker Swarm.

1. Specify the constraints:
```sh
docker node update --label-add ris-kafka=kafka <NODE_NAME>
docker node update --label-add ris-kafka=rrc00 <NODE_NAME>
docker node update --label-add ris-kafka=rrc01 <NODE_NAME>
docker node update --label-add ris-kafka=rrc03 <NODE_NAME>
docker node update --label-add ris-kafka=rrc04 <NODE_NAME>
docker node update --label-add ris-kafka=rrc05 <NODE_NAME>
docker node update --label-add ris-kafka=rrc06 <NODE_NAME>
docker node update --label-add ris-kafka=rrc07 <NODE_NAME>
docker node update --label-add ris-kafka=rrc10 <NODE_NAME>
docker node update --label-add ris-kafka=rrc11 <NODE_NAME>
docker node update --label-add ris-kafka=rrc12 <NODE_NAME>
docker node update --label-add ris-kafka=rrc13 <NODE_NAME>
docker node update --label-add ris-kafka=rrc14 <NODE_NAME>
docker node update --label-add ris-kafka=rrc15 <NODE_NAME>
docker node update --label-add ris-kafka=rrc16 <NODE_NAME>
docker node update --label-add ris-kafka=rrc18 <NODE_NAME>
docker node update --label-add ris-kafka=rrc19 <NODE_NAME>
docker node update --label-add ris-kafka=rrc20 <NODE_NAME>
docker node update --label-add ris-kafka=rrc21 <NODE_NAME>
docker node update --label-add ris-kafka=rrc22 <NODE_NAME>
docker node update --label-add ris-kafka=rrc23 <NODE_NAME>
docker node update --label-add ris-kafka=rrc24 <NODE_NAME>
docker node update --label-add ris-kafka=rrc25 <NODE_NAME>
docker node update --label-add ris-kafka=rrc26 <NODE_NAME>
```

2. Deploy the service:
```sh
curl -fsSL https://downloads.ris-kafka.com/docker-compose.yml | docker stack deploy -c - ris-kafka
```

This will start all collector instances with automatic leader election and failover capabilities:
- Only one instance actively sends data at a time
- Automatic failover if the leader becomes unavailable
- No message loss during failover
- Consistent message ordering maintained

## License

See [LICENSE](LICENSE)

