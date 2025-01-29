from websockets.asyncio.client import connect
from confluent_kafka import Producer
from collections import deque
from protocols.bmp import BMPv3
import redis.asyncio as redis
import logging
import asyncio
import socket
import uuid
import json
import os

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
KAFKA_FQDN = os.getenv("KAFKA_FQDN")
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS"))
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT"))
REDIS_DB = int(os.getenv("REDIS_DB"))
RIS_HOST = os.getenv("RIS_HOST")

class CircularBuffer:
    def __init__(self, size):
        self.size = size
        self.buffer = [None] * size
        self.start = 0
        self.end = 0
        self.count = 0

    def add(self, item):
        self.buffer[self.end] = item
        self.end = (self.end + 1) % self.size
        if self.count < self.size:
            self.count += 1
        else:
            # Overwrite the oldest element
            self.start = (self.start + 1) % self.size

    def get(self, index):
        if index < 0 or index >= self.count:
            raise IndexError("Index out of range")
        return self.buffer[(self.start + index) % self.size]

    def __len__(self):
        return self.count

class PersistentIterator:
    def __init__(self, buffer):
        self.buffer = buffer
        self.current_index = 0

    def next(self):
        if self.current_index < len(self.buffer):
            item = self.buffer.get(self.current_index)
            self.current_index += 1
            return item
        else:
             # Reached the end of the buffer
            return None

    def find(self, key, value):
        for i in range(len(self.buffer)):
            item = self.buffer.get(i)
            if item and item.get(key) == value:
                self.current_index = i
                return item
        # Not found
        return None  

    def reset(self):
        self.current_index = 0

# Acquire Leader Task
async def acquire_leader_task(redis_client, memory):
    while True:
        await asyncio.sleep(2)
        if not memory['is_leader']: 
            memory['is_leader'] = False if await redis_client.set(
                f"{RIS_HOST}_leader",
                memory['leader_id'],
                nx=True,
                ex=10 # seconds
            ) is None else True

# Renew Leader Task
async def renew_leader_task(redis_client, memory, logger):
    while True:
        await asyncio.sleep(5)
        if memory['is_leader']:
            try:
                current_leader = await redis_client.get(f"{RIS_HOST}_leader")
                if current_leader == memory['leader_id']:
                    await redis_client.expire(f"{RIS_HOST}_leader", 10)
                else:
                    memory['is_leader'] = False
            except Exception as e:
                logger.error(f"Error renewing leadership: {e}")
                memory['is_leader'] = False
        
# Consumer Task
async def consumer_task(buffer, memory):
    async with connect(f"{WEBSOCKET_URI}?client={WEBSOCKET_IDENTITY}") as ws:
        await ws.send(json.dumps({"type": "ris_subscribe", "data": {"host": RIS_HOST}}))

        async for data in ws:
            memory['receive_counter'][0] += len(data)  # Increment received data size
            marshal = json.loads(data)['data']
            buffer.add(marshal)

