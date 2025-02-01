from websockets.asyncio.client import connect
from datetime import datetime, timedelta
from contextlib import contextmanager
from confluent_kafka import Producer
import redis.asyncio as redis_async
from protocols.bmp import BMPv3
import redis as redis_sync
import ipaddress
import logging
import asyncio
import socket
import time
import uuid
import json
import os

# Environment Variables
WEBSOCKET_URI = "wss://ris-live.ripe.net/v1/ws/"
WEBSOCKET_IDENTITY = f"ris-kafka-{socket.gethostname()}"
ENSURE_CONTINUITY = os.getenv("ENSURE_CONTINUITY", "true") == "true"
ENABLE_PROFILING = os.getenv("ENABLE_PROFILING", "false") == "true"
BUFFER_SIZE = int(os.getenv("BUFFER_SIZE", 10000))
BUFFER_PADDING = int(os.getenv("BUFFER_PADDING", 100))
TIME_LAG_LIMIT = int(os.getenv("TIME_LAG_LIMIT", 10))
BATCH_CONSUME = int(os.getenv("BATCH_CONSUME", 1000))
BATCH_SEND = int(os.getenv("BATCH_SEND", 1000))
KAFKA_FQDN = os.getenv("KAFKA_FQDN")
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", 20))
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT"))
REDIS_DB = int(os.getenv("REDIS_DB"))
RIS_HOST = os.getenv("RIS_HOST")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Logger
logger = logging.getLogger(__name__)
log_level = os.getenv('LOG_LEVEL', LOG_LEVEL).upper()
logger.setLevel(getattr(logging, log_level, logging.INFO))
ch = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

