
from confluent_kafka import Producer
from protocols.bmp import BMPv3
import redis.asyncio as redis
import websocket
import asyncio
import json
import socket
import json
import os

# Environment Variables
WEBSOCKET_URI = "wss://ris-live.ripe.net/v1/ws/"
WEBSOCKET_IDENTITY = f"ris-kafka-{socket.gethostname()}"
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_FQDN")
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = os.getenv("REDIS_PORT")
REDIS_DB = os.getenv("REDIS_DB")
RIS_HOST = os.getenv("RIS_HOST")

# Main Coroutine
async def main():
    # Redis Connection
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        encoding="utf-8",
        max_connections=10  # Equivalent to maxsize
    )

    # Kafka Producer
    producer = Producer({
        'bootstrap.servers': KAFKA_BOOTSTRAP_SERVERS,
        'enable.idempotence': True,  # Enable idempotence for safe retries
        'acks': 'all',
        'retries': 5,
        'linger.ms': 10
    })

    # Start the WebSocket Consumer
    try:
        ws = websocket.WebSocket()
        ws.connect(f"{WEBSOCKET_URI}?client={WEBSOCKET_IDENTITY}")
        ws.send(json.dumps({"type": "ris_subscribe", "data": {"host": RIS_HOST}}))

        for data in ws:
            marshal = json.loads(data)['data']

            # Check if the message is a duplicate
            is_duplicate = await redis_client.exists(marshal['id'])
            if is_duplicate:
                continue

            # Construct the BMP message
            # JSON Schema: https://ris-live.ripe.net/manual/
            messages =BMPv3.construct(
                collector=RIS_HOST,
                peer_ip=marshal['peer'],
                peer_asn=int(marshal['peer_asn']), # Cast to int from string
                timestamp=marshal['timestamp'],
                msg_type='PEER_STATE' if marshal['type'] == 'RIS_PEER_STATE' else marshal['type'],
                path=marshal.get('path', []),
                origin=marshal.get('origin', 'INCOMPLETE'),
                community=marshal.get('community', []),
                announcements=marshal.get('announcements', []),
                withdrawals=marshal.get('withdrawals', []),
                state=marshal.get('state', None),
                med=marshal.get('med', None)
            )

            for message in messages:
                # Callback for Producer Delivery Reports
                def delivery_report(err, msg):
                    if err is not None:
                        raise Exception(f"Message delivery failed: {err}, {msg}")
                        
                # Produce the message to Kafka
                producer.produce(
                    topic=f"{RIS_HOST}.{marshal['peer_asn']}.bmp_raw",
                    key=marshal['id'],
                    value=message,
                    timestamp=int(marshal['timestamp'] * 1000),
                    callback=delivery_report
                )

                # Trigger delivery report callbacks
                producer.poll(0)
            
            # Mark as processed in Redis (expire 24 hours)
            await redis_client.set(marshal['id'], "processed", expire=86400)
    finally:
        await redis_client.close()
        producer.flush()

if __name__ == "__main__":
    asyncio.run(main())
