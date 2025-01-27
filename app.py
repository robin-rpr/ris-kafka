import logging
import os
import json
import socket
import asyncio
from confluent_kafka import Producer
from protocols.bmp import BMPv3
import redis.asyncio as redis
import websocket

# Logger
logger = logging.getLogger(__name__)
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logger.setLevel(getattr(logging, log_level, logging.INFO))
ch = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

# Environment Variables
WEBSOCKET_URI = "wss://ris-live.ripe.net/v1/ws/"
WEBSOCKET_IDENTITY = f"ris-kafka-{socket.gethostname()}"
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_FQDN")
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = os.getenv("REDIS_PORT")
REDIS_DB = os.getenv("REDIS_DB")
RIS_HOST = os.getenv("RIS_HOST")

# Async function to log data transfer rates
async def log_transfer_rates(receive_counter, send_counter):
    while True:
        timeout = 10 # seconds
        received_kbps = (receive_counter[0] * 8) / 1024 / timeout  # Convert bytes to kilobits
        sent_kbps = (send_counter[0] * 8) / 1024 / timeout
        logger.info(f"host={RIS_HOST} receive={received_kbps:.2f} kbps send={sent_kbps:.2f} kbps")
        receive_counter[0] = 0  # Reset counters
        send_counter[0] = 0
        await asyncio.sleep(timeout)

# Main Coroutine
async def main():
    # Redis Connection
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        encoding="utf-8",
        max_connections=10
    )

    # Kafka Producer
    producer = Producer({
        'bootstrap.servers': KAFKA_BOOTSTRAP_SERVERS,
        'enable.idempotence': True,
        'acks': 'all',
        'retries': 5,
        'linger.ms': 10
    })

    # Counters for data transfer
    receive_counter = [0]  # To store received data size in bytes
    send_counter = [0]  # To store sent data size in bytes

    # Start the WebSocket Consumer
    try:
        ws = websocket.WebSocket()
        ws.connect(f"{WEBSOCKET_URI}?client={WEBSOCKET_IDENTITY}")
        ws.send(json.dumps({"type": "ris_subscribe", "data": {"host": RIS_HOST}}))

        # Start the logging task
        asyncio.create_task(log_transfer_rates(receive_counter, send_counter))

        for data in ws:
            receive_counter[0] += len(data)  # Increment received data size

            marshal = json.loads(data)['data']

            # Check if the message is a duplicate
            is_duplicate = await redis_client.exists(marshal['id'])
            if is_duplicate:
                continue

            # Construct the BMP message
            messages = BMPv3.construct(
                collector=RIS_HOST,
                peer_ip=marshal['peer'],
                peer_asn=int(marshal['peer_asn']),
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
                def delivery_report(err, msg):
                    if err is not None:
                        logger.error(f"Message delivery failed: {err}")

                producer.produce(
                    topic=f"{RIS_HOST}.{marshal['peer_asn']}.bmp_raw",
                    key=marshal['id'],
                    value=message,
                    timestamp=int(marshal['timestamp'] * 1000),
                    callback=delivery_report
                )
                send_counter[0] += len(message)  # Increment sent data size

                producer.poll(0)  # Trigger delivery report callbacks

            await redis_client.set(marshal['id'], "processed", ex=86400)

    finally:
        await redis_client.close()
        producer.flush()

if __name__ == "__main__":
    asyncio.run(main())