# Sender Task
async def sender_task(producer, redis_client, buffer, memory):
    loop = asyncio.get_event_loop()
    iterator = PersistentIterator(buffer)

    while True:
        # Get last message id and index# Get last message id and index
        last_id = await redis_client.get(f"{RIS_HOST}_last_id")
        last_index = await redis_client.get(f"{RIS_HOST}_last_index")
        last_completed = await redis_client.get(f"{RIS_HOST}_last_completed")

        # If we are the leader
        if memory['is_leader']:
            item = None

            # Set iterator to last message id
            if last_id:
                item = iterator.find("id", last_id)

            # If last transmission was completed
            if last_completed == "True" or last_completed is None:
                item = iterator.next()

            # If we reached the end of the buffer
            if item is not None:
                # Construct the BMP message
                messages = BMPv3.construct(
                    collector=RIS_HOST,
                    peer_ip=item['peer'],
                    peer_asn=int(item['peer_asn']),
                    timestamp=item['timestamp'],
                    msg_type='PEER_STATE' if item['type'] == 'RIS_PEER_STATE' else item['type'],
                    path=item.get('path', []),
                    origin=item.get('origin', 'INCOMPLETE'),
                    community=item.get('community', []),
                    announcements=item.get('announcements', []),
                    withdrawals=item.get('withdrawals', []),
                    state=item.get('state', None),
                    med=item.get('med', None)
                )

                # Send messages to Kafka
                for i, message in enumerate(messages):
                    # If last transmission was not completed
                    if last_completed == "False":
                        # Skip messages that were already sent
                        if i < last_index:
                            continue
                    
                    # Delivery Report Callback
                    def delivery_report(err, _):
                        if err is None:
                            asyncio.run_coroutine_threadsafe(
                                redis_client.set(f"{RIS_HOST}_last_id", item['id']), loop
                            )
                            asyncio.run_coroutine_threadsafe(
                                redis_client.set(f"{RIS_HOST}_last_index", str(i)), loop
                            )
                            asyncio.run_coroutine_threadsafe(
                                redis_client.set(f"{RIS_HOST}_last_completed", "True" if i == len(messages) - 1 else "False"), loop
                            )
                        else:
                            raise Exception(f"Message delivery failed: {err}")

                    producer.produce(
                        topic=f"{RIS_HOST}.{item['peer_asn']}.bmp_raw",
                        key=item['id'],
                        value=message,
                        timestamp=int(item['timestamp'] * 1000),
                        callback=delivery_report
                    )

                    # Increment sent data size
                    memory['send_counter'][0] += len(message) 

                    # Trigger delivery report callbacks
                    producer.poll(0) 
            else:
                # Reached the end of the buffer
                await asyncio.sleep(1)
                continue
        
        else:
            await asyncio.sleep(1)
            continue

            
# Logging Task
async def logging_task(redis_client, memory):
    while True:
        timeout = 10 # seconds
        received_kbps = (memory['receive_counter'][0] * 8) / 1024 / timeout  # Convert bytes to kilobits
        sent_kbps = (memory['send_counter'][0] * 8) / 1024 / timeout
        redis_connections = len(redis_client.connection_pool._in_use_connections)
        logger.info(f"host={RIS_HOST} is_leader={memory['is_leader']} receive={received_kbps:.2f} kbps send={sent_kbps:.2f} kbps redis_connections={redis_connections}")
        memory['receive_counter'][0] = 0 # Reset counters
        memory['send_counter'][0] = 0
        await asyncio.sleep(timeout)

# Main Coroutine
async def main():
    
    # Redis Connection
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        encoding="utf-8",
        max_connections=REDIS_MAX_CONNECTIONS,
        decode_responses=True
    )

    # Kafka Producer
    producer = Producer({
        'bootstrap.servers': KAFKA_FQDN,
        'enable.idempotence': True,
        'acks': 'all',
        'retries': 5,
        'linger.ms': 10
    })

    # Buffer
    buffer = CircularBuffer(500000)

    # Memory
    memory = {
        'receive_counter': [0],  # To store received data size in bytes
        'send_counter': [0],  # To store sent data size in bytes
        'is_leader': False, # Leader Lock (Leader Election)
        'leader_id': str(uuid.uuid4())  # Random UUID as leader ID
    }

    # Tasks
    tasks = []

    try:
        # Create an event to signal shutdown
        shutdown_event = asyncio.Event()

        # Start the leader tasks
        tasks.append(asyncio.create_task(acquire_leader_task(redis_client, memory)))
        tasks.append(asyncio.create_task(renew_leader_task(redis_client, memory, logger)))

        # Start the consumer and sender tasks
        tasks.append(asyncio.create_task(consumer_task(buffer, memory)))
        tasks.append(asyncio.create_task(sender_task(producer, redis_client, buffer, memory)))

        # Start the logging task
        tasks.append(asyncio.create_task(logging_task(redis_client, memory)))

        # Monitor tasks for exceptions
        for task in tasks:
            task.add_done_callback(lambda t: shutdown_event.set() if t.exception() else None)

        # Wait for the shutdown event
        await shutdown_event.wait()
    finally:
        logger.info("Shutting down...")

        # Cancel all running tasks
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Relinquish leadership
        if memory['is_leader']:
            await redis_client.delete(f"{RIS_HOST}_leader")
            memory['is_leader'] = False

        await redis_client.close()
        producer.flush()

if __name__ == "__main__":
    asyncio.run(main())
