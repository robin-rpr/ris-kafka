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
import signal
import time
import uuid
import json
import os

# Environment Variables
WEBSOCKET_URI = "wss://ris-live.ripe.net/v1/ws/"
WEBSOCKET_IDENTITY = f"ris-kafka-{socket.gethostname()}"
ENABLE_PROFILING = os.getenv("ENABLE_PROFILING", "false") == "true"
BUFFER_SIZE = int(os.getenv("BUFFER_SIZE", 10000))
BUFFER_PADDING = int(os.getenv("BUFFER_PADDING", 100))
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
    """
    A circular buffer that can be sorted and seeked.
    """
    def __init__(self, size, padding):
        self.pointer = size - padding - 1
        self.padding = padding
        self.size = size
        self.buffer = [None] * size
        self.sorted = False
        self.locked = False

    def append(self, item):
        """
        Append an item to the buffer.

        Args:
            item (dict): The item to append to the buffer.
        """
        self.buffer.pop(0)
        self.buffer.append(item)
            
        # Move pointer to the left
        self.pointer -= 1
        if self.pointer < 0:
            # Pointer out of bounds
            raise Exception("Exceeded buffer size")

        self.sorted = False

    def sort(self):
        """
        Sort the buffer.
        """
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
        """
        Seek to the first item that matches the key and value.

        Args:
            key (str): The key to seek by.
            value (str): The value to seek by.
            force (bool): Whether to force the seek.
        """
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
        """
        Get the next item in the buffer.
        """
        if self.sorted and self.pointer + 1 < self.size - self.padding:
            self.pointer += 1
            return self.buffer[self.pointer]
        return None

    def __len__(self):
        """
        Get the length of the buffer.
        """
        return self.size

async def leader_task(redis_async_client, memory, interval=2, ttl=30):
    """
    A single task that continuously tries to acquire or renew leadership.
    interval: how often (in seconds) to attempt acquiring or renewing.
    ttl: the time-to-live in Redis for the leadership key (in seconds).

    Args:
        redis_async_client (redis.asyncio.Redis): The Redis client.
        memory (dict): The memory dictionary.
        interval (int): The interval in seconds to attempt acquiring or renewing leadership.
        ttl (int): The time-to-live in Redis for the leadership key (in seconds).
    """
    leader_key = f"{RIS_HOST}_leader"

    while True:
        try:
            # If not the leader, attempt to become the leader
            if not memory['is_leader']:
                # SET if not exists, with a TTL
                result = await redis_async_client.set(
                    leader_key,
                    memory['leader_id'],
                    nx=True,
                    ex=ttl
                )
                # If we succeeded in setting the key, we are the leader
                if result is not None:
                    memory['is_leader'] = True
                    logger.info("Acquired leadership")
            else:
                # If already leader, confirm it's still us
                current_leader = await redis_async_client.get(leader_key)
                if current_leader == memory['leader_id']:
                    # Refresh TTL
                    await redis_async_client.expire(leader_key, ttl)
                else:
                    # We lost leadership
                    memory['is_leader'] = False
                    raise Exception("Lost leadership")

            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            # Stop gracefully if the task is cancelled
            raise
        except Exception as e:
            # Log the error, but do not kill the entire loop
            memory['is_leader'] = False
            # logger.exception(f"Leader election task exception: {e}")
            await asyncio.sleep(interval)

# Consumer Task
async def consumer_task(buffer, memory):
    """
    A single task that continuously consumes messages from the websocket.

    Args:
        buffer (CircularBuffer): The circular buffer.
        memory (dict): The memory dictionary.
    """
    try:
        async with connect(f"{WEBSOCKET_URI}?client={WEBSOCKET_IDENTITY}") as ws:
            await ws.send(json.dumps({"type": "ris_subscribe", "data": {"host": RIS_HOST, "socketOptions": {"includeRaw": True}}}))
            batch = []
            
            async for data in ws:
                memory['receive_counter'][0] += len(data)
                marshal = json.loads(data)['data']

                if marshal['type'] == "ris_error":
                    raise Exception(f"Websocket error: {marshal['message']}")

                # Allow only specific messages
                # TODO: Implement also "STATE" and "RIS_PEER_STATE"
                #       Blocked by: https://fosstodon.org/@robinroeper/113909690169940714
                if marshal['type'] not in ["UPDATE", "OPEN", "NOTIFICATION", "KEEPALIVE"]:
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
    except asyncio.CancelledError:
        raise
                