# Profiling
@contextmanager
def profile_section(section_name, profile_output_dir="profiles", profile_every=1, call_count=[0]):
    """
    Context manager to wall-clock a specific section of code.
    
    Args:
        section_name (str): Name of the code section being measured.
        profile_output_dir (str): Directory where timing reports are saved.
    """
    call_count[0] += 1
    if call_count[0] % profile_every != 0:
        yield # Do not profile
    else:
        start_time = time.perf_counter()
        try:
            yield
        finally:
            end_time = time.perf_counter()
            duration = end_time - start_time
            os.makedirs(profile_output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            profile_filename = f"{profile_output_dir}/{section_name}_{timestamp}.txt"
            with open(profile_filename, 'w') as f:
                f.write(f"--- Timing Report: {section_name} at {timestamp} ---\n")
                f.write(f"Duration: {duration:.6f} seconds\n")
            logger.debug(f"Timing report saved to {profile_filename} with duration {duration:.6f} seconds")

@contextmanager
def normal_section():
    yield

class CircularBuffer:
    def __init__(self, size, padding):
        self.pointer = size - padding - 1
        self.padding = padding
        self.size = size
        self.buffer = [None] * size
        self.sorted = False
        self.locked = False

    def append(self, item):
        self.buffer.pop(0)
        self.buffer.append(item)
            
        # Move pointer to the left
        self.pointer -= 1
        if self.pointer < 0:
            # Pointer out of bounds
            self.pointer = 0

        self.sorted = False

    def sort(self):
        if not self.sorted:
            # Count nones
            nones = 0
            for item in self.buffer:
                if item is not None:
                    break
                nones += 1

            # Retrieve current pointed item
            pointed = self.buffer[self.pointer] if self.pointer >= nones else None
            
            # Sort items that are not None
            sorted_slice = sorted(self.buffer[nones:], key=lambda item: (
                int(ipaddress.ip_address(item["peer"])), # Group by peer (same peer together)
                item["timestamp"], # Within each peer, sort by timestamp (earliest first)
                int(item["id"].split('-')[-1], 16)  # If multiple messages have the same timestamp, order by sequence_id
            ))
            
            # Reconstruct buffer with None padding at start
            self.buffer = [None] * nones + sorted_slice

            # Reset pointer
            if pointed is not None:
                self.seek('id', pointed['id'], force=True)

            # Release lock
            self.sorted = True
            
    def seek(self, key, value, force=False):
        if self.sorted or force:
            # Binary search implementation
            left = 0
            right = len(self.buffer) - self.padding - 1
            
            while left <= right:
                mid = (left + right) // 2
                item = self.buffer[mid]
                
                if item is None:
                    right = mid - 1
                    continue
                    
                # Compare based on our sorting criteria
                if key == "peer":
                    current_value = int(ipaddress.ip_address(item["peer"]))
                    search_value = int(ipaddress.ip_address(value))
                elif key == "timestamp":
                    current_value = item["timestamp"]
                    search_value = value
                elif key == "id":
                    current_value = int(item["id"].split('-')[-1], 16)
                    search_value = int(value.split('-')[-1], 16)
                else:
                    current_value = item.get(key)
                    search_value = value
                
                if current_value == search_value:
                    self.pointer = mid
                    return item
                elif current_value < search_value:
                    left = mid + 1
                else:
                    right = mid - 1
                    
            return None

    def next(self):
        if self.sorted and self.pointer + 1 < self.size - self.padding:
            self.pointer += 1
            return self.buffer[self.pointer]
        return None

    def __len__(self):
        return self.size

# Acquire Leader Task
async def acquire_leader_task(redis_async_client, memory):
    while True:
        await asyncio.sleep(2)
        if not memory['is_leader']: 
            memory['is_leader'] = False if await redis_async_client.set(
                f"{RIS_HOST}_leader",
                memory['leader_id'],
                nx=True,
                ex=10 # seconds
            ) is None else True

# Renew Leader Task
async def renew_leader_task(redis_async_client, memory, logger):
    while True:
        await asyncio.sleep(5)
        if memory['is_leader']:
            try:
                current_leader = await redis_async_client.get(f"{RIS_HOST}_leader")
                if current_leader == memory['leader_id']:
                    await redis_async_client.expire(f"{RIS_HOST}_leader", 10)
                else:
                    memory['is_leader'] = False
            except Exception as e:
                logger.error(f"Error renewing leadership: {e}")
                memory['is_leader'] = False
        
# Consumer Task
async def consumer_task(buffer, memory):
    async with connect(f"{WEBSOCKET_URI}?client={WEBSOCKET_IDENTITY}") as ws:
        await ws.send(json.dumps({"type": "ris_subscribe", "data": {"host": RIS_HOST}}))
        batch = []

        async for data in ws:
            memory['receive_counter'][0] += len(data)
            marshal = json.loads(data)['data']

            # Filter out non-implemented messages
            # TODO: Implement also "STATE", "OPEN", and "NOTIFICATION"
            if marshal['type'] not in ["UPDATE", "KEEPALIVE", "RIS_PEER_STATE"]:
                continue

            # Add message to buffer
            batch.append(marshal)

            # Sort and reset batch
            if len(batch) == BATCH_CONSUME:
                with profile_section("Buffer_extend") if ENABLE_PROFILING else normal_section():
                    for item in batch:
                        buffer.append(item)
                    buffer.sort()
                batch = []
                

# Sender Task
async def sender_task(producer, redis_async_client, redis_sync_client, buffer, memory):
    initialized = False
    produced_size = 0
    reported_size = 0
    batch_size = 0
    item = None

    # If we lost messages
    if await redis_async_client.get(f"{RIS_HOST}_batch_reported") == "False":
        # Last time we were not able to send all messages.
        # We have no safe way of recovering from this without data loss.
        if ENSURE_CONTINUITY:
            raise Exception("Unrecoverable message loss detected.")
        else:
            logger.warning("Unrecoverable message loss detected.")

    while True:
        # Get details about the last batch
        batch_id = await redis_async_client.get(f"{RIS_HOST}_batch_id")

        # If we are the leader
        if memory['is_leader']:
            # If we have not yet initialized
            if batch_id is not None and not initialized:
                buffer.seek('id', batch_id, force=True)
                initialized = True

            # If not all messages have been reported
            if batch_size == BATCH_SEND and reported_size < produced_size:
                logger.warning("Kafka message processing is falling behind. Delaying by 200ms")
                await asyncio.sleep(0.20)
                producer.flush()
                continue

            # If all messages have been reported
            if batch_size == BATCH_SEND and reported_size == produced_size:
                redis_sync_client.set(f"{RIS_HOST}_batch_id", item['id'])
                redis_sync_client.set(f"{RIS_HOST}_batch_reported", "True")
                reported_size = 0
                produced_size = 0
                batch_size = 0

            # Retrieve next item in the buffer
            with profile_section("Buffer_next", profile_every=1000) if ENABLE_PROFILING else normal_section():
                item = buffer.next()

            if item is not None:
                # Increment counter for batch
                batch_size += 1

                # Construct the BMP message
                with profile_section("BMPv3_construct", profile_every=1000) if ENABLE_PROFILING else normal_section():
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
                for message in messages:
                    # Delivery Report Callback
                    # NOTE: Kafka does not guarantee that delivery reports will be received in the same order as the messages were sent
                    def delivery_report(err, _):
                        nonlocal reported_size
                        if err is None:
                            reported_size += 1
                        else:
                            raise Exception(f"Message delivery failed: {err}")

                    # Produce message to Kafka
                    with profile_section("Kafka_produce", profile_every=1000) if ENABLE_PROFILING else normal_section():
                        producer.produce(
                            topic=f"{RIS_HOST}.{item['peer_asn']}.bmp_raw",
                            key=item['id'],
                            value=message,
                            timestamp=int(item['timestamp'] * 1000),
                            callback=delivery_report
                        )

                    # Set last delivered to false
                    await redis_async_client.set(f"{RIS_HOST}_batch_reported", "False")

                    # Increment counter for produced
                    produced_size += 1

                    # Update the approximated time lag
                    memory['time_lag'] = datetime.now() - datetime.fromtimestamp(int(item['timestamp']))

                    # Increment sent data size
                    memory['send_counter'][0] += len(message)

                # Poll Kafka producer
                producer.poll(0)

                # Terminate if time lag is greater than TIME_LAG_LIMIT minutes
                if memory['time_lag'].total_seconds() / 60 > TIME_LAG_LIMIT:
                    raise Exception("Time lag is too high.")
            else:
                await asyncio.sleep(0.1)
        else:
            await asyncio.sleep(1)

            
# Logging Task
async def logging_task(memory):
    while True:
        timeout = 10 # seconds

        # Calculate kbps
        received_kbps = (memory['receive_counter'][0] * 8) / 1024 / timeout  
        sent_kbps = (memory['send_counter'][0] * 8) / 1024 / timeout

        # Calculate time lag
        h, remainder = divmod(memory['time_lag'].total_seconds(), 3600)
        m, s = divmod(remainder, 60)

        # Log out
        logger.info(f"host={RIS_HOST} is_leader={memory['is_leader']} receive={received_kbps:.2f} kbps send={sent_kbps:.2f} kbps lag={int(h)}h {int(m)}m {int(s)}s")

        # Reset counters
        memory['receive_counter'][0] = 0 
        memory['send_counter'][0] = 0

        await asyncio.sleep(timeout)

# Main Coroutine
async def main():
    
    # Asynchronous Redis Connection
    redis_async_client = redis_async.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        encoding="utf-8",
        max_connections=REDIS_MAX_CONNECTIONS,
        decode_responses=True
    )

    # Synchronous Redis Connection
    redis_sync_client = redis_sync.Redis(
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
        'retries': 10,
        'compression.type': 'lz4'
    })

    # Buffer
    buffer = CircularBuffer(BUFFER_SIZE, BUFFER_PADDING)

    # Memory
    memory = {
        'receive_counter': [0],  # To store received data size in bytes
        'send_counter': [0],  # To store sent data size in bytes
        'batch_counter': 0, # To store batch counter
        'is_leader': False, # Leader Lock (Leader Election)
        'leader_id': str(uuid.uuid4()),  # Random UUID as leader ID
        'time_lag': timedelta(0), # Time lag in seconds
    }

    # Tasks
    tasks = []

    try:
        logger.info("Starting ...")

        # Create an event to signal shutdown
        shutdown_event = asyncio.Event()

        # Start the leader tasks
        tasks.append(asyncio.create_task(acquire_leader_task(redis_async_client, memory)))
        tasks.append(asyncio.create_task(renew_leader_task(redis_async_client, memory, logger)))

        # Start the consumer and sender tasks
        tasks.append(asyncio.create_task(consumer_task(buffer, memory)))
        tasks.append(asyncio.create_task(sender_task(producer, redis_async_client, redis_sync_client, buffer, memory)))

        # Start the logging task
        tasks.append(asyncio.create_task(logging_task(memory)))

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
            await redis_async_client.delete(f"{RIS_HOST}_leader")
            memory['is_leader'] = False

        # Flush Kafka producer
        producer.flush()

        # Ensure all messages got delivered
        logger.debug("Waiting for messages to be delivered...")
        while await redis_async_client.get(f"{RIS_HOST}_batch_reported") == "False":
            logger.info("Messages still in queue or transit. Delaying shutdown by 1000ms")
            await asyncio.sleep(1)
        logger.debug("All messages delivered. Shutting down...")
        

        # Close Redis connections
        await redis_async_client.close()
        redis_sync_client.close()

if __name__ == "__main__":
    asyncio.run(main())
