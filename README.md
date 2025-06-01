<img title="RIS Kafka" src="logo.svg" height="125" align="left" />

<br />
<br />
<br />
<br />

---

[https://ris-kafka.com](https://ris-kafka.com/?ref=github) — Unofficial Routing Information Service (RIS) Kafka

A high-performance message broker that streams BGP updates from RIPE NCC's [Routing Information Service (RIS)](https://www.ripe.net/analyse/internet-measurements/routing-information-service-ris/) Live websocket feed to Apache Kafka, with real-time JSON to BMPv3 protocol conversion.

## Overview

This service connects to the [RIS Live](https://ris-live.ripe.net/) websocket endpoint to receive real-time BGP routing updates from RIPE NCC's global network of Remote Route Collectors (RRCs). It processes these messages by:

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
    'session.timeout.ms': 30000, # For stable group membership
})
```

You can check the current status of this public service at [status.superclustr.net](https://status.superclustr.net).

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

> **Note:** This will start collecting from all RIS Collectors. You can further specify to collect from a specific host by modifying the `values.yaml` file or by specifying the collector name e.g. `jinja2 docker-compose.jinja values.yaml | docker compose -f - up rrc04`.

3. Open http://localhost:8080 (Kafbat Dashboard)

## Production Deployment

For production deployment, we use Docker Swarm, but any other orchestration tool may be used.
The recommended system requirements are a minimum of 16 GB of RAM and 8 vCPU cores.
Additionally, by default Kafka is _allowed_ to scale up to each 24 Network and I/O threads.

```sh
curl -fsSL https://downloads.ris-kafka.com/docker-compose.yml | docker stack deploy -c - ris-kafka
```

> **Note:** For more fine grained control, you can override the default of your deployment values by using [Docker's Environment Variable Interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation). Please refer to this [docker-compose.yml](https://downloads.ris-kafka.com/docker-compose.yml) file for more information. You can export or prepend these environment variables before running the `docker stack deploy` command above.

This will start all collector instances with automatic leader election and failover capabilities:
- Only one instance actively sends data at a time
- Automatic failover if the leader becomes unavailable
- No message loss during failover
- Consistent message ordering maintained

## License

See [LICENSE](LICENSE)