# Sender Task
async def sender_task(producer, redis_async_client, redis_sync_client, buffer, memory):
    """
    A single task that continuously sends messages to Kafka.

    Args:
        producer (confluent_kafka.Producer): The Kafka producer.
        redis_async_client (redis.asyncio.Redis): The Redis client.
        redis_sync_client (redis.Redis): The Redis client.
        buffer (CircularBuffer): The circular buffer.
        memory (dict): The memory dictionary.
    """
    try:
        initialized = False
        seeked = False
        produced_size = 0
        reported_size = 0
        batch_size = 0
        latest = None
        item = None

        # Delivery Report Callback
        # NOTE: Kafka does not guarantee that delivery reports will be received in the same order as the messages were sent
        def delivery_report(err, _):
            nonlocal reported_size
            if err is None:
                reported_size += 1
            else:
                raise Exception(f"Message delivery failed: {err}")

        while True:
            # Get details about the last batch
            batch_id = await redis_async_client.get(f"{RIS_HOST}_batch_id")

            if not memory['is_leader']:
                # Deinitialize
                initialized = False
                seeked = False
                # Wait for 1 second
                await asyncio.sleep(1)
                continue
            else:
                # If we lost messages since the last execution
                if not initialized:
                    initialized = True
                    if await redis_async_client.get(f"{RIS_HOST}_batch_reported") == "False" or \
                        await redis_async_client.get(f"{RIS_HOST}_batch_transacting") == "True":
                        # Last time we seem to have been not able to send all messages.
                        # We have no safe way of recovering from this without data loss.
                        # A softlock state is the only option to prevent data corruption.
                        raise Exception("Softlock detected.")

                # If we need to seek
                if batch_id is not None and not seeked:
                    buffer.seek('id', batch_id)
                    seeked = True

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
                    # Whether message is metadata
                    is_metadata = False

                    # Construct the BMP message
                    with profile_section("BMPv3_construct", profile_every=1000) if ENABLE_PROFILING else normal_section():
                        match item['type']:
                            # TODO: Handle RIS Peer State 'STATE' and 'RIS_PEER_STATE'
                            case 'UPDATE':
                                message = BMPv3.monitoring_message(
                                    peer_ip=item['peer'],
                                    peer_asn=int(item['peer_asn']),
                                    timestamp=item['timestamp'],
                                    bgp_update=bytes.fromhex(item['raw']),
                                    collector=RIS_HOST
                                )
                            case 'OPEN':
                                message = BMPv3.peer_up_message(
                                    peer_ip=item['peer'],
                                    peer_asn=int(item['peer_asn']),
                                    timestamp=item['timestamp'],
                                    bgp_open=bytes.fromhex(item['raw']),
                                    collector=RIS_HOST
                                )
                            case 'NOTIFICATION':
                                message = BMPv3.peer_down_message(
                                    peer_ip=item['peer'],
                                    peer_asn=int(item['peer_asn']),
                                    timestamp=item['timestamp'],
                                    bgp_notification=bytes.fromhex(item['raw']),
                                    collector=RIS_HOST
                                )
                            case 'KEEPALIVE':
                                message = BMPv3.keepalive_message(
                                    peer_ip=item['peer'],
                                    peer_asn=int(item['peer_asn']),
                                    timestamp=item['timestamp'],
                                    bgp_keepalive=bytes.fromhex(item['raw']),
                                    collector=RIS_HOST
                                )
                            case 'RIS_PEER_STATE':
                                # Internal state of the peer
                                # NOTE: This is not a BGP state!
                                is_metadata = True
                                if item['state'] == 'connected':
                                    message = BMPv3.peer_up_message(
                                        peer_ip=item['peer'],
                                        peer_asn=int(item['peer_asn']),
                                        timestamp=item['timestamp'],
                                        bgp_open=bytes.fromhex(item['raw']),
                                        collector=RIS_HOST
                                    )
                                elif item['state'] == 'down':
                                    message = BMPv3.peer_down_message(
                                        peer_ip=item['peer'],
                                        peer_asn=int(item['peer_asn']),
                                        timestamp=item['timestamp'],
                                        bgp_notification=bytes.fromhex(item['raw']),
                                        collector=RIS_HOST
                                    )
                            case 'STATE':
                                # Unknown message type
                                # Read more: https://fosstodon.org/@robinroeper/113909690169940714
                                continue
                            case _:
                                raise Exception(f"Unexpected type: {item['type']}")
                            
                    # Set transacting to true
                    await redis_async_client.set(f"{RIS_HOST}_batch_transacting", "True")

                    # Send messages to Kafka
                    with profile_section("Kafka_produce", profile_every=1000) if ENABLE_PROFILING else normal_section():
                        producer.produce(
                            topic=f"{RIS_HOST}.{item['peer_asn'] if not is_metadata else 'meta'}.bmp_raw",
                            key=item['id'],
                            value=message,
                            timestamp=int(item['timestamp'] * 1000),
                            callback=delivery_report
                        )

                    # Set latest delivered item
                    latest = item

                    # Increment counter for batch
                    batch_size += 1

                    # Set last delivered to false
                    await redis_async_client.set(f"{RIS_HOST}_batch_reported", "False")

                    # Increment counter for produced
                    produced_size += 1

                    # Set transacting to false
                    await redis_async_client.set(f"{RIS_HOST}_batch_transacting", "False")

                    # Update the approximated time lag
                    memory['time_lag'] = datetime.now() - datetime.fromtimestamp(int(item['timestamp']))

                    # Increment sent data size
                    memory['send_counter'][0] += len(message)

                    # Poll Kafka producer
                    producer.poll(0)
                else:
                    # Wait for 100ms
                    await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        # If we are interrupted amid an active message delivery transaction
        if not await redis_async_client.get(f"{RIS_HOST}_batch_transacting") == "True":
            # Ensure all messages got delivered
            while await redis_async_client.get(f"{RIS_HOST}_batch_reported") == "False":
                # If not all messages have been reported
                if reported_size < produced_size:
                    logger.warning("Messages still in queue or transit. Delaying shutdown by 1000ms")
                    await asyncio.sleep(1)
                    producer.flush()
                    continue

                # If all messages have been reported
                if reported_size == produced_size:
                    logger.info("Outstanding messages delivered")
                    redis_sync_client.set(f"{RIS_HOST}_batch_id", latest['id'])
                    redis_sync_client.set(f"{RIS_HOST}_batch_reported", "True")
        raise

# Logging Task
async def logging_task(memory):
    """
    A single task that continuously logs the current state of the system.

    Args:
        memory (dict): The memory dictionary.
    """
    try:
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
    except asyncio.CancelledError:
        raise

# Signal handler
def handle_shutdown(signum, frame, shutdown_event):
    """
    Signal handler for shutdown.

    Args:
        signum (int): The signal number.
        frame (frame): The frame.
        shutdown_event (asyncio.Event): The shutdown event.
    """
    logger.info(f"Signal {signum}. Triggering shutdown...")
    shutdown_event.set()

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
        logger.info("Starting up...")

        # Create an event to signal shutdown
        shutdown_event = asyncio.Event()

        # Register SIGTERM handler
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, handle_shutdown, signal.SIGTERM, None, shutdown_event)
        loop.add_signal_handler(signal.SIGINT, handle_shutdown, signal.SIGINT, None, shutdown_event)  # Handle Ctrl+C

        # Start the leader tasks
        tasks.append(asyncio.create_task(leader_task(redis_async_client, memory)))

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

        # Close Redis connections
        await redis_async_client.close()
        redis_sync_client.close()

        logger.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
